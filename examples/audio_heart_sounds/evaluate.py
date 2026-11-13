#!/usr/bin/env python3
"""Score a saved global model on prepared heart sound data (site-side, outside the Hub).

Loads a checkpoint written by the swarm job (FL_global_model.pt: {"model": state_dict}),
or a plain state dict, into the model defined in training.py next to this script, and
scores it on one split of:

  * one site's folder (site_info.json and <split>.npz at its root), or
  * a folder of site folders (fl-client-1/, fl-client-2/, ... as written by
    prepare_data.py --layout local-test): every site is scored and the recordings of
    all fl-client-k sites are also pooled. Folders holdout-training-x/ (prepare_data.py
    --holdout) are scored separately and are not pooled.

Each site's data is normalised with that site's own statistics from site_info.json,
as in training. A recording's score is the mean of its segments' probabilities.
Reported per site: recording-level AUROC with a 95% bootstrap confidence interval
(percentile method, recordings resampled with replacement), segment-level AUROC, and
accuracy, sensitivity and specificity at a score of 0.5.

Example:
    python evaluate.py --checkpoint FL_global_model.pt --data-dir prepared/local-test --out eval.json
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import torch


def load_training_module():
    """Import training.py from this folder under a name that cannot shadow a package."""
    path = Path(__file__).resolve().parent / "training.py"
    # No __pycache__ in the template folder: SwarmMed uploads every file in it as code.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("audio_heart_sounds_training", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


T = load_training_module()


def file_md5(path: Path) -> str:
    """MD5 of a file (the Hub reports global models by MD5)."""
    digest = hashlib.md5(usedforsecurity=False)  # identifies the file, as the Hub does
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_model(checkpoint: Path):
    """Build the training.py model and load the checkpoint's weights (strict)."""
    data = torch.load(checkpoint, map_location="cpu", weights_only=True)
    state = data.get("model", data) if isinstance(data, dict) else None
    if not isinstance(state, dict):
        raise ValueError(
            f"{checkpoint}: expected a state dict or {{'model': state_dict}}"
        )
    model = T.PCGNet()
    model.load_state_dict({k: torch.as_tensor(v) for k, v in state.items()})
    model.eval()
    return model


def find_sites(data_dir: Path, split: str):
    """List (name, folder, pooled) for the site folders to score.

    data_dir itself if it holds one site's files, else its fl-client-k/ folders (in
    numeric order); then any holdout-*/ folders, which are not pooled.
    """

    def usable(folder):
        return (folder / "site_info.json").is_file() and (
            folder / f"{split}.npz"
        ).is_file()

    def client_number(folder):
        suffix = folder.name.rsplit("-", 1)[-1]
        return int(suffix) if suffix.isdigit() else 0

    folders = sorted(p for p in data_dir.iterdir() if p.is_dir() and usable(p))
    if usable(data_dir):
        sites = [(data_dir.resolve().name, data_dir, True)]
    else:
        clients = [p for p in folders if p.name.startswith("fl-client-")]
        sites = [(p.name, p, True) for p in sorted(clients, key=client_number)]
    sites += [(p.name, p, False) for p in folders if p.name.startswith("holdout-")]
    return sites


def bootstrap_auroc(labels, scores, n_boot, seed):
    """95% percentile interval of the AUROC over recordings resampled with replacement."""
    if n_boot <= 0 or labels.min() == labels.max():
        return None
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(n_boot):
        idx = rng.integers(0, labels.size, labels.size)
        if labels[idx].min() != labels[idx].max():  # skip resamples with one class
            values.append(T.auroc(labels[idx], scores[idx]))
    low, high = np.percentile(values, [2.5, 97.5])
    return {"low": float(low), "high": float(high), "resamples": len(values)}


def threshold_metrics(labels, scores, threshold=0.5):
    """Accuracy, sensitivity and specificity of score >= threshold."""
    pred = scores >= threshold
    pos, neg = labels == 1, labels == 0
    return {
        "accuracy": float(np.mean(pred == pos)),
        "sensitivity": float(pred[pos].mean()) if pos.any() else None,
        "specificity": float((~pred[neg]).mean()) if neg.any() else None,
    }


def score_site(model, folder: Path, split: str):
    """Recording-level labels and scores plus segment-level AUROC for one site."""
    info = json.loads((folder / "site_info.json").read_text())
    arrays = T.load_npz((folder / f"{split}.npz").read_bytes())
    data = T.prepare_split(arrays, info)
    seg_prob = T.predict_segments(model, data["x"])
    rec_prob = T.recording_scores(seg_prob, data["rec"], data["rec_y"].size)
    return {
        "source": info.get("source"),
        "rec_ids": [str(r) for r in data["rec_ids"]],
        "labels": data["rec_y"],
        "scores": rec_prob,
        "n_segments": np.bincount(data["rec"], minlength=data["rec_y"].size),
        "auroc_segment": T.auroc(data["y"].numpy(), seg_prob),
    }


