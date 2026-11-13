#!/usr/bin/env python3
"""Score a saved global model on the test split of one or more sites (run at a site, no FLARE).

Usage:
    python evaluate.py --model FL_global_model.pt --data SITE_DIR [--out result.json]
    python evaluate.py --model FL_global_model.pt --data LOCAL_TEST_DIR [--out result.json]

SITE_DIR holds one site's ``test.npz`` (``prepare_data.py --layout single``). LOCAL_TEST_DIR
holds ``fl-client-1/``, ``fl-client-2/``, ... (``prepare_data.py --layout local-test``); then
every site is scored, and all test cases together ("pooled").

The model file is the ``FL_global_model.pt`` that SwarmMed saves at every site after the last
round (download it from the job's results in the web interface, or with ``swarmed results
export``). Score the final model: ``best_FL_global_model.pt`` is chosen by the validation
accuracy that sites report, not by AUROC.

Reported per site: AUROC with a 95% bootstrap confidence interval (percentile method, fixed
seed), AUPRC, and accuracy, sensitivity and specificity at a probability threshold of 0.5,
plus n and the number of positives. Fewer than 5 cases of a class make the AUROC and its
interval uninformative; the script then adds a warning. The pooled AUROC mixes sites with
different prevalences (36% to 94%), so the per-site values are the ones to compare.

Needs numpy, torch and scikit-learn, and training.py next to this file (for the model and the
data checks); NVIDIA FLARE is not needed.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import roc_auc_score

MIN_CLASS_CASES = 5


def load_training_module():
    """Import training.py from this folder under a unique name (nothing else is imported)."""
    path = Path(__file__).resolve().parent / "training.py"
    # No __pycache__ in the template folder: SwarmMed uploads every file in it as code.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(
        "tabular_heart_disease_training", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_global_model(path: Path, training):
    """Load FL_global_model.pt (NVFlare format {"model": state_dict}) or a plain state dict."""
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    state = (
        checkpoint.get("model", checkpoint) if isinstance(checkpoint, dict) else None
    )
    if not isinstance(state, dict) or not state:
        sys.exit(f"{path} holds no PyTorch state dict.")
    model = training.HeartDiseaseMLP()
    # strict loading: a model file from another template fails here instead of scoring nonsense
    model.load_state_dict({key: torch.as_tensor(value) for key, value in state.items()})
    model.eval()
    return model


def find_sites(data_dir: Path, split: str) -> list[tuple[str, Path]]:
    """Return (site name, folder) for SITE_DIR or for every site folder in LOCAL_TEST_DIR."""
    if (data_dir / f"{split}.npz").is_file():
        return [(site_label(data_dir), data_dir)]
    folders = [
        p for p in data_dir.iterdir() if p.is_dir() and (p / f"{split}.npz").is_file()
    ]
    if not folders:
        sys.exit(f"No {split}.npz in {data_dir} or in its subfolders.")

    def natural(path: Path):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path.name)]

    return [(p.name, p) for p in sorted(folders, key=natural)]


def site_label(folder: Path) -> str:
    """Site name from site_info.json (written by prepare_data.py), else the folder name."""
    try:
        return json.loads((folder / "site_info.json").read_text())["site_name"]
    except (OSError, ValueError, KeyError):
        return folder.name


def source_site(folder: Path):
    try:
        return json.loads((folder / "site_info.json").read_text()).get("source_site")
    except (OSError, ValueError):
        return None


def bootstrap_auroc(y, prob, *, resamples: int, seed: int) -> dict:
    """Percentile 95% CI of the AUROC over bootstrap resamples that contain both classes."""
    if resamples <= 0 or len(np.unique(y)) < 2:
        return {
            "ci95": [math.nan, math.nan],
            "resamples": resamples,
            "used": 0,
            "seed": seed,
        }
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(resamples):
        index = rng.integers(0, len(y), len(y))
        if y[index].min() != y[index].max():
            values.append(roc_auc_score(y[index], prob[index]))
    low, high = np.percentile(values, [2.5, 97.5])
    return {
        "ci95": [float(low), float(high)],
        "resamples": resamples,
        "used": len(values),
        "seed": seed,
    }


def score(name, x, y, model, args, training) -> dict:
    """All reported numbers for one set of test cases."""
    loss = training.evaluate(model, x, y)["loss"]
    prob = training.predict_proba(model, x)
    metrics = training.binary_metrics(y, prob, threshold=args.threshold, loss=loss)
    n_neg = metrics["n"] - metrics["n_pos"]
    result = {
        **metrics,
        "n_neg": n_neg,
        "prevalence": metrics["n_pos"] / metrics["n"],
        "threshold": args.threshold,
        "auroc_bootstrap": bootstrap_auroc(
            y, prob, resamples=args.bootstrap, seed=args.seed
        ),
        "warnings": [],
    }
    for count, label in ((metrics["n_pos"], "positive"), (n_neg, "negative")):
        if count < MIN_CLASS_CASES:
            result["warnings"].append(
                f"{name}: only {count} {label} case(s) in the {args.split} split; "
                "the AUROC and its confidence interval are not interpretable."
            )
    return result


def without_nan(value):
    """Replace NaN (an undefined metric) by None, so the output is strict JSON."""
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {key: without_nan(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_nan(item) for item in value]
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Score a saved global model on sites' test splits."
    )
    parser.add_argument("--model", required=True, type=Path, help="FL_global_model.pt")
    parser.add_argument(
        "--data", required=True, type=Path, help="SITE_DIR or LOCAL_TEST_DIR"
    )
    parser.add_argument("--split", default="test", choices=["test", "val", "train"])
    parser.add_argument("--out", type=Path, help="write the JSON result here")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--bootstrap", type=int, default=2000, help="resamples")
    parser.add_argument("--seed", type=int, default=0, help="bootstrap seed")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    training = load_training_module()
    model = load_global_model(args.model, training)
    model_bytes = args.model.read_bytes()

    sites, all_x, all_y = {}, [], []
    for name, folder in find_sites(args.data, args.split):
        arrays = training.load_local_split(folder / f"{args.split}.npz")
        x, y = arrays["X"], arrays["y"].astype(np.int64)
        sites[name] = {
            "source_site": source_site(folder),
            **score(name, x, y, model, args, training),
        }
        all_x.append(x)
        all_y.append(y)

    result = {
        "template": "tabular_heart_disease",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": {
            "file": args.model.name,
            "sha256": hashlib.sha256(model_bytes).hexdigest(),
        },
        "data": str(args.data),
        "split": args.split,
        "sites": sites,
    }
    if len(sites) > 1:
        x, y = np.concatenate(all_x), np.concatenate(all_y)
        result["pooled"] = score("pooled", x, y, model, args, training)
        result["pooled"]["note"] = (
            "All sites' cases together; sites differ in prevalence, so compare per-site AUROC."
        )

    header = f"{'site':<12} {'n':>4} {'pos':>4}  {'AUROC (95% CI)':<22} {'AUPRC':>6} {'acc':>6} {'sens':>6} {'spec':>6}"
    print(
        f"Model {args.model} (sha256 {result['model']['sha256'][:16]}), split {args.split}"
    )
    print(header)
    rows = list(sites.items()) + (
        [("pooled", result["pooled"])] if "pooled" in result else []
    )
    for name, r in rows:
        low, high = r["auroc_bootstrap"]["ci95"]
        auroc = f"{r['auroc']:.3f} ({low:.3f}-{high:.3f})"
        print(
            f"{name:<12} {r['n']:>4} {r['n_pos']:>4}  {auroc:<22} {r['auprc']:>6.3f} "
            f"{r['accuracy']:>6.3f} {r['sensitivity']:>6.3f} {r['specificity']:>6.3f}"
        )
    for _, r in rows:
        for warning in r["warnings"]:
            print("Warning: " + warning)

    text = json.dumps(without_nan(result), indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
