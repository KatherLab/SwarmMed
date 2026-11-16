"""Per-case explanations of the sleep-staging model, for SwarmMedHub.

Runs in the Hub sandbox like results_visualization.py (global object ``explain``),
or locally:

    python -m swarmmed_explain.run_local --script examples/eeg_sleep_staging/explainability.py \
        --data <folder with fl-client-k/> --model FL_global_model.pt --out <bundle>

(from the repository root).

It reads the test split of the first site folder found (fl-client-1/, ...) or of
the data root, scores it with the global model, picks the most confident correct
and incorrect epochs of different sleep stages and explains each with two
methods, shown over the 30-s epoch:

* integrated gradients (zero baseline, 48 steps) for the predicted stage,
* occlusion: the drop of the predicted stage's logit when a 1-s window of one
  channel is set to zero (the median of the scaled signal).

Each case starts with its input panel: the epoch with the epochs before and after it for
context (when they are in the test split) and no model output, the panel a blinded review
shows before the reader saves a read. A fourth panel compares the two maps per second
(methods in greys) and shows the class probabilities. The
run-level figures show the confusion matrix and the agreement of the two methods.
Maps show what the model used; they do not show cause, and the scores are not
calibrated.
"""

import io

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from swarmmed_explain import (
    integrated_gradients,
    occlusion,
    plot_signal,
    rank_agreement,
    select_cases,
)
from swarmmed_explain.plotting import AGREEMENT_COLORS, method_style

CLASS_NAMES = ["W", "N1", "N2", "N3", "REM"]
CHANNEL_NAMES = ["Fpz-Cz", "Pz-Oz"]
FS = 100
MODEL_FILENAME = "FL_global_model.pt"
SITE_FILES = {"train.npz", "val.npz", "test.npz", "site_info.json"}
N_CASES = 8  # 4 correct and 4 incorrect, each of a different reference stage
IG_STEPS = 48
OCCLUSION_WINDOW = 100  # samples, 1 s
OCCLUSION_STRIDE = 50
SMOOTH_SAMPLES = 100  # attribution is smoothed over 1 s for drawing only
TOP_FRACTION = 0.2  # top-k overlap on the 30 one-second bins: k = 6
EPOCH_S = 30
# Methods are told apart by neutral greys: red and blue mean "supports" and "opposes".
IG_STYLE = method_style("integrated_gradients")
OCC_STYLE = method_style("occlusion")


class SleepStageCNN(nn.Module):
    """Small 1-D CNN for one 30-s, 2-channel EEG epoch (as in training.py)."""

    def __init__(self, n_channels=2, n_classes=5):
        """Build the layers."""
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv1d(n_channels, 32, kernel_size=50, stride=6, bias=False),
            nn.BatchNorm1d(32),
            nn.ReLU(),
            nn.MaxPool1d(8),
            nn.Conv1d(32, 64, kernel_size=8, padding=4, bias=False),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.Conv1d(64, 64, kernel_size=8, padding=4, bias=False),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.MaxPool1d(4),
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
        )
        self.head = nn.Linear(64, n_classes)

    def forward(self, x):
        """Return class logits for a batch of epochs."""
        return self.head(self.features(x))


def find_site_prefix():
    """Return the folder prefix ('' for the data root) of the first site with test.npz."""
    keys = [k for k in explain.listdir("") if not k.startswith("results/")]
    if "test.npz" in keys:
        return ""
    folders = sorted(
        k.split("/")[0] for k in keys if k.count("/") == 1 and k.endswith("/test.npz")
    )
    if not folders:
        raise FileNotFoundError("no test.npz at the data root or in a site folder")
    return folders[0] + "/"


