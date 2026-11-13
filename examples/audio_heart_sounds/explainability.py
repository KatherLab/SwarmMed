"""Per-case explanations for the heart sound template, run by SwarmMedHub in its sandbox.

SwarmMedHub provides the `explain` helper (get_model, load_weights, open, exists,
listdir, add_case, add_panel, add_summary, set_info) with the project data and the
job's result files ("results/<site>/FL_global_model.pt"), and the package
`swarmmed_explain`. The script

  1. loads the final global model and the test split of the first site folder it finds
     (or the root-level files);
  2. scores every test recording (the mean of its segments' probabilities) and selects
     2 recordings per outcome (TP, FP, FN, TN) with swarmmed_explain.select_cases, the
     most confident ones per outcome;
  3. shows each selected recording first without any model output (the input panel, which
     a blinded review shows before the reader saves a read): the log-mel spectrogram of its
     middle 3-second segment, a choice that does not depend on the model;
  4. explains, for each selected recording, the 3-second log-mel segment that pushes
     the recording's score most in the direction of the model's decision, with
       - occlusion over time-frequency patches (8 mel bands x 15 frames): the drop of
         the abnormal logit when a patch is replaced by the site's mean level,
       - integrated gradients of the abnormal logit from the mean level (zeros in the
         normalised input),
       - the two maps summed over the mel bands, as a function of time (methods in greys);
  5. adds two run-level figures: the mean attribution per mel band over the selected
     cases, and how much the two methods agree on each case.

Red means evidence for "abnormal" (the abnormal logit rises with it), blue evidence for
"normal". The maps describe what the model used. They support a reviewer's reading of the
recording; they do not show why the heart sound is abnormal and do not justify a prediction.
"""

import io
import json
import time

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from swarmmed_explain import (
    integrated_gradients,
    occlusion,
    plot_spectrogram,
    rank_agreement,
    select_cases,
)
from swarmmed_explain.plotting import method_style

N_PER_OUTCOME = 2
OCCLUSION_WINDOW = (8, 15)  # mel bands x frames (frame hop 20 ms)
OCCLUSION_STRIDE = (4, 8)
IG_STEPS = 32
BATCH = 16
N_MELS = 32
FRAME_S = 0.02  # hop of 40 samples at 2000 Hz
FMIN, FMAX = 25.0, 800.0
SEED = 0


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
        """Return one logit per segment (abnormal vs normal)."""
        return self.head(self.features(x)).squeeze(1)


def mel_centres_hz():
    """Centre frequencies of the 32 HTK mel bands between 25 and 800 Hz."""

    def to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    edges = np.linspace(to_mel(FMIN), to_mel(FMAX), N_MELS + 2)
    return 700.0 * (10.0 ** (edges[1:-1] / 2595.0) - 1.0)


def read_bytes(path):
    """Read a registered file completely."""
    with explain.open(path, "rb") as handle:
        return handle.read()


def find_site_folder():
    """First site folder with test.npz and site_info.json ('' for root-level files)."""
    names = explain.listdir("")
    folders = sorted(
        {n.split("/", 1)[0] for n in names if "/" in n and not n.startswith("results/")}
    )
    for folder in folders:
        if explain.exists(f"{folder}/test.npz") and explain.exists(
            f"{folder}/site_info.json"
        ):
            return f"{folder}/"
    if explain.exists("test.npz") and explain.exists("site_info.json"):
        return ""
    raise FileNotFoundError(
        "no test.npz and site_info.json in the registered data (looked in "
        f"{folders[:10] or 'no folders'} and the root); see README.md, 'Data layout'"
    )


@torch.no_grad()
def segment_probabilities(model, x):
    """Abnormal probability of every segment."""
    out = [torch.sigmoid(model(x[i : i + 64])) for i in range(0, len(x), 64)]
    return torch.cat(out).numpy().astype(np.float64)


def input_panel(segment, start_s, title):
    """Log-mel segment in grey, no attribution (mel band, seconds in the recording)."""
    return plot_spectrogram(
        segment,
        times=start_s + np.arange(segment.shape[-1]) * FRAME_S,
        freqs=mel_centres_hz(),
        title=title,
        figsize=(6.0, 3.2),
    )


def standard_panel(segment, attribution, title, label):
    """Log-mel segment in grey with an attribution overlay (mel band, seconds)."""
    return plot_spectrogram(
        segment,
        attribution,
        times=np.arange(segment.shape[-1]) * FRAME_S,
        freqs=mel_centres_hz(),
        title=title,
        attribution_label=label,
        figsize=(6.0, 3.2),
    )


