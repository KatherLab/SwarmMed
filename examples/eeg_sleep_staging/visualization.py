"""Data plots for the EEG sleep-staging template (run by SwarmMedHub in its sandbox).

The sandbox defines the object `visualization` (save_plot, open, listdir,
exists); at most four plots are kept:

1. Epochs per sleep stage for every site and split (from site_info.json).
2. One example 30-s epoch per stage, both channels (first site, validation subject).
3. Mean power spectrum per stage and channel (validation splits).
4. Hypnogram of the first site's validation subject.

Only the small validation files are read (at most about 22 MB each).
"""

import io
import json

import matplotlib.pyplot as plt
import numpy as np

CLASS_NAMES = ["W", "N1", "N2", "N3", "REM"]
STAGE_COLOURS = ["#4C72B0", "#DD8452", "#55A868", "#8172B3", "#C44E52"]
CHANNELS = ["Fpz-Cz", "Pz-Oz"]
SPLITS = ["train", "val", "test"]
FS = 100
SITE_FILES = {"train.npz", "val.npz", "test.npz", "site_info.json"}


def site_locations(keys):
    """Return [(label, prefix)] for the root and each top-level folder with site files."""
    locations = [("data root", "")] if any(k in SITE_FILES for k in keys) else []
    folders = sorted(
        {
            k.split("/")[0]
            for k in keys
            if k.count("/") == 1 and k.split("/")[1] in SITE_FILES
        }
    )
    return locations + [(folder, folder + "/") for folder in folders]


def read_arrays(path):
    """Read one .npz file completely, then return its arrays."""
    with visualization.open(path, "rb") as handle:
        raw = handle.read()
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def stage_counts(prefix):
    """{split: [count per stage]} from site_info.json, else from the label arrays."""
    if visualization.exists(prefix + "site_info.json"):
        with visualization.open(prefix + "site_info.json", "r") as handle:
            counts = json.load(handle).get("counts", {})
        if all(split in counts for split in SPLITS):
            return {s: [counts[s][c] for c in CLASS_NAMES] for s in SPLITS}
    result = {}
    for split in SPLITS:
        if visualization.exists(f"{prefix}{split}.npz"):
            y = read_arrays(f"{prefix}{split}.npz")["y"].astype(np.int64)
            result[split] = np.bincount(y, minlength=len(CLASS_NAMES)).tolist()
    return result


def save(title):
    """Save the current figure as one Hub plot and free it."""
    plt.tight_layout()
    visualization.save_plot(title)
    plt.close("all")


locations = site_locations(visualization.listdir(""))
if not locations:
    raise SystemExit("No prepared site data found (train.npz, val.npz, test.npz).")
first_label, first_prefix = locations[0]

# 1. Stage counts per site and split.
bars = [
    (label, split, counts)
    for label, prefix in locations
    for split, counts in stage_counts(prefix).items()
]
fig, ax = plt.subplots(figsize=(max(8, 0.7 * len(bars) + 3), 5))
positions = np.arange(len(bars))
bottom = np.zeros(len(bars))
for k, stage in enumerate(CLASS_NAMES):
    values = np.array([counts[k] for _, _, counts in bars], dtype=float)
    ax.bar(positions, values, bottom=bottom, color=STAGE_COLOURS[k], label=stage)
    bottom += values
for x, total in zip(positions, bottom, strict=True):
    ax.text(x, total, f"{int(total)}", ha="center", va="bottom", fontsize=8)
ax.set_xticks(positions)
ax.set_xticklabels([f"{label}\n{split}" for label, split, _ in bars], fontsize=8)
ax.set_ylabel("30-s epochs")
ax.set_title("Epochs per sleep stage, by site and split")
ax.legend(title="Stage", ncol=5, fontsize=8, loc="upper right")
save("Epochs per sleep stage, by site and split")

