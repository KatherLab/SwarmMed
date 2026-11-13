"""Result plots for the ecg_ptbxl template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script for a finished training job with a helper object
``visualization`` (``open``, ``listdir``, ``exists``, ``save_plot``; at most four plots). The
helper's manifest holds the project data (``fl-client-1/test.npz``, ...) and the job's result
files under ``results/`` (``results/fl-client-1/app_fl-client-1/FL_global_model.pt``, the
client logs ``results/fl-client-1/log.txt``, ...). The script

1. plots the validation AUROC of the received global model per round and site, read from the
   ``SWARMMED_METRIC`` lines in the client logs (confusion matrices instead if no log is found),
2. scores the final global model (``FL_global_model.pt``) on every site's test split and plots
   the ROC curves per site and pooled,
3. plots the test AUROC per site and pooled with a 95% bootstrap confidence interval (2,000
   resamples of records, seed 0, as evaluate.py),
4. plots, per site and diagnostic superclass, the share of test records the model calls
   abnormal (probability >= 0.5): for NORM this is the false-positive rate, for the others the
   sensitivity.

The model class and the normalisation below must match training.py; the weights are loaded
strictly, so a mismatch stops the script with an error instead of plotting a half-loaded model.
AUROC and ROC points are computed with numpy (no scikit-learn import), and the test records are
scored one site at a time in small batches, which keeps the run within the sandbox's 1 GB of
memory.
"""

import contextlib
import io
import re

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch import nn

N_LEADS = 12
THRESHOLD = 0.5
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0
MIN_CLASS_CASES = 5
SUPERCLASSES = ["NORM", "MI", "STTC", "CD", "HYP"]
SITE_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
POOLED_COLOUR = "#0b0b0b"
INK, INK_2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

plt.rcParams.update(
    {
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.8,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
    }
)


