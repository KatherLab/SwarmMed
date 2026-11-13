#!/usr/bin/env python3
"""Score a saved model of the ehr_eicu_mortality template on prepared test splits.

Usage:
    python evaluate.py --model FL_global_model.pt --data DIR [--split test] [--out result.json]

--model  a checkpoint of the global model: the file SwarmMed writes at the end of the training
         (FL_global_model.pt, a dict whose "model" entry is the state dict) or a plain state
         dict. Score the final model, not best_FL_global_model.pt: the platform picks the "best"
         model by validation accuracy, which is about 0.9 at a 9 % death rate whatever the
         ranking quality.
--data   one site's folder (holding test.npz), e.g. the folder a site prepared with
         prepare_data.py --layout single, or a folder of site folders (fl-client-1/, ...), e.g.
         the --layout local-test output. With several sites the pooled cases are scored too.

Reported per site and pooled: AUROC with a 95 % bootstrap confidence interval (2,000 case
resamples, fixed seed), AUPRC, and accuracy, sensitivity and specificity at a threshold of 0.5,
with n and the number of deaths; a warning when a class has fewer than 10 cases. As a reference
line, the AUROC of the APACHE IVa predicted hospital mortality stored by prepare_data.py on the
cases that have it (APACHE IVa uses more variables, including the admission diagnosis, so it is
not a like-for-like comparison).

Only numpy, scikit-learn and torch are needed; training.py (next to this file) provides the
model. Nothing is written unless --out is given.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import average_precision_score, roc_auc_score

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 42
THRESHOLD = 0.5
MIN_CLASS_CASES = 10  # below this many deaths (or survivors) the AUROC is unstable


def load_training_module():
    """Import training.py from this folder under a name no installed package uses."""
    path = Path(__file__).resolve().parent / "training.py"
    # No __pycache__ in the template folder: SwarmMed uploads every file in it as code.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("ehr_eicu_mortality_training", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_model(path: Path, training):
    """Build the template's model and load the checkpoint's weights (all keys must match)."""
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    state = checkpoint
    if isinstance(checkpoint, dict):
        for key in ("model", "state_dict"):
            if isinstance(checkpoint.get(key), dict):
                state = checkpoint[key]
                break
    if not isinstance(state, dict) or not state:
        sys.exit(f"{path} holds no state dict.")
    state = {k: torch.as_tensor(v).float() for k, v in state.items()}
    model = training.MortalityMLP()
    expected = {k: tuple(v.shape) for k, v in model.state_dict().items()}
    found = {k: tuple(v.shape) for k, v in state.items()}
    if found != expected:
        sys.exit(
            f"{path} does not fit this template's model.\n  expected {expected}\n  found    {found}"
        )
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


def find_sites(data_dir: Path, split: str) -> list[tuple[str, Path]]:
    """(name, folder) of every site to score: DIR itself or its fl-client-k subfolders."""
    if (data_dir / f"{split}.npz").is_file():
        return [(data_dir.name, data_dir)]
    sites = [
        p for p in data_dir.iterdir() if p.is_dir() and (p / f"{split}.npz").is_file()
    ]
    if not sites:
        sys.exit(f"No {split}.npz in {data_dir} or in its subfolders.")

    def order(path: Path):  # fl-client-2 before fl-client-10
        tail = path.name.rsplit("-", 1)[-1]
        return (0, int(tail), path.name) if tail.isdigit() else (1, 0, path.name)

    return [(p.name, p) for p in sorted(sites, key=order)]


def auroc_ci(y: np.ndarray, p: np.ndarray, rng: np.random.Generator) -> tuple:
    """Percentile 95 % CI of the AUROC over case resamples (resamples with one class skipped)."""
    values = []
    n = len(y)
    for _ in range(BOOTSTRAP_RESAMPLES):
        idx = rng.integers(0, n, n)
        if 0 < y[idx].sum() < n:
            values.append(roc_auc_score(y[idx], p[idx]))
    if not values:
        return None, None, 0
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high), len(values)


def score(y: np.ndarray, p: np.ndarray, seed: int) -> dict:
    """Metrics of scores p for labels y (with the bootstrap CI of the AUROC)."""
    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=float)
    n, n_pos = int(len(y)), int(y.sum())
    result = {"n": n, "n_pos": n_pos, "n_neg": n - n_pos}
    warnings = [
        f"only {count} {label} in this split: the AUROC and its interval are unstable"
        for count, label in ((n_pos, "deaths"), (n - n_pos, "survivors"))
        if count < MIN_CLASS_CASES
    ]
    if n_pos == 0 or n_pos == n:
        result.update(auroc=None, auroc_ci95=None, auprc=None)
        warnings.append("one class only: AUROC and AUPRC are undefined")
    else:
        low, high, valid = auroc_ci(y, p, np.random.default_rng(seed))
        result.update(
            auroc=float(roc_auc_score(y, p)),
            auroc_ci95=[low, high],
            bootstrap_valid_resamples=valid,
            auprc=float(average_precision_score(y, p)),
        )
    predicted = p >= THRESHOLD
    result.update(
        threshold=THRESHOLD,
        accuracy=float((predicted == (y == 1)).mean()) if n else None,
        sensitivity=float(predicted[y == 1].mean()) if n_pos else None,
        specificity=float((~predicted[y == 0]).mean()) if n - n_pos else None,
        warnings=warnings,
    )
    return result


def reference_score(y: np.ndarray, reference: np.ndarray, seed: int) -> dict:
    """AUROC of the APACHE IVa prediction on the cases that have one."""
    have = ~np.isnan(reference)
    result = score(y[have], reference[have], seed) if have.any() else {"n": 0}
    result["cases_without_score"] = int((~have).sum())
    for key in ("threshold", "accuracy", "sensitivity", "specificity"):
        result.pop(key, None)  # APACHE probabilities are not calibrated to this cohort
    return result


def fmt(value, digits=3) -> str:
    return "  -  " if value is None else f"{value:.{digits}f}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a saved ehr_eicu_mortality model on prepared test splits."
    )
    parser.add_argument(
        "--model", required=True, type=Path, help="FL_global_model.pt or a state dict"
    )
    parser.add_argument("--data", required=True, type=Path,
                        help="a site folder, or a folder of site folders")  # fmt: skip
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument(
        "--out", type=Path, help="write the results as JSON to this file"
    )
    parser.add_argument(
        "--seed", type=int, default=BOOTSTRAP_SEED, help="bootstrap seed"
    )
    args = parser.parse_args(argv)
    started = time.perf_counter()
    torch.set_num_threads(4)

    training = load_training_module()
    model_path = args.model.expanduser().resolve()
    model = load_model(model_path, training)
    data_dir = args.data.expanduser().resolve()

    sites, ys, ps, refs = {}, [], [], []
    for name, folder in find_sites(data_dir, args.split):
        arrays = training.load_npz((folder / f"{args.split}.npz").read_bytes(), name)
        y, p = arrays["y"], training.predict_proba(model, arrays["X"])
        entry = {"folder": str(folder)}
        info_path = folder / "site_info.json"
        if info_path.is_file():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            entry.update(
                site_name=info.get("site_name"), source_site=info.get("source_site")
            )
        case_ids = sorted(str(c) for c in arrays.get("case_id", []))
        if case_ids:
            digest = hashlib.sha256("\n".join(case_ids).encode()).hexdigest()[:16]
            entry["case_fingerprint"] = digest
        entry["model"] = score(y, p, args.seed)
        reference = arrays.get("ref_apache_iva")
        if reference is not None:
            reference = reference.astype(float)
            entry["reference_apache_iva"] = reference_score(y, reference, args.seed)
            refs.append(reference)
        sites[name] = entry
        ys.append(y)
        ps.append(p)

    result = {
        "template": "ehr_eicu_mortality",
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_path": str(model_path),
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "data_dir": str(data_dir),
        "split": args.split,
        "bootstrap": {
            "resamples": BOOTSTRAP_RESAMPLES,
            "seed": args.seed,
            "method": "percentile interval over case resamples; resamples with one class skipped",
        },
        "sites": sites,
    }
    if len(sites) > 1:
        y_all, p_all = np.concatenate(ys), np.concatenate(ps)
        result["pooled"] = {
            "sites": list(sites),
            "model": score(y_all, p_all, args.seed),
        }
        if len(refs) == len(sites):
            result["pooled"]["reference_apache_iva"] = reference_score(
                y_all, np.concatenate(refs), args.seed
            )

    rows = list(sites.items()) + (
        [("pooled", result["pooled"])] if "pooled" in result else []
    )
    print(f"{args.split} split, model {model_path.name}")
    print(f"{'site':<14} {'n':>4} {'deaths':>6} {'AUROC':>6} {'95% CI':>13} {'AUPRC':>6} "
          f"{'acc':>6} {'sens':>6} {'spec':>6} {'APACHE IVa AUROC (n)':>22}")  # fmt: skip
    for name, entry in rows:
        m = entry["model"]
        ci = m.get("auroc_ci95") or [None, None]
        ref = entry.get("reference_apache_iva", {})
        ref_text = f"{fmt(ref.get('auroc'))} ({ref.get('n', 0)})" if ref else "-"
        print(
            f"{name:<14} {m['n']:>4} {m['n_pos']:>6} {fmt(m.get('auroc')):>6} "
            f"{fmt(ci[0]):>6}-{fmt(ci[1]):<6} {fmt(m.get('auprc')):>6} {fmt(m['accuracy']):>6} "
            f"{fmt(m['sensitivity']):>6} {fmt(m['specificity']):>6} {ref_text:>22}"
        )
        for warning in m["warnings"]:
            print(f"{'':<14} warning: {warning}")
    print(f"({time.perf_counter() - started:.1f} s)")

    text = json.dumps(result, indent=2)
    if args.out:
        out = args.out.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
