"""Result plots for the tabular_heart_disease template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script for a finished training job with a helper object
``visualization`` (``open``, ``listdir``, ``exists``, ``save_plot``; at most four plots). The
helper's manifest holds the project data (``fl-client-1/test.npz``, ...) and the job's result
files under ``results/`` (``results/fl-client-1/app_fl-client-1/FL_global_model.pt``, the
client logs ``results/fl-client-1/log.txt``, ...). The script

1. plots the validation AUROC of the received global model per round and site, read from the
   ``SWARMMED_METRIC`` lines in the client logs (confusion matrices instead if no log is found),
2. scores the final global model (``FL_global_model.pt``) on every site's test split and
   plots the ROC curves,
3. plots the test AUROC per site with a 95% bootstrap confidence interval (2,000 resamples,
   seed 0, as evaluate.py),
4. plots the predicted probabilities by site and diagnosis.

The model class below must match training.py; the weights are loaded strictly, so a mismatch
stops the script with an error instead of plotting a half-loaded model. AUROC and ROC points
are computed with numpy (no scikit-learn import), which keeps the run within the sandbox's
1 GB of memory.
"""

import contextlib
import io
import json
import re

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch import nn

HIDDEN_UNITS = 32  # as in training.py
DROPOUT = 0.2
N_FEATURES = 15
THRESHOLD = 0.5
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0
MIN_CLASS_CASES = 5
SITE_NAMES = {
    "cleveland": "Cleveland",
    "hungarian": "Budapest",
    "va_long_beach": "Long Beach VA",
    "switzerland": "Switzerland",
}
SITE_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
CLASS_COLOURS = {0: "#6250d6", 1: "#e34948"}  # no disease, disease
CLASS_LABELS = {0: "no disease", 1: "disease"}
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


class HeartDiseaseMLP(nn.Module):
    """Same layers and names as in training.py: 15 -> 32 (ReLU, dropout) -> 1 logit."""

    def __init__(self, n_features=N_FEATURES):
        super().__init__()
        self.hidden = nn.Linear(n_features, HIDDEN_UNITS)
        self.dropout = nn.Dropout(DROPOUT)
        self.output = nn.Linear(HIDDEN_UNITS, 1)

    def forward(self, x):
        return self.output(self.dropout(torch.relu(self.hidden(x)))).squeeze(-1)


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


def load_model(key):
    checkpoint = torch.load(
        io.BytesIO(read_bytes(key)), map_location="cpu", weights_only=True
    )
    state = (
        checkpoint.get("model", checkpoint)
        if isinstance(checkpoint, dict)
        else checkpoint
    )
    model = HeartDiseaseMLP()
    model.load_state_dict(
        {name: torch.as_tensor(value) for name, value in state.items()}
    )
    model.eval()
    return model


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


def source_name(prefix, keys):
    path = f"{prefix}site_info.json"
    if path not in keys:
        return None
    info = json.loads(read_bytes(path).decode("utf-8"))
    return SITE_NAMES.get(info.get("source_site"), info.get("source_site"))


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


def spread_labels(values, gap):
    """Shift label positions apart so that neighbours are at least `gap` apart."""
    order = np.argsort(values)
    placed = np.array(values, dtype=float)
    for previous, current in zip(order[:-1], order[1:], strict=True):
        if placed[current] - placed[previous] < gap:
            placed[current] = placed[previous] + gap
    return placed


# ----------------------------------------------------------------------------- plots


