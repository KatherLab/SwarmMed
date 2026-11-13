"""Result plots for the heart sound template, run by SwarmMedHub in its sandbox.

SwarmMedHub provides the `visualization` helper (save_plot, open, exists, listdir; at most
4 plots) with the project data and the job's result files (the final global model is
FL_global_model.pt somewhere under "results/"). The script loads that model,
scores the test split of every site folder it finds (or of the root-level files) and
plots, per site:
  1. ROC curves (recording level: a recording's score is the mean of its segments'
     probabilities);
  2. recording scores by class;
  3. AUROC with a 95% bootstrap interval (recordings resampled), and segment AUROC;
  4. sensitivity and specificity at a score of 0.5.
Recordings of all fl-client-k sites are also pooled; held-out sources
(holdout-training-x/, from prepare_data.py --holdout) are shown but not pooled.
"""

import io
import json

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn

MODEL_FILE = "FL_global_model.pt"
BATCH = 64  # small inference batches keep memory low (the sandbox allows 1 GB)
N_BOOTSTRAP = 1000
SEED = 0
COLOURS = {0: "#4C72B0", 1: "#DD8452"}  # normal, abnormal


class PCGNet(nn.Module):
    """Same architecture as PCGNet in training.py (keep the two identical)."""

    def __init__(self):
        """Build the layers."""
        super().__init__()

        def block(c_in, c_out, pool=True):
            layers = [
                nn.Conv2d(c_in, c_out, 3, padding=1),
                nn.GroupNorm(4, c_out),
                nn.ReLU(),
            ]
            return layers + [nn.MaxPool2d(2)] if pool else layers

        self.features = nn.Sequential(
            *block(1, 16), *block(16, 32), *block(32, 64), *block(64, 64, pool=False)
        )
        self.head = nn.Sequential(
            nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.3), nn.Linear(64, 1)
        )

    def forward(self, x):
        """Return one logit per segment."""
        return self.head(self.features(x)).squeeze(1)


def read_bytes(path):
    """Read a registered file completely."""
    with visualization.open(path, "rb") as handle:
        return handle.read()


