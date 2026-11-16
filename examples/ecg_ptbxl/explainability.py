"""Per-case explanations of the ECG model (normal vs abnormal), for SwarmMedHub.

Runs in the Hub sandbox like results_visualization.py (global object ``explain``), or locally
from the repository root:

    python -m swarmmed_explain.run_local --script examples/ecg_ptbxl/explainability.py \
        --data <folder with fl-client-k/> --model FL_global_model.pt --out <bundle>

It reads the test split of the first site folder found (fl-client-1/, ...) or of the data
root, scores it with the global model, picks the two most confident cases of each outcome (true
and false positives and negatives; "abnormal" is the positive class), shows each record first
without any model output (the input panel, which a blinded review shows before the reader saves
a read) and explains each with two methods for the abnormal-class logit, drawn over the
10-s, 12-lead record:

* integrated gradients (baseline: every lead at its median, 32 steps),
* occlusion: the drop of the logit when a 0.5-s window of one lead is set to its median
  (stride 0.25 s).

A fourth panel compares the two per half second and per lead (methods in greys). The run-level
figures show the
distribution of the model's scores on the test split, the agreement of the two methods per case
and the share of attribution each lead receives. Red supports "abnormal", blue opposes it. The
maps show what the model used in a record; they do not show cause, they are not a diagnosis, and
the scores are not calibrated.
"""

import io

import matplotlib.pyplot as plt
import numpy as np
import torch
from swarmmed_explain import (
    as_predict_fn,
    integrated_gradients,
    occlusion,
    plot_signal,
    rank_agreement,
    select_cases,
)
from swarmmed_explain.plotting import AGREEMENT_COLORS, method_style
from torch import nn