def time_profile_panel(occ, ig, title):
    """Both maps summed over the mel bands, as a function of time in the segment."""
    t = np.arange(occ.shape[-1]) * FRAME_S
    fig, ax = plt.subplots(figsize=(6.0, 2.6))
    ax.axhline(0, color="#888888", lw=0.8)
    ax.plot(
        t,
        occ.sum(axis=0) / np.abs(occ).sum() * occ.shape[-1],
        label="occlusion",
        lw=1.4,
        **method_style("occlusion"),
    )
    ax.plot(
        t,
        ig.sum(axis=0) / np.abs(ig).sum() * ig.shape[-1],
        label="integrated gradients",
        lw=1.4,
        **method_style("integrated_gradients"),
    )
    ax.set_xlabel("time in the segment (s)", fontsize=8)
    ax.set_ylabel("sum over mel bands,\nscaled per method", fontsize=8)
    ax.set_title(title, fontsize=9)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=7, frameon=False)
    ax.margins(x=0)
    fig.tight_layout()
    return fig


def main():
    """Select cases from the first site's test split and explain them."""
    started = time.perf_counter()
    torch.set_num_threads(2)
    torch.manual_seed(SEED)
    folder = find_site_folder()
    site = folder.rstrip("/") or "registered data (root)"
    print(f"explaining test recordings of {site}")

    info = json.loads(read_bytes(folder + "site_info.json").decode("utf-8"))
    norm = info["normalisation"]
    mean = np.asarray(norm["mean"], dtype=np.float32)[None, :, None]
    std = np.asarray(norm["std"], dtype=np.float32)[None, :, None]
    with np.load(io.BytesIO(read_bytes(folder + "test.npz")), allow_pickle=False) as z:
        raw = {k: z[k] for k in ("x", "y", "rec", "rec_ids", "rec_y")}
        starts = z["seg_start_s"] if "seg_start_s" in z.files else None
    x_all = torch.from_numpy(((raw["x"].astype(np.float32) - mean) / std)[:, None])
    rec, rec_y = raw["rec"].astype(np.int64), raw["rec_y"].astype(np.int64)
    rec_ids = [str(r) for r in raw["rec_ids"]]

    model = PCGNet()
    explain.load_weights(
        model, client_name="fl-client-1", model_filename="FL_global_model.pt"
    )
    model.eval()

    seg_prob = segment_probabilities(model, x_all)
    counts = np.bincount(rec, minlength=rec_y.size)
    rec_score = np.bincount(rec, weights=seg_prob, minlength=rec_y.size) / counts
    chosen = select_cases(rec_y, rec_score, n_per_outcome=N_PER_OUTCOME, threshold=0.5)
    print(f"{len(rec_y)} test recordings; explaining {len(chosen)} of them")

    attr_rows, agreement_rows = [], []
    for case in chosen:
        i = case.index
        seg_idx = np.flatnonzero(rec == i)
        up = case.prediction == 1
        pick = seg_idx[
            np.argmax(seg_prob[seg_idx]) if up else np.argmin(seg_prob[seg_idx])
        ]
        segment = x_all[pick]  # (1, 32, 150), normalised
        occ = occlusion(
            model,
            segment,
            window=OCCLUSION_WINDOW,
            stride=OCCLUSION_STRIDE,
            baseline=0.0,
            batch_size=BATCH,
        )[0]
        ig = integrated_gradients(
            model, segment, baseline=0.0, steps=IG_STEPS, batch_size=BATCH
        )
        ig_map = ig.attributions[0]
        agree = rank_agreement(occ, ig_map, k=0.1)
        agree_abs = rank_agreement(occ, ig_map, k=0.1, absolute=True)
        patch = torch.nn.functional.avg_pool2d  # compare at the occlusion patch scale
        pooled = [
            patch(torch.as_tensor(m)[None, None], OCCLUSION_WINDOW, OCCLUSION_STRIDE)[
                0, 0
            ]
            for m in (occ, ig_map)
        ]
        agree_patch = rank_agreement(pooled[0].numpy(), pooled[1].numpy(), k=0.1)
        attr_rows.append((case.outcome, ig_map))
        agreement_rows.append(
            (case.outcome, rec_ids[i], agree["spearman"], agree["top_k_overlap"])
        )

        details = {
            "site": site,
            "segments_in_recording": int(counts[i]),
            "explained_segment_probability": float(seg_prob[pick]),
            "explained_segment_of_recording": int(np.flatnonzero(seg_idx == pick)[0])
            + 1,
            "ig_completeness_gap": float(ig.completeness_gap),
            "top10pct_overlap": float(agree["top_k_overlap"]),
            "top10pct_overlap_abs": float(agree_abs["top_k_overlap"]),
        }
        if agree_patch["spearman"] is not None:
            details["spearman_patch_level"] = float(agree_patch["spearman"])
        if agree["spearman"] is not None:
            details["spearman_occlusion_vs_ig"] = float(agree["spearman"])
        cid = rec_ids[i]
        explain.add_case(
            cid,
            label=int(case.label),
            prediction=int(case.prediction),
            score=float(case.score),
            target="abnormal logit",
            outcome=case.outcome,
            details=details,
        )
        # Input panel first: the middle segment, chosen without the model (the explained
        # segment depends on the prediction), with no score, prediction or map.
        middle = seg_idx[len(seg_idx) // 2]
        input_panel(
            x_all[middle][0].numpy(),
            0.0 if starts is None else float(starts[middle]),
            f"Recording {cid}: log-mel, segment {len(seg_idx) // 2 + 1} of "
            f"{len(seg_idx)} (the middle one), normalised per band",
        )
        explain.add_panel(cid, "input", "Log-mel spectrogram of the middle segment")
        shown = segment[0].numpy()
        standard_panel(
            shown, occ, f"Occlusion, {cid} ({case.outcome})", "drop in abnormal logit"
        )
        explain.add_panel(cid, "occlusion", "Occlusion over time-frequency patches")
        standard_panel(
            shown,
            ig_map,
            f"Integrated gradients, {cid} ({case.outcome})",
            "attribution",
        )
        explain.add_panel(cid, "integrated_gradients", "Integrated gradients")
        time_profile_panel(occ, ig_map, f"Attribution over time, {cid}")
        explain.add_panel(cid, "time_profile", "Both maps summed over mel bands")

    # Run-level figures.
    freqs = mel_centres_hz()
    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    for outcome, colour in (
        ("TP", "#b2182b"),
        ("FP", "#ef8a62"),
        ("FN", "#67a9cf"),
        ("TN", "#2166ac"),
    ):
        maps = [m for o, m in attr_rows if o == outcome]
        if maps:
            ax.plot(
                freqs,
                np.mean([m.sum(axis=1) for m in maps], axis=0),
                color=colour,
                lw=1.6,
                label=f"{outcome} (n={len(maps)})",
            )
    ax.axhline(0, color="#888888", lw=0.8)
    ax.set_xlabel("mel band centre (Hz)", fontsize=8)
    ax.set_ylabel("integrated gradients summed over time", fontsize=8)
    ax.set_title(
        "Attribution by frequency band, mean over the selected cases", fontsize=9
    )
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    explain.add_summary("Mean integrated gradients by frequency band")

    fig, ax = plt.subplots(figsize=(6.0, 3.0))
    labels = [f"{o} {c}" for o, c, _, _ in agreement_rows]
    overlap = [v for _, _, _, v in agreement_rows]
    ax.bar(range(len(labels)), overlap, color="#7a7a7a")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=60, ha="right", fontsize=6)
    ax.set_ylim(0, 1)
    ax.set_ylabel("overlap of the top 10% cells", fontsize=8)
    ax.set_title("Agreement of occlusion and integrated gradients", fontsize=9)
    ax.tick_params(labelsize=7)
    fig.tight_layout()
    explain.add_summary("Agreement between the two methods")

    pooled = float(np.mean(overlap)) if overlap else float("nan")
    explain.set_info(
        methods=["occlusion", "integrated_gradients", "time_profile"],
        target="abnormal logit of one 3-second log-mel segment per recording",
        notes=(
            f"Test split of {site}: {len(rec_y)} recordings, {int(rec_y.sum())} abnormal. "
            "Case score = mean abnormal probability over the recording's segments; "
            "outcome at 0.5. The explained segment is the one pushing the recording's "
            "score furthest towards the decision. Occlusion patches: 8 mel bands x 15 "
            "frames filled with the site mean; integrated gradients from the site mean "
            "(32 steps). Red supports 'abnormal'. The maps support review; they do not "
            "justify a prediction."
        ),
        metrics={
            "n_test_recordings": int(rec_y.size),
            "n_cases": len(chosen),
            "mean_top10pct_overlap": pooled,
        },
    )
    print(f"done in {time.perf_counter() - started:.1f} s")


main()