def conv_block(c_in, c_out, kernel):
    return nn.Sequential(
        nn.Conv1d(c_in, c_out, kernel, padding=kernel // 2, bias=False),
        nn.BatchNorm1d(c_out),
        nn.ReLU(),
        nn.MaxPool1d(2),
    )


class ECGNet(nn.Module):
    """Same layers and names as training.py: four Conv1d blocks, global pooling, linear head."""

    def __init__(self, n_leads=N_LEADS, n_outputs=1):
        super().__init__()
        self.features = nn.Sequential(
            conv_block(n_leads, 32, 7),
            conv_block(32, 64, 7),
            conv_block(64, 64, 5),
            conv_block(64, 128, 5),
        )
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.head = nn.Linear(128, n_outputs)

    def forward(self, x):
        return self.head(self.pool(self.features(x)).flatten(1))


def normalise_ecg(X):
    """int16 microvolts (N, 12, 1000) -> float32 millivolts minus each lead's median (as training.py)."""
    x = torch.from_numpy(np.asarray(X).astype(np.float32) / 1000.0)
    return x - x.median(dim=-1, keepdim=True).values


# ----------------------------------------------------------------------------- metrics


def average_ranks(values):
    """1-based ranks, ties sharing their average rank."""
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    starts = np.concatenate(([0], np.flatnonzero(np.diff(sorted_values)) + 1))
    ends = np.concatenate((starts[1:], [len(values)]))
    ranks = np.empty(len(values))
    for start, end in zip(starts, ends, strict=True):
        ranks[order[start:end]] = (start + end + 1) / 2.0
    return ranks


def auroc(y, score):
    """Area under the ROC curve (Mann-Whitney statistic; ties count one half)."""
    y = np.asarray(y).astype(bool)
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = average_ranks(np.asarray(score, dtype=float))
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def roc_points(y, score):
    """False and true positive rates at every distinct score threshold, from (0, 0)."""
    y = np.asarray(y).astype(int)
    order = np.argsort(-np.asarray(score, dtype=float), kind="mergesort")
    sorted_scores, sorted_y = np.asarray(score)[order], y[order]
    last = np.concatenate((np.flatnonzero(np.diff(sorted_scores)), [len(y) - 1]))
    true_pos = np.cumsum(sorted_y)[last]
    false_pos = last + 1 - true_pos
    return (
        np.concatenate(([0.0], false_pos / max(len(y) - y.sum(), 1))),
        np.concatenate(([0.0], true_pos / max(y.sum(), 1))),
    )


def bootstrap_ci(y, prob):
    """Percentile 95% CI of the AUROC over resamples with both classes (as evaluate.py)."""
    if len(np.unique(y)) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    values = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        index = rng.integers(0, len(y), len(y))
        if y[index].min() != y[index].max():
            values.append(auroc(y[index], prob[index]))
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


# ----------------------------------------------------------------------------- inputs


def natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def site_colour(index):
    return SITE_COLOURS[index] if index < len(SITE_COLOURS) else MUTED


def read_bytes(path):
    with visualization.open(path, "rb") as handle:
        return handle.read()


def find_global_model(keys):
    """Key of the final global model: FL_global_model.pt (not best_...), first site first."""
    candidates = [
        k
        for k in keys
        if k.startswith("results/") and k.split("/")[-1] == "FL_global_model.pt"
    ]
    if not candidates:
        raise FileNotFoundError(
            "No FL_global_model.pt under results/. Has the job finished and have its "
            "results been synced?"
        )
    return sorted(candidates, key=natural_key)[0]


def find_test_sites(keys):
    folders = {
        k.split("/")[0]
        for k in keys
        if k.count("/") == 1
        and k.endswith("/test.npz")
        and not k.startswith("results/")
    }
    if folders:
        return {f: f + "/" for f in sorted(folders, key=natural_key)}
    return {"data root": ""} if "test.npz" in keys else {}


def read_metric_lines(keys):
    """{site: {round: {metric: value}}} from SWARMMED_METRIC lines in results/<site>/log.txt."""
    curves = {}
    for key in sorted(keys, key=natural_key):
        if not re.fullmatch(r"results/[^/]+/log\.txt", key):
            continue
        text = read_bytes(key).decode("utf-8", errors="replace")
        for line in text.splitlines():
            if "SWARMMED_METRIC " not in line:
                continue
            fields = dict(
                re.findall(r"(\w+)=(\S+)", line.split("SWARMMED_METRIC ", 1)[1])
            )
            if (
                fields.get("split") != "val"
                or "site" not in fields
                or "round" not in fields
            ):
                continue
            values = {}
            for name, value in fields.items():
                # site=... and split=... are not numbers
                with contextlib.suppress(ValueError):
                    values[name] = float(value)
            curves.setdefault(fields["site"], {})[int(fields["round"])] = values
    return curves


def load_model(key):
    checkpoint = torch.load(
        io.BytesIO(read_bytes(key)), map_location="cpu", weights_only=True
    )
    state = (
        checkpoint.get("model", checkpoint)
        if isinstance(checkpoint, dict)
        else checkpoint
    )
    model = ECGNet()
    model.load_state_dict(
        {name: torch.as_tensor(value) for name, value in state.items()}
    )
    model.eval()
    return model


@torch.no_grad()
def predict(model, X, batch_size=128):
    """Probabilities of the abnormal class for int16 ECGs (N, 12, 1000)."""
    out = []
    for start in range(0, len(X), batch_size):
        out.append(
            torch.sigmoid(model(normalise_ecg(X[start : start + batch_size]))).squeeze(
                1
            )
        )
    return torch.cat(out).numpy().astype(float)


# ----------------------------------------------------------------------------- plots


def main():
    torch.set_num_threads(1)
    keys = set(visualization.listdir(""))
    model_key = find_global_model(keys)
    model = load_model(model_key)
    print(f"Final global model: {model_key}")

    sites = {}
    for label, prefix in find_test_sites(keys).items():
        with np.load(
            io.BytesIO(read_bytes(f"{prefix}test.npz")), allow_pickle=False
        ) as npz:
            X, y, y_super = npz["X"], npz["y"].astype(int), npz["y_super"].astype(int)
        prob = predict(model, X)
        del X
        sites[label] = {
            "y": y,
            "y_super": y_super,
            "prob": prob,
            "auroc": auroc(y, prob),
            "ci": bootstrap_ci(y, prob),
            "few": min(int(y.sum()), int(len(y) - y.sum())) < MIN_CLASS_CASES,
        }
    if not sites:
        raise FileNotFoundError("No test.npz in the project data; run prepare_data.py.")
    labels = list(sites)
    if len(labels) > 1:
        y_pool = np.concatenate([sites[s]["y"] for s in labels])
        p_pool = np.concatenate([sites[s]["prob"] for s in labels])
        pooled = {
            "y": y_pool,
            "y_super": np.concatenate([sites[s]["y_super"] for s in labels]),
            "prob": p_pool,
            "auroc": auroc(y_pool, p_pool),
            "ci": bootstrap_ci(y_pool, p_pool),
            "few": False,
        }
    else:
        pooled = None
    print(f"{'site':<14} {'n':>5} {'abn':>5}  AUROC (95% CI)")
    for label in [*labels, *(["pooled"] if pooled else [])]:
        s = pooled if label == "pooled" else sites[label]
        print(
            f"{label:<14} {len(s['y']):>5} {int(s['y'].sum()):>5}  "
            f"{s['auroc']:.3f} ({s['ci'][0]:.3f}-{s['ci'][1]:.3f})"
        )

    # 1. Validation AUROC per round (from the client logs), else confusion matrices
    curves = read_metric_lines(keys)
    if curves:
        fig, ax = plt.subplots(figsize=(8.5, 4.8))
        names = sorted(curves, key=natural_key)
        for i, site in enumerate(names):
            rounds = sorted(curves[site])
            values = [curves[site][r].get("val_auroc", np.nan) for r in rounds]
            colour = site_colour(labels.index(site) if site in labels else i)
            ax.plot(
                rounds,
                values,
                color=colour,
                linewidth=2,
                marker="o",
                markersize=4,
                label=site,
            )
        ax.set_xlabel("round (round 0: untrained initial model)")
        ax.set_ylabel("validation AUROC")
        ax.xaxis.get_major_locator().set_params(integer=True)
        ax.yaxis.grid(True)
        ax.set_axisbelow(True)
        ax.legend(frameon=False, loc="lower right", fontsize=8)
        ax.set_title(
            "Validation AUROC of the received global model per round",
            loc="left",
            fontsize=11,
        )
        fig.tight_layout()
        visualization.save_plot("Validation AUROC per round")
    else:
        print(
            "No SWARMMED_METRIC lines in results/*/log.txt; plotting confusion matrices."
        )
        fig, axes = plt.subplots(
            1, len(labels), figsize=(3 * len(labels), 3.4), squeeze=False
        )
        for ax, label in zip(axes[0], labels, strict=True):
            s = sites[label]
            predicted = (s["prob"] >= THRESHOLD).astype(int)
            matrix = np.array(
                [
                    [int(((s["y"] == t) & (predicted == p)).sum()) for p in (0, 1)]
                    for t in (0, 1)
                ]
            )
            ax.imshow(matrix, cmap="Blues", vmin=0)
            for (i, j), count in np.ndenumerate(matrix):
                ax.text(
                    j,
                    i,
                    str(count),
                    ha="center",
                    va="center",
                    color="#ffffff" if count > matrix.max() / 2 else INK,
                )
            ax.set_xticks([0, 1], ["pred. normal", "pred. abnormal"], fontsize=8)
            ax.set_yticks([0, 1], ["normal", "abnormal"])
            ax.set_title(label, fontsize=9, loc="left")
        fig.suptitle(
            f"Confusion matrices on the test split (threshold {THRESHOLD})",
            x=0.01,
            ha="left",
            fontsize=11,
        )
        fig.tight_layout()
        visualization.save_plot("Confusion matrices")
    plt.close("all")

    # 2. ROC curves on the test split
    fig, ax = plt.subplots(figsize=(6.2, 7.0))
    ax.plot([0, 1], [0, 1], color=AXIS, linewidth=0.8)
    for i, label in enumerate(labels):
        s = sites[label]
        if np.isnan(s["auroc"]):
            continue
        fpr, tpr = roc_points(s["y"], s["prob"])
        ax.plot(
            fpr,
            tpr,
            color=site_colour(i),
            linewidth=1.6,
            label=f"{label}: AUROC {s['auroc']:.3f} (n={len(s['y'])})",
        )
    if pooled:
        fpr, tpr = roc_points(pooled["y"], pooled["prob"])
        ax.plot(
            fpr,
            tpr,
            color=POOLED_COLOUR,
            linewidth=2.4,
            label=f"pooled: AUROC {pooled['auroc']:.3f} (n={len(pooled['y'])})",
        )
    ax.set_xlabel("1 - specificity")
    ax.set_ylabel("sensitivity")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.set_aspect("equal")
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0.0, -0.12), fontsize=8)
    ax.set_title(
        "ROC curves of the final global model (test split)", loc="left", fontsize=11
    )
    fig.tight_layout()
    visualization.save_plot("ROC curves on the test split")
    plt.close("all")

    # 3. Test AUROC per site and pooled with 95% bootstrap CI (numbers in the row labels)
    rows = [(label, sites[label], site_colour(i)) for i, label in enumerate(labels)]
    if pooled:
        rows.append(("pooled", pooled, POOLED_COLOUR))
    fig, ax = plt.subplots(figsize=(8, 0.8 * len(rows) + 1.4))
    row_labels = []
    for row, (label, s, colour) in enumerate(rows):
        y_pos = len(rows) - 1 - row
        low, high = s["ci"]
        ax.plot([low, high], [y_pos, y_pos], color=colour, linewidth=2)
        ax.plot(
            s["auroc"],
            y_pos,
            "o",
            color=colour,
            markersize=8,
            markeredgecolor="#fcfcfb",
            markeredgewidth=2,
        )
        row_labels.append(
            f"{label}\n{s['auroc']:.3f} ({low:.3f}-{high:.3f}), n={len(s['y'])}, {int(s['y'].sum())} abnormal"
        )
    ax.axvline(0.5, color=AXIS, linewidth=0.8)
    ax.set_yticks(range(len(rows)), list(reversed(row_labels)), fontsize=8)
    lows = [s["ci"][0] for _, s, _ in rows if not np.isnan(s["ci"][0])]
    ax.set_xlim(max(0.0, min(lows, default=0.5) - 0.05), 1.0)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_xlabel("AUROC on the test split (95% bootstrap CI)")
    ax.xaxis.grid(True)
    ax.set_axisbelow(True)
    ax.set_title(
        "Test AUROC of the final global model per site", loc="left", fontsize=11
    )
    fig.tight_layout()
    visualization.save_plot("Test AUROC per site")
    plt.close("all")

    # 4. Share of test records called abnormal, per site and superclass
    names = [*labels, *(["pooled"] if pooled else [])]
    share = np.full((len(names), len(SUPERCLASSES)), np.nan)
    counts = np.zeros((len(names), len(SUPERCLASSES)), dtype=int)
    for i, label in enumerate(names):
        s = pooled if label == "pooled" else sites[label]
        called = s["prob"] >= THRESHOLD
        for j in range(len(SUPERCLASSES)):
            member = s["y_super"][:, j] == 1
            counts[i, j] = int(member.sum())
            if member.any():
                share[i, j] = called[member].mean() * 100
    fig, ax = plt.subplots(figsize=(8, 0.7 * len(names) + 2.0))
    image = ax.imshow(
        np.nan_to_num(share), cmap="Blues", vmin=0, vmax=100, aspect="auto"
    )
    for (i, j), value in np.ndenumerate(share):
        text = "n/a" if np.isnan(value) else f"{value:.0f}%\n(n={counts[i, j]})"
        ax.text(
            j,
            i,
            text,
            ha="center",
            va="center",
            fontsize=8,
            color="#ffffff" if value > 55 else INK,
        )
    ax.set_xticks(
        range(len(SUPERCLASSES)),
        [f"{c}\n(false positives)" if c == "NORM" else c for c in SUPERCLASSES],
    )
    ax.set_yticks(range(len(names)), names)
    ax.spines[:].set_visible(False)
    fig.colorbar(image, ax=ax, label="% called abnormal", fraction=0.04, pad=0.02)
    ax.set_title(
        f"Test records called abnormal (probability >= {THRESHOLD}), by superclass",
        loc="left",
        fontsize=11,
    )
    fig.tight_layout()
    visualization.save_plot("Called abnormal by superclass")
    plt.close("all")


try:
    main()
finally:
    plt.close("all")