def summarise(labels, scores, n_boot, seed):
    """Metrics for one set of recordings."""
    auc = T.auroc(labels, scores)
    return {
        "recordings": int(labels.size),
        "abnormal": int(labels.sum()),
        "auroc": None if np.isnan(auc) else auc,
        "auroc_ci95": bootstrap_auroc(labels, scores, n_boot, seed),
        **threshold_metrics(labels, scores),
    }


def fmt(value, digits=3):
    """Format a metric for the table (None -> '-')."""
    return "-" if value is None else f"{value:.{digits}f}"


def main(argv=None) -> int:
    """Score the checkpoint and print a table; write JSON (and optional predictions)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--checkpoint",
        required=True,
        type=Path,
        help="FL_global_model.pt or a state dict",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="one site's folder, or a folder of fl-client-k/ (and holdout-*) folders",
    )
    parser.add_argument("--split", default="test", choices=("train", "val", "test"))
    parser.add_argument(
        "--bootstrap", type=int, default=2000, help="bootstrap resamples (0: no CI)"
    )
    parser.add_argument("--seed", type=int, default=0, help="bootstrap seed")
    parser.add_argument("--threads", type=int, default=4, help="CPU threads for torch")
    parser.add_argument(
        "--out", type=Path, help="write the results as JSON here (default: print them)"
    )
    parser.add_argument(
        "--predictions", type=Path, help="also write per-recording scores as CSV here"
    )
    args = parser.parse_args(argv)

    torch.set_num_threads(args.threads)
    model = load_model(args.checkpoint)
    sites = find_sites(args.data_dir, args.split)
    if not sites:
        sys.exit(
            f"No folder with site_info.json and {args.split}.npz in or below {args.data_dir}"
        )

    results = {
        "checkpoint": str(args.checkpoint),
        "checkpoint_md5": file_md5(args.checkpoint),
        "split": args.split,
        "score": "recording score = mean of its segments' sigmoid probabilities",
        "threshold": 0.5,
        "bootstrap": {
            "resamples": args.bootstrap,
            "seed": args.seed,
            "method": "percentile, recordings resampled",
        },
        "sites": {},
        "holdout": {},
    }
    pooled_labels, pooled_scores, rows = [], [], []
    for name, folder, pooled in sites:
        s = score_site(model, folder, args.split)
        entry = {
            "source": s["source"],
            **summarise(s["labels"], s["scores"], args.bootstrap, args.seed),
        }
        entry["auroc_segment"] = (
            None if np.isnan(s["auroc_segment"]) else s["auroc_segment"]
        )
        results["sites" if pooled else "holdout"][name] = entry
        if pooled:
            pooled_labels.append(s["labels"])
            pooled_scores.append(s["scores"])
        rows += [
            (name, s["source"], r, int(y), float(p), int(n))
            for r, y, p, n in zip(
                s["rec_ids"], s["labels"], s["scores"], s["n_segments"], strict=True
            )
        ]
    if len(pooled_labels) > 1:
        results["pooled"] = summarise(
            np.concatenate(pooled_labels),
            np.concatenate(pooled_scores),
            args.bootstrap,
            args.seed,
        )
        results["pooled"]["sites"] = [n for n, _, p in sites if p]

    print(
        f"Checkpoint {args.checkpoint} (MD5 {results['checkpoint_md5']}), split {args.split}"
    )
    print(
        f"{'site':<22} {'source':<11} {'rec':>5} {'abn':>4} {'AUROC':>6} {'95% CI':>13} "
        f"{'segAUC':>6} {'acc':>5} {'sens':>5} {'spec':>5}"
    )
    table = [*results["sites"].items(), *results["holdout"].items()]
    if "pooled" in results:
        table.append(
            ("pooled", {"source": "-", "auroc_segment": None, **results["pooled"]})
        )
    for name, e in table:
        ci = e["auroc_ci95"]
        ci_text = f"{ci['low']:.3f}-{ci['high']:.3f}" if ci else "-"
        print(
            f"{name:<22} {e['source'] or '-':<11} {e['recordings']:>5} {e['abnormal']:>4} {fmt(e['auroc']):>6} "
            f"{ci_text:>13} {fmt(e['auroc_segment']):>6} {fmt(e['accuracy'], 2):>5} "
            f"{fmt(e['sensitivity'], 2):>5} {fmt(e['specificity'], 2):>5}"
        )

    if args.predictions:
        with open(args.predictions, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["site", "source", "record", "label", "score", "segments"])
            writer.writerows(rows)
    if args.out:
        args.out.write_text(json.dumps(results, indent=1) + "\n")
        print(f"Wrote {args.out}")
    else:
        print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