def read_arrays(path):
    """Read one .npz file completely, then return its arrays."""
    with explain.open(path, "rb") as handle:
        raw = handle.read()
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def per_second(values):
    """Sum a (channels, samples) map over channels and 1-s bins -> (30,)."""
    bins = values.sum(axis=0)[: len(values[0]) // FS * FS]
    return bins.reshape(-1, FS).sum(axis=1)


def smooth(values):
    """Moving average over SMOOTH_SAMPLES samples along the last axis (drawing only)."""
    kernel = np.ones(SMOOTH_SAMPLES) / SMOOTH_SAMPLES
    return np.stack([np.convolve(row, kernel, mode="same") for row in values])


def pick_cases(labels, predictions, probabilities):
    """Most confident correct and incorrect epochs, each of a different reference stage."""
    ranked = select_cases(labels, probabilities, n_per_outcome=len(labels))
    chosen = []
    for outcome in ("correct", "incorrect"):
        seen = set()
        for case in ranked:
            if (
                case.outcome == outcome
                and case.label not in seen
                and len(seen) < N_CASES // 2
            ):
                seen.add(case.label)
                chosen.append(case)
    return chosen


def context_window(test, x_all, i):
    """Epoch i with the epochs just before and after it, when the test split holds them.

    Returns the (channels, samples) signal, the start of epoch i in it (s) and a text
    naming the neighbours shown.
    """
    index = {
        (int(s), int(n), int(e)): k
        for k, (s, n, e) in enumerate(
            zip(test["subject"], test["night"], test["epoch"], strict=True)
        )
    }
    key = (int(test["subject"][i]), int(test["night"][i]), int(test["epoch"][i]))
    before = index.get((key[0], key[1], key[2] - 1))
    after = index.get((key[0], key[1], key[2] + 1))
    parts = [x_all[k] for k in (before, i, after) if k is not None]
    shown = [
        name for name, k in (("before", before), ("after", after)) if k is not None
    ]
    text = f"with the epoch{'s' if len(shown) == 2 else ''} {' and '.join(shown)}"
    return (
        np.concatenate(parts, axis=-1),
        0.0 if before is None else float(EPOCH_S),
        text if shown else "without neighbouring epochs",
    )


def draw_probabilities(ax, probabilities, label, prediction):
    """Bar chart of the class probabilities; reference and predicted stage marked."""
    colours = ["#bababa"] * len(CLASS_NAMES)
    colours[prediction] = "#4d4d4d"
    ax.bar(CLASS_NAMES, probabilities, color=colours)
    ax.set_ylim(0, 1)
    ax.set_ylabel("probability", fontsize=8)
    ax.set_title(
        f"scored {CLASS_NAMES[label]}, predicted {CLASS_NAMES[prediction]}", fontsize=9
    )
    ax.tick_params(labelsize=7)


def main():
    """Explain the most confident correct and incorrect test epochs of one site."""
    torch.set_num_threads(2)
    prefix = find_site_prefix()
    site = prefix.rstrip("/") or "data root"
    test = read_arrays(prefix + "test.npz")
    x_all = test["X"].astype(np.float32)
    labels = test["y"].astype(np.int64)

    model = SleepStageCNN()
    state = explain.get_model(client_name="fl-client-1", model_filename=MODEL_FILENAME)
    model.load_state_dict(state, strict=True)
    model = model.float().eval()

    with torch.no_grad():
        logits = torch.cat(
            [
                model(torch.from_numpy(x_all[i : i + 256]))
                for i in range(0, len(x_all), 256)
            ]
        )
    probabilities = torch.softmax(logits, dim=1).numpy()
    predictions = probabilities.argmax(axis=1)
    accuracy = float((predictions == labels).mean())
    print(f"site {site}: {len(labels)} test epochs, accuracy {accuracy:.3f}")

    agreement_rows = []
    for case in pick_cases(labels, predictions, probabilities):
        i = case.index
        x = x_all[i]
        target = case.prediction
        case_id = f"s{int(test['subject'][i]):02d}n{int(test['night'][i])}e{int(test['epoch'][i]):04d}"
        ig = integrated_gradients(model, x, target, baseline=0.0, steps=IG_STEPS)
        occ = occlusion(
            model,
            x,
            target,
            window=(1, OCCLUSION_WINDOW),
            stride=(1, OCCLUSION_STRIDE),
            baseline=0.0,
        )
        ig_bins, occ_bins = per_second(ig.attributions), per_second(occ)
        stats = rank_agreement(ig_bins, occ_bins, k=TOP_FRACTION)
        explain.add_case(
            case_id,
            label=CLASS_NAMES[case.label],
            prediction=CLASS_NAMES[target],
            score=case.score,
            target=f"stage {CLASS_NAMES[target]}",
            outcome=case.outcome,
            details={
                "site": site,
                "subject": int(test["subject"][i]),
                "night": int(test["night"][i]),
                "epoch_index": int(test["epoch"][i]),
                "scored_stage": CLASS_NAMES[case.label],
                "predicted_stage": CLASS_NAMES[target],
                "spearman_ig_occlusion": stats["spearman"],
                "top6_overlap_ig_occlusion": stats["top_k_overlap"],
                "ig_completeness_gap": ig.completeness_gap,
            },
        )

        signal_kwargs = {"fs": FS, "channel_names": CHANNEL_NAMES, "units": "scaled"}
        # Input panel first: the epoch and its neighbours only (no score, prediction or
        # map), so it can stay visible in a blinded review.
        context, start, neighbours = context_window(test, x_all, i)
        fig = plot_signal(
            context,
            None,
            title=f"EEG epoch {case_id}: shaded the 30 s the model sees, {neighbours}",
            figsize=(9.0, 2.6),
            **signal_kwargs,
        )
        for ax in fig.axes:
            ax.axvspan(start, start + EPOCH_S, color="0.88", zorder=0)
        explain.add_panel(case_id, "input", "The epoch with its neighbours", figure=fig)

        plot_signal(
            x,
            smooth(ig.attributions),
            title=f"Integrated gradients for predicted stage {CLASS_NAMES[target]} ({case.outcome})",
            attribution_label="attribution (1-s mean)",
            **signal_kwargs,
        )
        explain.add_panel(
            case_id, "integrated_gradients", "Integrated gradients over the epoch"
        )

        plot_signal(
            x,
            occ,
            title=f"Occlusion of 1-s windows, predicted stage {CLASS_NAMES[target]}",
            attribution_label="logit drop",
            **signal_kwargs,
        )
        explain.add_panel(case_id, "occlusion", "Occlusion over the epoch")

        agreement_rows.append((case_id, stats["spearman"], stats["top_k_overlap"]))
        fig, (left, right) = plt.subplots(1, 2, figsize=(7.0, 2.8), width_ratios=[1, 2])
        draw_probabilities(left, probabilities[i], case.label, target)
        seconds = np.arange(len(ig_bins)) + 0.5
        right.plot(
            seconds,
            ig_bins / (np.abs(ig_bins).max() or 1.0),
            label="integrated gradients",
            **IG_STYLE,
        )
        right.plot(
            seconds,
            occ_bins / (np.abs(occ_bins).max() or 1.0),
            label="occlusion",
            **OCC_STYLE,
        )
        right.axhline(0, color="#888888", linewidth=0.5)
        right.set_xlim(0, len(ig_bins))
        right.set_xlabel("time in epoch (s)", fontsize=8)
        right.set_ylabel("attribution, per 1 s,\nscaled to max 1", fontsize=8)
        rho = stats["spearman"]
        right.set_title(
            f"agreement: Spearman {rho:.2f}, top-{stats['k']} overlap {stats['top_k_overlap']:.2f}"
            if rho is not None
            else "agreement: undefined",
            fontsize=9,
        )
        right.tick_params(labelsize=7)
        right.legend(fontsize=7, loc="best")
        fig.tight_layout()
        explain.add_panel(
            case_id, "method_agreement", "Probabilities and method agreement"
        )

    matrix = np.bincount(labels * 5 + predictions, minlength=25).reshape(5, 5)
    shares = matrix / np.maximum(matrix.sum(axis=1, keepdims=True), 1)
    fig, ax = plt.subplots(figsize=(4.2, 3.8))
    image = ax.imshow(shares, vmin=0, vmax=1, cmap="Blues")
    for row in range(5):
        for column in range(5):
            ax.text(
                column,
                row,
                f"{matrix[row, column]}",
                ha="center",
                va="center",
                fontsize=8,
                color="white" if shares[row, column] > 0.5 else "black",
            )
    ax.set_xticks(range(5), CLASS_NAMES)
    ax.set_yticks(range(5), CLASS_NAMES)
    ax.set_xlabel("predicted stage", fontsize=8)
    ax.set_ylabel("scored stage", fontsize=8)
    ax.set_title(f"Test epochs of {site} (colour: share of row)", fontsize=9)
    fig.colorbar(image, ax=ax, fraction=0.046)
    fig.tight_layout()
    explain.add_summary("Confusion matrix on the test split")

    names = [row[0] for row in agreement_rows]
    rho_values = [np.nan if row[1] is None else row[1] for row in agreement_rows]
    fig, ax = plt.subplots(figsize=(6.0, 3.2))
    positions = np.arange(len(names))
    ax.bar(
        positions - 0.2,
        rho_values,
        width=0.4,
        label="Spearman",
        color=AGREEMENT_COLORS["spearman"],
    )
    ax.bar(
        positions + 0.2,
        [row[2] for row in agreement_rows],
        width=0.4,
        label="top-6 overlap",
        color=AGREEMENT_COLORS["top_k_overlap"],
    )
    ax.axhline(0, color="#888888", linewidth=0.5)
    ax.set_xticks(positions, names, rotation=45, ha="right", fontsize=7)
    ax.set_ylim(-1, 1)
    ax.set_title(
        "Agreement of integrated gradients and occlusion (per 1 s)", fontsize=9
    )
    ax.legend(fontsize=7)
    fig.tight_layout()
    explain.add_summary("Agreement of the two methods")

    explain.set_info(
        methods=["integrated_gradients", "occlusion", "method_agreement"],
        target="predicted sleep stage (logit)",
        notes=(
            f"Test split of {site}; the most confident correct and incorrect epochs of "
            "different scored stages. Red supports, blue opposes the predicted stage. "
            "Maps show what the model used, not cause."
        ),
        metrics={"test_accuracy": accuracy, "test_epochs": int(len(labels))},
    )


main()
