"""Score a saved global model on a site's test split (EEG sleep staging).

Run at a site, next to training.py, after a swarm training run:

    python evaluate.py --model FL_global_model.pt --data /data/eeg-site-2
    python evaluate.py --model FL_global_model.pt --data /data/eeg-prepared \\
        --out eeg_test_metrics.json

--data is either one site's folder (holding test.npz) or a folder with one
subfolder per site (fl-client-1/, fl-client-2/, ...); then every site is scored
and the predictions of all sites are also pooled. Reported per site and pooled:
accuracy, macro-F1, Cohen's kappa, F1 per stage and the confusion matrix (rows:
true stage, columns: predicted stage). Each site's test split is one subject, so
per-site numbers vary a lot.

--bootstrap N adds a 95% confidence interval (2.5th and 97.5th percentile of N
resamples) for accuracy, macro-F1 and kappa. The 30-s epochs of a night are not
independent, so the resampling unit is a block of 20 consecutive epochs (10
minutes) of one night, not a single epoch. For a site with one test subject the
interval covers only the variation within that night, not between subjects; the
pooled interval (all sites) is the more informative one.

The model and metric code is loaded from training.py in this folder.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import confusion_matrix


def load_training_module():
    """Import training.py from this folder under a name that cannot clash.

    No bytecode is written, so the folder stays clean for use as a code bundle.
    """
    sys.dont_write_bytecode = True
    path = Path(__file__).resolve().with_name("training.py")
    spec = importlib.util.spec_from_file_location("eeg_sleep_staging_training", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_state_dict(path):
    """Read a state dict from FL_global_model.pt ({"model": ...}) or a plain one."""
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("model"), dict):
        checkpoint = checkpoint["model"]
    if not isinstance(checkpoint, dict):
        raise ValueError(f"{path}: no state dict found")
    return {key: torch.as_tensor(value) for key, value in checkpoint.items()}


def find_sites(data_dir, split):
    """Return {site name: split file} for one site folder or a folder of sites."""
    direct = data_dir / f"{split}.npz"
    if direct.is_file():
        name = data_dir.name
        info = data_dir / "site_info.json"
        if info.is_file():
            name = json.loads(info.read_text()).get("site", name)
        return {name: direct}
    sites = {
        child.name: child / f"{split}.npz"
        for child in sorted(data_dir.iterdir())
        if (child / f"{split}.npz").is_file()
    }
    if not sites:
        raise SystemExit(f"No {split}.npz in {data_dir} or in its subfolders")
    return sites


def score(training, y_true, y_pred):
    """Metrics plus the confusion matrix for one set of predictions."""
    metrics = training.sleep_metrics(y_true, y_pred)
    labels = list(range(len(training.CLASS_NAMES)))
    metrics["confusion_matrix"] = confusion_matrix(
        y_true, y_pred, labels=labels
    ).tolist()
    return metrics


BLOCK_EPOCHS = 20  # bootstrap unit: 10 minutes of one night


def bootstrap_intervals(training, y_true, y_pred, blocks, n_boot, seed=0):
    """95% percentile intervals of accuracy, macro-F1 and kappa, resampling blocks.

    Args:
        training: The training module (for sleep_metrics).
        y_true, y_pred: Class indices, one per epoch.
        blocks: One block label per epoch; blocks are resampled with replacement.
        n_boot: Number of resamples.
        seed: Seed of the resampling.

    Returns:
        dict: key -> [lower, upper], plus "n_blocks" and "resamples".
    """
    rng = np.random.default_rng(seed)
    _, block_index = np.unique(blocks, return_inverse=True)
    members = [np.flatnonzero(block_index == b) for b in range(block_index.max() + 1)]
    draws = {"accuracy": [], "macro_f1": [], "kappa": []}
    for _ in range(n_boot):
        pick = rng.integers(0, len(members), len(members))
        rows = np.concatenate([members[b] for b in pick])
        metrics = training.sleep_metrics(y_true[rows], y_pred[rows])
        for key in draws:
            draws[key].append(metrics[key])
    out = {
        key: [float(v) for v in np.percentile(values, [2.5, 97.5])]
        for key, values in draws.items()
    }
    out.update(n_blocks=len(members), resamples=n_boot, block_epochs=BLOCK_EPOCHS)
    return out


def main(argv=None):
    """Evaluate the model and print (and optionally write) the metrics as JSON."""
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model", required=True, type=Path, help="FL_global_model.pt")
    parser.add_argument(
        "--data", required=True, type=Path, help="Site folder or folder of site folders"
    )
    parser.add_argument("--split", default="test", choices=["test", "val", "train"])
    parser.add_argument("--out", type=Path, help="Also write the JSON to this file")
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=0,
        metavar="N",
        help="Add 95%% intervals from N block-bootstrap resamples (e.g. 1000)",
    )
    parser.add_argument("--threads", type=int, default=4, help="CPU threads")
    args = parser.parse_args(argv)

    training = load_training_module()
    torch.set_num_threads(args.threads)
    model = training.SleepStageCNN()
    model.load_state_dict(
        load_state_dict(args.model)
    )  # strict: architecture must match
    model.eval()

    result = {
        "template": "eeg_sleep_staging",
        "task": "5-class sleep staging of 30-s EEG epochs",
        "model": {
            "path": str(args.model),
            "sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
        },
        "split": args.split,
        "class_names": training.CLASS_NAMES,
        "sites": {},
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    pooled_true, pooled_pred, pooled_blocks = [], [], []
    for site, path in find_sites(args.data, args.split).items():
        arrays = training.load_split(path)
        x, y = training.to_tensors(arrays)
        y_pred = training.predict_logits(model, x).argmax(dim=1).numpy()
        y_true = y.numpy()
        metrics = score(training, y_true, y_pred)
        metrics["subjects"] = sorted({int(s) for s in arrays["subject"]})
        blocks = [
            f"{site}/{s}/{n}/{e // BLOCK_EPOCHS}"
            for s, n, e in zip(
                arrays["subject"], arrays["night"], arrays["epoch"], strict=True
            )
        ]
        if args.bootstrap:
            metrics["ci95"] = bootstrap_intervals(
                training, y_true, y_pred, np.array(blocks), args.bootstrap
            )
        pooled_blocks.extend(blocks)
        result["sites"][site] = metrics
        pooled_true.append(y_true)
        pooled_pred.append(y_pred)
    if len(result["sites"]) > 1:
        result["pooled"] = score(
            training, np.concatenate(pooled_true), np.concatenate(pooled_pred)
        )
        result["pooled"]["sites"] = list(result["sites"])
        if args.bootstrap:
            result["pooled"]["ci95"] = bootstrap_intervals(
                training,
                np.concatenate(pooled_true),
                np.concatenate(pooled_pred),
                np.array(pooled_blocks),
                args.bootstrap,
            )

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    for name, metrics in [*result["sites"].items(), ("pooled", result.get("pooled"))]:
        if metrics:
            print(
                f"{name}: n={metrics['n']} accuracy {metrics['accuracy']:.3f} "
                f"macro-F1 {metrics['macro_f1']:.3f} kappa {metrics['kappa']:.3f}",
                file=sys.stderr,
            )
            for key, (low, high) in (
                (k, v)
                for k, v in metrics.get("ci95", {}).items()
                if isinstance(v, list)
            ):
                print(f"    {key} 95% [{low:.3f}, {high:.3f}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