MODEL_FILENAME = "FL_global_model.pt"
LEADS = ["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]
SUPERCLASSES = ["NORM", "MI", "STTC", "CD", "HYP"]
FS = 100
N_LEADS = 12
N_PER_OUTCOME = 2
IG_STEPS = 32
OCCLUSION_WINDOW = 50  # samples, 0.5 s
OCCLUSION_STRIDE = 25
BIN = 50  # the agreement is computed per half second
SMOOTH_SAMPLES = 25  # integrated gradients are smoothed over 0.25 s for drawing only
TOP_FRACTION = 0.25  # top-k overlap over the 20 half-second bins: k = 5
# Methods are told apart by neutral greys: red and blue mean "supports" and "opposes".
IG_STYLE = method_style("integrated_gradients")
OCC_STYLE = method_style("occlusion")


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


def smooth(values):
    """Moving average over SMOOTH_SAMPLES samples along time (drawing only)."""
    kernel = np.ones(SMOOTH_SAMPLES) / SMOOTH_SAMPLES
    return np.stack([np.convolve(row, kernel, mode="same") for row in values])


def per_bin(values):
    """Sum a (leads, samples) map over leads and half-second bins -> (20,)."""
    return values.sum(axis=0).reshape(-1, BIN).sum(axis=1)


def per_lead(values):
    """Sum of |map| per lead, as a share of the total -> (12,)."""
    total = np.abs(values).sum(axis=1)
    return total / max(float(total.sum()), 1e-12)


def scale(values):
    return values / (np.abs(values).max() or 1.0)


def main():
    prefix = find_site_prefix()
    site = prefix.rstrip("/") or "data root"
    test = read_arrays(prefix + "test.npz")
    x_all = normalise_ecg(test["X"])
    labels = test["y"].astype(np.int64)
    ecg_ids = test["ecg_id"]
    y_super = test["y_super"]

    model = ECGNet()
    state = explain.get_model(client_name="fl-client-1", model_filename=MODEL_FILENAME)
    model.load_state_dict(
        {name: torch.as_tensor(value) for name, value in state.items()}
    )
    model.eval()
    scores = as_predict_fn(model, output="probability", batch_size=128)(x_all.numpy())
    predictions = (scores >= 0.5).astype(int)
    accuracy = float((predictions == labels).mean())
    print(f"site {site}: {len(labels)} test records, accuracy {accuracy:.3f}")

    rows = []
    for case in select_cases(labels, scores, n_per_outcome=N_PER_OUTCOME):
        i = case.index
        x = x_all[i].numpy()
        case_id = str(int(ecg_ids[i]))
        present = [
            name for name, flag in zip(SUPERCLASSES, y_super[i], strict=True) if flag
        ]
        ig = integrated_gradients(model, x, None, baseline=0.0, steps=IG_STEPS)
        occ = occlusion(
            model,
            x,
            None,
            window=(1, OCCLUSION_WINDOW),
            stride=(1, OCCLUSION_STRIDE),
            baseline=0.0,
        )
        ig_bins, occ_bins = per_bin(ig.attributions), per_bin(occ)
        stats = rank_agreement(ig_bins, occ_bins, k=TOP_FRACTION)
        rows.append(
            (case_id, case.outcome, stats, per_lead(ig.attributions), per_lead(occ))
        )
        explain.add_case(
            case_id,
            label="abnormal" if case.label else "normal",
            prediction="abnormal" if case.prediction else "normal",
            score=case.score,
            target="abnormal logit",
            outcome=case.outcome,
            details={
                "site": site,
                "superclasses": ",".join(present),
                "spearman_ig_occlusion": stats["spearman"],
                "top5_overlap_ig_occlusion": stats["top_k_overlap"],
                "ig_completeness_gap": ig.completeness_gap,
            },
        )
        signal_kwargs = {"fs": FS, "channel_names": LEADS, "units": "mV"}
        # Input panel first: the record only (no score, prediction or map), so it can
        # stay visible in a blinded review.
        plot_signal(
            x,
            None,
            title=f"ECG record {case_id}: 12 leads, 10 s at {FS} Hz, each lead minus its median",
            figsize=(8.0, 7.2),
            **signal_kwargs,
        )
        explain.add_panel(case_id, "input", "The 12-lead ECG")

        plot_signal(
            x,
            smooth(ig.attributions),
            title=f"Integrated gradients, {case.outcome}: score {case.score:.2f} (record {case_id})",
            attribution_label="attribution (0.25-s mean)",
            figsize=(8.0, 7.2),
            **signal_kwargs,
        )
        explain.add_panel(
            case_id, "integrated_gradients", "Integrated gradients over the record"
        )

        plot_signal(
            x,
            occ,
            title="Occlusion of 0.5-s windows: drop of the abnormal logit",
            attribution_label="logit drop",
            figsize=(8.0, 7.2),
            **signal_kwargs,
        )
        explain.add_panel(case_id, "occlusion", "Occlusion over the record")

        fig, (left, right) = plt.subplots(
            1, 2, figsize=(9.0, 3.4), width_ratios=[1.5, 1]
        )
        seconds = (np.arange(len(ig_bins)) + 0.5) * BIN / FS
        left.plot(seconds, scale(ig_bins), label="integrated gradients", **IG_STYLE)
        left.plot(seconds, scale(occ_bins), label="occlusion", **OCC_STYLE)
        left.axhline(0, color="#888888", linewidth=0.5)
        left.set_xlim(0, N_SAMPLES_S)
        left.set_xlabel("time (s)", fontsize=8)
        left.set_ylabel("attribution per 0.5 s,\nscaled to max 1", fontsize=8)
        rho = stats["spearman"]
        left.set_title(
            f"Spearman {rho:.2f}, top-{stats['k']} overlap {stats['top_k_overlap']:.2f}"
            if rho is not None
            else "agreement undefined",
            fontsize=9,
        )
        left.tick_params(labelsize=7)
        left.legend(fontsize=7, loc="best")
        positions = np.arange(N_LEADS)
        right.bar(
            positions - 0.2,
            rows[-1][3],
            width=0.4,
            color=IG_STYLE["color"],
            label="integrated gradients",
        )
        right.bar(
            positions + 0.2,
            rows[-1][4],
            width=0.4,
            color=OCC_STYLE["color"],
            label="occlusion",
        )
        right.set_xticks(positions, LEADS, fontsize=7)
        right.set_ylabel("share of total |attribution|", fontsize=8)
        right.set_title("Which leads", fontsize=9)
        right.tick_params(labelsize=7)
        fig.tight_layout()
        explain.add_panel(case_id, "method_agreement", "Agreement of the two methods")

    # Run-level figures
    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    bins = np.linspace(0, 1, 26)
    ax.hist(scores[labels == 0], bins=bins, color="#6250d6", alpha=0.75, label="normal")
    ax.hist(
        scores[labels == 1], bins=bins, color="#e34948", alpha=0.75, label="abnormal"
    )
    ax.axvline(0.5, color="#555555", linewidth=0.8)
    ax.set_xlabel("model score (probability of abnormal)", fontsize=8)
    ax.set_ylabel("test records", fontsize=8)
    ax.set_title(f"Scores on the test split of {site} (n={len(labels)})", fontsize=9)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=7)
    fig.tight_layout()
    explain.add_summary("Model scores on the test split")

    names = [row[0] for row in rows]
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    positions = np.arange(len(rows))
    ax.bar(
        positions - 0.2,
        [np.nan if r[2]["spearman"] is None else r[2]["spearman"] for r in rows],
        width=0.4,
        color=AGREEMENT_COLORS["spearman"],
        label="Spearman",
    )
    ax.bar(
        positions + 0.2,
        [r[2]["top_k_overlap"] for r in rows],
        width=0.4,
        color=AGREEMENT_COLORS["top_k_overlap"],
        label="top-5 overlap",
    )
    ax.axhline(0, color="#888888", linewidth=0.5)
    ax.set_xticks(
        positions,
        [f"{n}\n{r[1]}" for n, r in zip(names, rows, strict=True)],
        fontsize=7,
    )
    ax.set_ylim(-0.2, 1.25)
    ax.set_title(
        "Agreement of integrated gradients and occlusion (per 0.5 s)", fontsize=9
    )
    ax.legend(fontsize=7, ncols=2, loc="upper right")
    fig.tight_layout()
    explain.add_summary("Agreement of the two methods per case")

    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    positions = np.arange(N_LEADS)
    ax.bar(
        positions - 0.2,
        np.mean([r[3] for r in rows], axis=0),
        width=0.4,
        color=IG_STYLE["color"],
        label="integrated gradients",
    )
    ax.bar(
        positions + 0.2,
        np.mean([r[4] for r in rows], axis=0),
        width=0.4,
        color=OCC_STYLE["color"],
        label="occlusion",
    )
    ax.set_xticks(positions, LEADS, fontsize=8)
    ax.set_ylabel("share of |attribution|, mean over the cases", fontsize=8)
    ax.set_title(
        f"Attribution per lead over the {len(rows)} explained records", fontsize=9
    )
    ax.legend(fontsize=7)
    fig.tight_layout()
    explain.add_summary("Attribution per lead")

    explain.set_info(
        methods=["integrated_gradients", "occlusion", "method_agreement"],
        target="abnormal logit (normal vs abnormal ECG)",
        notes=(
            f"Test split of {site}; the {N_PER_OUTCOME} most confident cases of each outcome "
            "(TP, FP, FN, TN; abnormal is the positive class). Red supports abnormal, blue opposes it. "
            "Maps show what the model used in a record, not the cause of a finding; they support a "
            "review and do not justify a prediction."
        ),
        metrics={"test_accuracy": accuracy, "test_records": int(len(labels))},
    )


N_SAMPLES_S = 10
main()