# 2. One example epoch per stage (first site, validation subject).
val = read_arrays(f"{first_prefix}val.npz")
time_s = np.arange(val["X"].shape[-1]) / FS
fig, axes = plt.subplots(len(CLASS_NAMES), 1, figsize=(10, 9), sharex=True)
for k, (ax, stage) in enumerate(zip(axes, CLASS_NAMES, strict=True)):
    indices = np.flatnonzero(val["y"] == k)
    if len(indices) == 0:
        ax.text(0.5, 0.5, f"no {stage} epoch", transform=ax.transAxes, ha="center")
    else:
        signal = val["X"][indices[len(indices) // 2]].astype(np.float32)
        for c, channel in enumerate(CHANNELS):
            ax.plot(time_s, signal[c] - 6 * c, lw=0.5, label=channel)
        ax.set_yticks([])
    ax.set_ylabel(stage, rotation=0, labelpad=20, fontsize=11)
axes[0].legend(loc="upper right", fontsize=8, ncol=2)
axes[-1].set_xlabel("Time within the epoch (s)")
subject = int(val["subject"][0]) if len(val["subject"]) else -1
fig.suptitle(
    f"Example epoch per stage ({first_label}, validation subject {subject}; "
    "robust-scaled, channels offset)"
)
save("Example 30-s epoch per sleep stage")

# 3. Mean power spectrum per stage and channel (validation splits).
window = np.hanning(val["X"].shape[-1]).astype(np.float32)
freqs = np.fft.rfftfreq(val["X"].shape[-1], d=1.0 / FS)
power = np.zeros((len(CLASS_NAMES), len(CHANNELS), len(freqs)))
counts = np.zeros(len(CLASS_NAMES))
for _label, prefix in locations:
    arrays = val if prefix == first_prefix else read_arrays(f"{prefix}val.npz")
    for start in range(0, len(arrays["y"]), 512):
        block = arrays["X"][start : start + 512].astype(np.float32) * window
        spectrum = np.abs(np.fft.rfft(block, axis=-1)) ** 2
        labels = arrays["y"][start : start + 512]
        for k in range(len(CLASS_NAMES)):
            power[k] += spectrum[labels == k].sum(axis=0)
            counts[k] += np.count_nonzero(labels == k)
band = (freqs >= 0.3) & (freqs <= 35)
fig, axes = plt.subplots(1, len(CHANNELS), figsize=(11, 4.5), sharey=True)
for c, (ax, channel) in enumerate(zip(axes, CHANNELS, strict=True)):
    for k, stage in enumerate(CLASS_NAMES):
        if counts[k]:
            mean_db = 10 * np.log10(power[k, c, band] / counts[k])
            ax.plot(
                freqs[band],
                mean_db,
                color=STAGE_COLOURS[k],
                lw=1.2,
                label=f"{stage} (n={int(counts[k])})",
            )
    ax.set_title(channel)
    ax.set_xlabel("Frequency (Hz)")
    ax.grid(True, alpha=0.3)
axes[0].set_ylabel("Mean power (dB, robust-scaled signal)")
axes[-1].legend(fontsize=8)
fig.suptitle("Mean power spectrum per sleep stage (validation subjects of all sites)")
save("Mean power spectrum per sleep stage")

# 4. Hypnogram of the first site's validation subject.
order = [0, 4, 1, 2, 3]  # plotted top to bottom: W, REM, N1, N2, N3
level = {stage: len(order) - 1 - i for i, stage in enumerate(order)}
hours = val["epoch"].astype(np.float64) * 30 / 3600
fig, ax = plt.subplots(figsize=(11, 3.8))
ax.step(hours, [level[int(v)] for v in val["y"]], where="post", lw=0.8, color="black")
ax.set_yticks([level[k] for k in order])
ax.set_yticklabels([CLASS_NAMES[k] for k in order])
ax.set_xlabel("Hours since the start of the recording")
ax.set_title(
    f"Hypnogram, {first_label}, validation subject {subject} "
    f"({len(val['y'])} scored epochs)"
)
ax.grid(True, axis="x", alpha=0.3)
save("Hypnogram of the validation subject")