def auroc(labels, scores):
    """Area under the ROC curve (ties averaged); nan with one class."""
    labels = np.asarray(labels).astype(bool)
    scores = np.asarray(scores, dtype=np.float64)
    n_pos = int(labels.sum())
    n_neg = labels.size - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    _, first, counts = np.unique(scores[order], return_index=True, return_counts=True)
    ranks = np.empty(scores.size)
    ranks[order] = np.repeat(first + (counts + 1) / 2.0, counts)
    return float((ranks[labels].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def roc_points(labels, scores):
    """False and true positive rates at every distinct score threshold."""
    order = np.argsort(-scores, kind="mergesort")
    y = labels[order].astype(bool)
    s = scores[order]
    last = np.r_[np.flatnonzero(np.diff(s)), y.size - 1]  # last index of each tie group
    tps, fps = np.cumsum(y)[last], np.cumsum(~y)[last]
    return np.r_[0.0, fps / max(fps[-1], 1)], np.r_[0.0, tps / max(tps[-1], 1)]


def bootstrap_ci(labels, scores):
    """95% percentile interval of the AUROC, recordings resampled with replacement."""
    if labels.min() == labels.max():
        return None
    rng = np.random.default_rng(SEED)
    values = []
    for _ in range(N_BOOTSTRAP):
        idx = rng.integers(0, labels.size, labels.size)
        if labels[idx].min() != labels[idx].max():
            values.append(auroc(labels[idx], scores[idx]))
    return tuple(np.percentile(values, [2.5, 97.5]))


def find_sites(keys):
    """Data folders with site_info.json and test.npz: '' (root) or 'name/'."""
    found = []
    for key in keys:
        if key.startswith("results/") or not key.endswith("site_info.json"):
            continue
        prefix = key[: -len("site_info.json")]
        if (prefix == "" or prefix.count("/") == 1) and prefix + "test.npz" in keys:
            found.append(prefix)

    def order(prefix):
        name = prefix.rstrip("/")
        tail = name.rsplit("-", 1)[-1]
        return (
            prefix.startswith("holdout-"),
            prefix != "",
            int(tail) if tail.isdigit() else 0,
            name,
        )

    return sorted(found, key=order)


def find_model():
    """Path of the final global model under results/, or None.

    The Hub lists the job's files as results/<participant>/.../FL_global_model.pt
    (the folder depth depends on how the results were fetched); every site holds the
    same final model, so the first match in sorted order is used. best_FL_global_model.pt
    is not used.
    """
    paths = [
        n
        for n in visualization.listdir("results")
        if n == MODEL_FILE or n.endswith("/" + MODEL_FILE)
    ]
    return ("results/" + sorted(paths)[0]) if paths else None


def load_state(path):
    """Read a checkpoint ({"model": state_dict} or a plain state dict) from the manifest."""
    data = torch.load(
        io.BytesIO(read_bytes(path)), map_location="cpu", weights_only=True
    )
    if isinstance(data, dict) and isinstance(data.get("model"), dict):
        data = data["model"]
    return data


@torch.no_grad()
def score_site(model, prefix):
    """Recording labels and scores, and the segment-level AUROC, on one site's test split."""
    info = json.loads(read_bytes(prefix + "site_info.json").decode("utf-8"))
    with np.load(io.BytesIO(read_bytes(prefix + "test.npz")), allow_pickle=False) as z:
        x, y, rec, rec_y = (
            z["x"],
            z["y"],
            z["rec"].astype(np.int64),
            z["rec_y"].astype(np.int64),
        )
    mean = np.asarray(info["normalisation"]["mean"], dtype=np.float32)[None, :, None]
    std = np.asarray(info["normalisation"]["std"], dtype=np.float32)[None, :, None]
    probs = []
    for i in range(0, len(x), BATCH):
        xb = torch.from_numpy((x[i : i + BATCH].astype(np.float32) - mean) / std)[
            :, None
        ]
        probs.append(torch.sigmoid(model(xb)).numpy())
    seg_prob = np.concatenate(probs).astype(np.float64)
    rec_prob = np.bincount(rec, weights=seg_prob, minlength=rec_y.size) / np.bincount(
        rec, minlength=rec_y.size
    )
    return {
        "source": info.get("source", "?"),
        "labels": rec_y,
        "scores": rec_prob,
        "auroc_seg": auroc(y, seg_prob),
    }


def summarise(labels, scores):
    """AUROC with interval and threshold metrics for one set of recordings."""
    pred = scores >= 0.5
    pos, neg = labels == 1, labels == 0
    return {
        "n": int(labels.size),
        "abnormal": int(pos.sum()),
        "auroc": auroc(labels, scores),
        "ci": bootstrap_ci(labels, scores),
        "sensitivity": float(pred[pos].mean()) if pos.any() else float("nan"),
        "specificity": float((~pred[neg]).mean()) if neg.any() else float("nan"),
    }


def main():
    """Score the final global model and save the four plots."""
    torch.set_num_threads(1)  # the sandbox has one CPU
    keys = set(visualization.listdir(""))
    sites = find_sites(keys)
    model_path = find_model()
    if not sites or model_path is None:
        print(
            f"Nothing to plot: data folders with test.npz {sites}; global model found: {model_path}"
        )
        return
    model = PCGNet()
    state = load_state(model_path)
    model.load_state_dict(
        {k: torch.as_tensor(v) for k, v in state.items()}
    )  # strict: same architecture
    model.eval()
    print(f"Loaded {model_path}")

    results, pooled_labels, pooled_scores = {}, [], []
    for prefix in sites:
        name = prefix.rstrip("/") or "site"
        s = score_site(model, prefix)
        label = f"{name} ({s['source']})" + (
            ", held out" if name.startswith("holdout-") else ""
        )
        results[label] = {
            **summarise(s["labels"], s["scores"]),
            "auroc_seg": s["auroc_seg"],
            "labels": s["labels"],
            "scores": s["scores"],
        }
        if not name.startswith("holdout-"):
            pooled_labels.append(s["labels"])
            pooled_scores.append(s["scores"])
    if len(pooled_labels) > 1:
        labels, scores = np.concatenate(pooled_labels), np.concatenate(pooled_scores)
        results["pooled (sites above)"] = {
            **summarise(labels, scores),
            "auroc_seg": float("nan"),
            "labels": labels,
            "scores": scores,
        }

    for label, r in results.items():
        ci = f"{r['ci'][0]:.3f}-{r['ci'][1]:.3f}" if r["ci"] else "-"
        print(
            f"{label}: n={r['n']} abnormal={r['abnormal']} AUROC={r['auroc']:.3f} (95% CI {ci}) "
            f"segment AUROC={r['auroc_seg']:.3f} sensitivity={r['sensitivity']:.2f} "
            f"specificity={r['specificity']:.2f}"
        )
    names = list(results)

    # 1. ROC curves
    plt.figure(figsize=(6.5, 6))
    for label, r in results.items():
        if r["labels"].min() == r["labels"].max():
            continue
        fpr, tpr = roc_points(r["labels"], r["scores"])
        style = "--" if label.startswith("pooled") else "-"
        plt.plot(
            fpr, tpr, style, lw=1.8, label=f"{label}: {r['auroc']:.3f} (n={r['n']})"
        )
    plt.plot([0, 1], [0, 1], color="grey", lw=0.8)
    plt.xlabel("false positive rate")
    plt.ylabel("true positive rate")
    plt.title("ROC on each site's test split (per recording)")
    plt.legend(fontsize=7, loc="lower right", title="AUROC", title_fontsize=8)
    visualization.save_plot("ROC curves per site")
    plt.close("all")

    # 2. Recording scores by class
    plt.figure(figsize=(max(6.0, 1.6 * len(names)), 4.5))
    rng = np.random.default_rng(SEED)
    for i, label in enumerate(names):
        r = results[label]
        for c in (0, 1):
            v = r["scores"][r["labels"] == c]
            plt.scatter(
                i + (c - 0.5) * 0.35 + rng.uniform(-0.1, 0.1, v.size),
                v,
                s=7,
                alpha=0.6,
                color=COLOURS[c],
                label=("normal", "abnormal")[c] if i == 0 else None,
            )
    plt.axhline(0.5, color="grey", lw=0.8, ls=":")
    plt.xticks(range(len(names)), [n.replace(" (", "\n(") for n in names], fontsize=7)
    plt.ylabel("recording score")
    plt.title("Recording scores on the test split by class")
    plt.legend(fontsize=8)
    visualization.save_plot("Recording scores by class")
    plt.close("all")

    # 3. AUROC with 95% bootstrap interval, and segment-level AUROC
    plt.figure(figsize=(max(6.0, 1.6 * len(names)), 4.5))
    pos = np.arange(len(names))
    auc = np.array([results[n]["auroc"] for n in names])
    low = np.array([results[n]["ci"][0] if results[n]["ci"] else np.nan for n in names])
    high = np.array(
        [results[n]["ci"][1] if results[n]["ci"] else np.nan for n in names]
    )
    plt.errorbar(
        pos,
        auc,
        yerr=[auc - low, high - auc],
        fmt="o",
        capsize=4,
        label="recording AUROC, 95% CI",
    )
    plt.scatter(
        pos + 0.15,
        [results[n]["auroc_seg"] for n in names],
        marker="x",
        label="segment AUROC",
    )
    plt.axhline(0.5, color="grey", lw=0.8, ls=":")
    plt.ylim(0, 1.02)
    plt.xticks(pos, [n.replace(" (", "\n(") for n in names], fontsize=7)
    plt.ylabel("AUROC")
    plt.title("Test AUROC per site")
    plt.legend(fontsize=8, loc="lower right")
    visualization.save_plot("Test AUROC per site")
    plt.close("all")

    # 4. Sensitivity and specificity at 0.5
    plt.figure(figsize=(max(6.0, 1.6 * len(names)), 4.5))
    plt.bar(
        pos - 0.2,
        [results[n]["sensitivity"] for n in names],
        width=0.4,
        color=COLOURS[1],
        label="sensitivity",
    )
    plt.bar(
        pos + 0.2,
        [results[n]["specificity"] for n in names],
        width=0.4,
        color=COLOURS[0],
        label="specificity",
    )
    plt.ylim(0, 1.18)
    plt.xticks(pos, [n.replace(" (", "\n(") for n in names], fontsize=7)
    plt.title("Sensitivity and specificity at a score of 0.5 (test split)")
    plt.legend(fontsize=8, ncol=2, loc="upper center")
    visualization.save_plot("Sensitivity and specificity")
    plt.close("all")


if __name__ == "__main__":
    main()
