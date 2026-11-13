"""Result plots for the ehr_eicu_mortality template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script for a finished training job with a helper object
``visualization`` (``open``, ``listdir``, ``exists``, ``save_plot``; at most four plots). The
helper's manifest holds the project data (``fl-client-1/test.npz``, ...) and the job's result
files under ``results/`` (the final model ``FL_global_model.pt``, the per-round
``val_metrics.json`` that training.py writes next to it, and a client log if the job has one).
The script

1. plots the validation AUROC of the received global model per round and site (from the
   ``SWARMMED_METRIC`` lines of a client log, else from ``val_metrics.json``; confusion matrices
   of the final model if neither is found),
2. scores the final global model (``FL_global_model.pt``) on every site's test split and
   plots the ROC curves, with the pooled test cases as a thick line,
3. plots the test AUROC per site and pooled with a 95% bootstrap confidence interval (2,000
   resamples, seed 0, as evaluate.py),
4. plots the predicted probabilities by site and outcome.

The model class below must match training.py; the weights are loaded strictly, so a mismatch
stops the script with an error instead of plotting a half-loaded model. AUROC and ROC points
are computed with numpy (no scikit-learn import), which keeps the run within the sandbox's
1 GB of memory. With 5 to 7 deaths per site test split the per-site intervals are very wide.
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
N_FEATURES = 41
THRESHOLD = 0.5
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0
MIN_CLASS_CASES = 10  # fewer deaths (or survivors) make the AUROC unstable
SITE_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
POOLED_COLOUR = "#0b0b0b"
CLASS_COLOURS = {0: "#6250d6", 1: "#e34948"}  # survived, died
CLASS_LABELS = {0: "discharged alive", 1: "died in hospital"}
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


class MortalityMLP(nn.Module):
    """Same layers and names as in training.py: 41 -> 32 (ReLU, dropout) -> 1 logit."""

    def __init__(self, n_features=N_FEATURES):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_features, HIDDEN_UNITS),
            nn.ReLU(),
            nn.Dropout(DROPOUT),
            nn.Linear(HIDDEN_UNITS, 1),
        )

    def forward(self, x):
        return self.net(x).squeeze(-1)


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
    model = MortalityMLP()
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
    return info.get("source_site")


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


def read_metric_files(keys):
    """{site: {round: {metric: value}}} from results/.../val_metrics.json written by training.py."""
    curves = {}
    for key in sorted(keys, key=natural_key):
        if not key.startswith("results/") or key.split("/")[-1] != "val_metrics.json":
            continue
        payload = json.loads(read_bytes(key).decode("utf-8"))
        site = payload.get("site") or key.split("/")[1]
        for record in payload.get("rounds", []):
            curves.setdefault(site, {})[int(record["round"])] = {
                name[4:]: float(value)
                for name, value in record.items()
                if name.startswith("val_") and value is not None
            }
    return curves


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
    if len(labels) > 1:
        y_all = np.concatenate([sites[k]["y"] for k in labels])
        p_all = np.concatenate([sites[k]["prob"] for k in labels])
        sites["pooled"] = {
            "y": y_all,
            "prob": p_all,
            "name": "all sites pooled",
            "auroc": auroc(y_all, p_all),
            "ci": bootstrap_ci(y_all, p_all),
            "few": min(int(y_all.sum()), int(len(y_all) - y_all.sum()))
            < MIN_CLASS_CASES,
        }
    rows = list(sites)  # the sites, then "pooled"
    print(f"{'site':<28} {'n':>4} {'pos':>4}  AUROC (95% CI)")
    for label in rows:
        s = sites[label]
        print(
            f"{s['name']:<28} {len(s['y']):>4} {int(s['y'].sum()):>4}  "
            f"{s['auroc']:.3f} ({s['ci'][0]:.3f}-{s['ci'][1]:.3f})"
        )

    # 1. Validation AUROC per round (client logs or val_metrics.json), else confusion matrices
    curves = read_metric_lines(keys) or read_metric_files(keys)
    if curves:
        fig, ax = plt.subplots(figsize=(8.5, 4.8))
        names = sorted(curves, key=natural_key)
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
            "No SWARMMED_METRIC lines or val_metrics.json under results/; plotting confusion matrices."
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
            ax.set_xticks([0, 1], ["predicted survive", "predicted death"])
            ax.set_yticks([0, 1], ["survived", "died"])
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
    for i, label in enumerate(rows):
        s = sites[label]
        if np.isnan(s["auroc"]):
            continue
        fpr, tpr = roc_points(s["y"], s["prob"])
        note = ", few deaths" if s["few"] else ""
        pooled = label == "pooled"
        ax.plot(
            fpr,
            tpr,
            color=POOLED_COLOUR if pooled else site_colour(i),
            linewidth=3 if pooled else 1.6,
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
    fig, ax = plt.subplots(figsize=(8, 0.9 * len(rows) + 1.4))
    row_labels = []
    for row, label in enumerate(rows):
        s = sites[label]
        colour = POOLED_COLOUR if label == "pooled" else site_colour(row)
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
        text = (
            f"{s['name']}\n{s['auroc']:.2f} ({low:.2f}-{high:.2f}), "
            f"n={len(s['y'])}, {int(s['y'].sum())} deaths"
        )
        if s["few"]:
            text += "\nfew deaths: the interval is very wide"
        row_labels.append(text)
    ax.axvline(0.5, color=AXIS, linewidth=0.8)
    ax.set_yticks(range(len(rows)), list(reversed(row_labels)), fontsize=8)
    ax.set_xlim(0, 1)
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
    ax.set_ylabel("predicted probability of death")
    ax.set_ylim(0, 1)
    ax.yaxis.grid(True)
    ax.set_axisbelow(True)
    ax.legend(
        frameon=False, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=2, fontsize=8
    )
    ax.set_title("Predicted probabilities on the test split", loc="left", fontsize=11)
    fig.tight_layout()
    visualization.save_plot("Predicted probabilities by outcome")
    plt.close("all")
    print("Saved 4 plots.")


main()