def main():
    keys = set(visualization.listdir(""))
    model_key = find_global_model(keys)
    model = load_model(model_key)
    print(f"Final global model: {model_key}")

    sites = {}
    for label, prefix in find_test_sites(keys).items():
        with np.load(
            io.BytesIO(read_bytes(f"{prefix}test.npz")), allow_pickle=False
        ) as npz:
            x, y = npz["X"], npz["y"].astype(int)
        with torch.no_grad():
            prob = torch.sigmoid(model(torch.as_tensor(x, dtype=torch.float32))).numpy()
        source = source_name(prefix, keys)
        sites[label] = {
            "y": y,
            "prob": prob.astype(float),
            "name": f"{label} ({source})" if source else label,
            "auroc": auroc(y, prob),
            "ci": bootstrap_ci(y, prob.astype(float)),
            "few": min(int(y.sum()), int(len(y) - y.sum())) < MIN_CLASS_CASES,
        }
    if not sites:
        raise FileNotFoundError("No test.npz in the project data; run prepare_data.py.")
    labels = list(sites)
    print(f"{'site':<28} {'n':>4} {'pos':>4}  AUROC (95% CI)")
    for label in labels:
        s = sites[label]
        print(
            f"{s['name']:<28} {len(s['y']):>4} {int(s['y'].sum()):>4}  "
            f"{s['auroc']:.3f} ({s['ci'][0]:.3f}-{s['ci'][1]:.3f})"
        )

    # 1. Validation AUROC per round (from the client logs), else confusion matrices
    curves = read_metric_lines(keys)
    if curves:
        fig, ax = plt.subplots(figsize=(8.5, 4.8))
        names = sorted(curves, key=natural_key)
        ends = []
        for i, site in enumerate(names):
            rounds = sorted(curves[site])
            values = [curves[site][r].get("auroc", np.nan) for r in rounds]
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
            ends.append((rounds[-1], values[-1]))
        low, high = ax.get_ylim()
        label_y = spread_labels([v for _, v in ends], gap=0.045 * (high - low))
        for site, (last_round, _), y_text in zip(names, ends, label_y, strict=True):
            ax.annotate(
                site,
                (last_round, y_text),
                xytext=(8, 0),
                textcoords="offset points",
                va="center",
                fontsize=8,
                color=INK_2,
                annotation_clip=False,
            )
        ax.set_xlabel("round")
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
                colour = "#ffffff" if count > matrix.max() / 2 else INK
                ax.text(j, i, str(count), ha="center", va="center", color=colour)
            ax.set_xticks([0, 1], ["predicted no", "predicted yes"])
            ax.set_yticks([0, 1], ["no disease", "disease"])
            ax.set_title(s["name"], fontsize=9, loc="left")
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
        note = ", not interpretable" if s["few"] else ""
        ax.plot(
            fpr,
            tpr,
            color=site_colour(i),
            linewidth=2,
            label=f"{s['name']}: AUROC {s['auroc']:.2f} (n={len(s['y'])}{note})",
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

    # 3. Test AUROC per site with 95% bootstrap CI (numbers in the row labels)
    fig, ax = plt.subplots(figsize=(8, 0.9 * len(labels) + 1.4))
    row_labels = []
    for row, label in enumerate(labels):
        s = sites[label]
        y_pos = len(labels) - 1 - row
        low, high = s["ci"]
        ax.plot([low, high], [y_pos, y_pos], color=site_colour(row), linewidth=2)
        ax.plot(
            s["auroc"],
            y_pos,
            "o",
            color=site_colour(row),
            markersize=8,
            markeredgecolor="#fcfcfb",
            markeredgewidth=2,
        )
        text = (
            f"{s['name']}\n{s['auroc']:.2f} ({low:.2f}-{high:.2f}), "
            f"n={len(s['y'])}, {int(s['y'].sum())} with disease"
        )
        if s["few"]:
            text += "\ntoo few cases of one class: not interpretable"
        row_labels.append(text)
    ax.axvline(0.5, color=AXIS, linewidth=0.8)
    ax.set_yticks(range(len(labels)), list(reversed(row_labels)), fontsize=8)
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.6, len(labels) - 0.4)
    ax.set_xlabel("AUROC on the test split (95% bootstrap CI)")
    ax.xaxis.grid(True)
    ax.set_axisbelow(True)
    ax.set_title(
        "Test AUROC of the final global model per site", loc="left", fontsize=11
    )
    fig.tight_layout()
    visualization.save_plot("Test AUROC per site")
    plt.close("all")

    # 4. Predicted probability by site and diagnosis
    fig, ax = plt.subplots(figsize=(8, 4.8))
    rng = np.random.default_rng(0)
    for i, label in enumerate(labels):
        s = sites[label]
        for cls, offset in ((0, -0.17), (1, 0.17)):
            p = s["prob"][s["y"] == cls]
            jitter = rng.uniform(-0.09, 0.09, len(p))
            ax.scatter(
                i + offset + jitter,
                p,
                s=14,
                color=CLASS_COLOURS[cls],
                alpha=0.8,
                edgecolors="none",
                label=CLASS_LABELS[cls] if i == 0 else None,
            )
    ax.axhline(THRESHOLD, color=AXIS, linewidth=0.8)
    ax.set_xticks(
        range(len(labels)), [sites[k]["name"].replace(" (", "\n(") for k in labels]
    )
    ax.set_ylabel("predicted probability of disease")
    ax.set_ylim(0, 1)
    ax.yaxis.grid(True)
    ax.set_axisbelow(True)
    ax.legend(
        frameon=False, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=2, fontsize=8
    )
    ax.set_title("Predicted probabilities on the test split", loc="left", fontsize=11)
    fig.tight_layout()
    visualization.save_plot("Predicted probabilities by diagnosis")
    plt.close("all")
    print("Saved 4 plots.")


main()
