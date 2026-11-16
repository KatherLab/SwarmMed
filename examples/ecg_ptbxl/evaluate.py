#!/usr/bin/env python3
"""Score a saved global model of the ecg_ptbxl template on a site's test split; write JSON.

Run it at a site, from the template folder: it imports the model and the normalisation from
training.py next to it, so training and evaluation use the same code.

    python evaluate.py --checkpoint FL_global_model.pt --data DIR [--split test] [--out result.json]

--checkpoint: the global model a run leaves at every site
(<job>/app_<site>/FL_global_model.pt, or exported with `swarmed results export`), or a plain
PyTorch state dict. --data: one site's folder (holding test.npz), or a folder of site folders
(fl-client-1/, fl-client-2/, ...), in which case every site is scored and all sites are also
pooled.

Reported per site and pooled: AUROC with a 95% percentile bootstrap interval (records resampled
with replacement), accuracy, sensitivity and specificity at probability 0.5, mean BCE loss, and
for each abnormal superclass the share of its records the model calls abnormal. The JSON goes to
stdout and, with --out, to a file; a short summary goes to stderr.
"""

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import numpy as np
import sklearn
import torch
from sklearn.metrics import roc_auc_score

HERE = Path(__file__).resolve().parent
TEMPLATE = "ecg_ptbxl"
SUPERCLASSES = ("NORM", "MI", "STTC", "CD", "HYP")


def load_training_module():
    """Import training.py from this folder under a name that cannot clash with other modules."""
    # No __pycache__ in the template folder: SwarmMed uploads every file in it as code.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(
        f"{TEMPLATE}_training", HERE / "training.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_state_dict(path):
    """State dict from an NVFlare checkpoint ({'model': state_dict, ...}) or a plain state dict."""
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("model"), dict):
        checkpoint = checkpoint["model"]
    return {key: torch.as_tensor(value) for key, value in checkpoint.items()}


def natural_key(name):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def find_sites(data_dir, split):
    """[(site name, folder)] for one site folder or a folder of site folders."""
    if (data_dir / f"{split}.npz").is_file():
        return [(data_dir.name, data_dir)]
    sites = [
        p for p in data_dir.iterdir() if p.is_dir() and (p / f"{split}.npz").is_file()
    ]
    if not sites:
        raise SystemExit(f"no {split}.npz in {data_dir} or in its subfolders")
    return [(p.name, p) for p in sorted(sites, key=lambda p: natural_key(p.name))]


def bootstrap_auroc(truth, prob, replicates, seed):
    """95% percentile interval of the AUROC; resamples with one class only are skipped."""
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(replicates):
        index = rng.integers(0, len(truth), len(truth))
        if truth[index].min() != truth[index].max():
            values.append(roc_auc_score(truth[index], prob[index]))
    if not values:
        return None, 0
    low, high = np.percentile(values, [2.5, 97.5])
    return [round(float(low), 4), round(float(high), 4)], len(values)


def score(truth, logits, y_super, threshold, replicates, seed):
    """Metrics of one set of records (truth 0/1, logits, multi-hot superclasses)."""
    prob = 1.0 / (1.0 + np.exp(-logits))
    abnormal = truth == 1
    called = prob >= threshold
    result = {
        "n": int(len(truth)),
        "n_abnormal": int(abnormal.sum()),
        "prevalence": round(float(abnormal.mean()), 4),
        "auroc": None,
        "auroc_ci95": None,
        "bootstrap_replicates_used": 0,
        "accuracy": round(float((called == abnormal).mean()), 4),
        "sensitivity": round(float(called[abnormal].mean()), 4)
        if abnormal.any()
        else None,
        "specificity": round(float((~called[~abnormal]).mean()), 4)
        if (~abnormal).any()
        else None,
        "loss": round(
            float(
                torch.nn.functional.binary_cross_entropy_with_logits(
                    torch.from_numpy(logits), torch.from_numpy(truth.astype(np.float32))
                )
            ),
            4,
        ),
    }
    if abnormal.any() and (~abnormal).any():
        result["auroc"] = round(float(roc_auc_score(truth, prob)), 4)
        result["auroc_ci95"], result["bootstrap_replicates_used"] = bootstrap_auroc(
            truth, prob, replicates, seed
        )
    # Share of records with each abnormal superclass that the model calls abnormal.
    result["called_abnormal_by_superclass"] = {
        name: {
            "n": int(y_super[:, k].sum()),
            "share": round(float(called[y_super[:, k] == 1].mean()), 4),
        }
        for k, name in enumerate(SUPERCLASSES)
        if name != "NORM" and y_super[:, k].any()
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score an ecg_ptbxl global model on test data."
    )
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument(
        "--data", required=True, type=Path, help="site folder or folder of site folders"
    )
    parser.add_argument("--split", default="test", choices=("train", "val", "test"))
    parser.add_argument("--out", type=Path, help="also write the JSON to this file")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=2000,
        help="bootstrap replicates (default 2000)",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)

    torch.set_num_threads(args.threads)
    training = load_training_module()
    model = training.build_model()
    model.load_state_dict(
        load_state_dict(args.checkpoint)
    )  # strict: the architecture must match
    model.eval()

    results, pooled = {}, {"truth": [], "logits": [], "y_super": []}
    for name, folder in find_sites(args.data.resolve(), args.split):
        fs = training.LocalDataFolder(folder)
        arrays = training.read_npz(fs, f"{args.split}.npz", training.ARRAYS)
        training.check_arrays(arrays, f"{name}/{args.split}.npz")
        x, _ = training.to_tensors(arrays)
        logits = training.predict_logits(model, x).squeeze(1).numpy().astype(np.float64)
        truth = arrays["y"].astype(np.int64)
        results[name] = score(
            truth, logits, arrays["y_super"], args.threshold, args.bootstrap, args.seed
        )
        pooled["truth"].append(truth)
        pooled["logits"].append(logits)
        pooled["y_super"].append(arrays["y_super"])

    report = {
        "template": TEMPLATE,
        "created": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checkpoint": {
            "file": args.checkpoint.name,
            "sha256": hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(),
        },
        "split": args.split,
        "threshold": args.threshold,
        "bootstrap": {
            "replicates": args.bootstrap,
            "seed": args.seed,
            "method": "percentile interval; records resampled with replacement (pooled: over all records)",
        },
        "sites": results,
    }
    if len(results) > 1:
        report["pooled"] = score(
            np.concatenate(pooled["truth"]),
            np.concatenate(pooled["logits"]),
            np.concatenate(pooled["y_super"]),
            args.threshold,
            args.bootstrap,
            args.seed,
        )
    report["versions"] = {
        "torch": torch.__version__,
        "numpy": np.__version__,
        "scikit-learn": sklearn.__version__,
    }

    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    for name, values in list(results.items()) + (
        [("pooled", report["pooled"])] if "pooled" in report else []
    ):
        print(
            f"{name}: n={values['n']} AUROC={values['auroc']} 95% CI={values['auroc_ci95']} "
            f"accuracy={values['accuracy']} sensitivity={values['sensitivity']} "
            f"specificity={values['specificity']}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
