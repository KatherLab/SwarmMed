"""Data plots for the heart sound template, run by SwarmMedHub in its sandbox.

SwarmMedHub provides the `visualization` helper (save_plot, open, listdir, exists;
at most 4 plots). The registered data holds one site's files at the root or one
folder per site (fl-client-1/, ...), as written by prepare_data.py. Plots:
  1. recordings per split and class, per site (from site_info.json);
  2. recording durations by class, per site (all splits);
  3. mean log-mel band profile by class, per site (training split, before the
     per-site normalisation that training applies);
  4. one normal and one abnormal training segment of the first site.
"""

import io
import json

import matplotlib.pyplot as plt
import numpy as np

SPLITS = ("train", "val", "test")
COLOURS = {0: "#4C72B0", 1: "#DD8452"}  # normal, abnormal
NAMES = {0: "normal", 1: "abnormal"}
FRAMES_PER_SECOND = 50  # 20 ms hop (prepare_data.py)


def read_bytes(path):
    """Read a registered file completely."""
    with visualization.open(path, "rb") as handle:
        return handle.read()


def find_sites(keys):
    """Site folder prefixes ('' for root files) with site_info.json, holdouts excluded."""
    prefixes = {
        k[: -len("site_info.json")]
        for k in keys
        if k.endswith("site_info.json") and not k.startswith(("results/", "holdout-"))
    }
    prefixes = [
        p for p in prefixes if p == "" or (p.endswith("/") and p.count("/") == 1)
    ]

    def order(prefix):
        tail = prefix.rstrip("/").rsplit("-", 1)[-1]
        return (prefix != "", int(tail) if tail.isdigit() else 0, prefix)

    return sorted(prefixes, key=order)


def site_grid(n):
    """A row of panels, one per site."""
    fig, axes = plt.subplots(1, n, figsize=(4.2 * n, 3.8), squeeze=False)
    return fig, axes[0]


sites = find_sites(visualization.listdir(""))
if not sites:
    print("No site_info.json found; run prepare_data.py and import its output.")
else:
    infos, durations, profiles, examples = {}, {}, {}, None
    for prefix in sites:
        info = json.loads(read_bytes(prefix + "site_info.json").decode("utf-8"))
        label = f"{prefix.rstrip('/') or 'site'} ({info.get('source', '?')})"
        infos[label] = info
        durations[label] = {0: [], 1: []}
        sums = {0: np.zeros(32), 1: np.zeros(32)}
        frames = {0: 0, 1: 0}
        for split in SPLITS:
            if not visualization.exists(prefix + f"{split}.npz"):
                continue
            with np.load(
                io.BytesIO(read_bytes(prefix + f"{split}.npz")), allow_pickle=False
            ) as z:
                rec_y, rec_dur = z["rec_y"], z["rec_dur_s"]
                for c in (0, 1):
                    durations[label][c].extend(rec_dur[rec_y == c].tolist())
                if split != "train":
                    continue
                x, y = z["x"], z["y"]
                for start in range(0, len(y), 1000):  # chunks keep memory low
                    xb = x[start : start + 1000].astype(np.float64)
                    yb = y[start : start + 1000]
                    for c in (0, 1):
                        sums[c] += xb[yb == c].sum(axis=(0, 2))
                        frames[c] += int((yb == c).sum()) * xb.shape[2]
                if examples is None and set(np.unique(y)) == {0, 1}:
                    rec_ids, rec = z["rec_ids"], z["rec"]
                    first = {c: int(np.flatnonzero(y == c)[0]) for c in (0, 1)}
                    examples = {
                        c: (
                            x[i].astype(np.float32),
                            str(rec_ids[rec[i]]),
                            float(z["seg_start_s"][i]),
                            label,
                        )
                        for c, i in first.items()
                    }
                del x, y
        profiles[label] = {c: sums[c] / frames[c] for c in (0, 1) if frames[c] > 0}

    labels = list(infos)

    # 1. Recordings per split and class (stacked: normal below, abnormal on top)
    fig, axes = site_grid(len(labels))
    for ax, label in zip(axes, labels, strict=True):
        splits = infos[label].get("splits", {})
        names = [s for s in SPLITS if s in splits]
        abnormal = np.array([splits[s]["recordings_abnormal"] for s in names])
        normal = np.array([splits[s]["recordings"] for s in names]) - abnormal
        ax.bar(names, normal, color=COLOURS[0], label="normal")
        ax.bar(names, abnormal, bottom=normal, color=COLOURS[1], label="abnormal")
        for i, (n0, n1) in enumerate(zip(normal, abnormal, strict=True)):
            ax.text(i, n0 + n1, f"{n0}/{n1}", ha="center", va="bottom", fontsize=8)
        ax.set_title(label, fontsize=10)
        ax.set_ylabel("recordings")
    axes[0].legend(fontsize=8)
    fig.suptitle("Recordings per split (normal/abnormal)")
    fig.tight_layout()
    visualization.save_plot("Recordings per split and class")
    plt.close("all")

    # 2. Recording durations by class
    fig, axes = site_grid(len(labels))
    for ax, label in zip(axes, labels, strict=True):
        values = durations[label]
        all_values = values[0] + values[1]
        if not all_values:
            continue
        bins = np.linspace(0, max(all_values) + 1, 30)
        for c in (0, 1):
            ax.hist(
                values[c],
                bins=bins,
                alpha=0.6,
                color=COLOURS[c],
                label=f"{NAMES[c]} (n={len(values[c])})",
            )
        ax.set_title(label, fontsize=10)
        ax.set_xlabel("duration (s)")
        ax.set_ylabel("recordings")
        ax.legend(fontsize=8)
    fig.suptitle("Recording duration by class (all splits)")
    fig.tight_layout()
    visualization.save_plot("Recording duration by class")
    plt.close("all")

    # 3. Mean log-mel band profile by class (training split, not normalised)
    fig, axes = site_grid(len(labels))
    for ax, label in zip(axes, labels, strict=True):
        for c, profile in profiles[label].items():
            ax.plot(np.arange(1, 33), profile, color=COLOURS[c], label=NAMES[c])
        ax.set_title(label, fontsize=10)
        ax.set_xlabel("mel band (25-800 Hz)")
        ax.set_ylabel("mean log-mel power")
        ax.legend(fontsize=8)
    fig.suptitle("Mean log-mel band profile by class (training split)")
    fig.tight_layout()
    visualization.save_plot("Mean log-mel profile by class")
    plt.close("all")

    # 4. Example segments
    if examples:
        fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), squeeze=False)
        low = min(
            float(examples[c][0].min()) for c in (0, 1)
        )  # one colour scale for both
        high = max(float(examples[c][0].max()) for c in (0, 1))
        for ax, c in zip(axes[0], (0, 1), strict=True):
            seg, record, start, label = examples[c]
            image = ax.imshow(
                seg,
                origin="lower",
                aspect="auto",
                cmap="magma",
                vmin=low,
                vmax=high,
                extent=(start, start + seg.shape[1] / FRAMES_PER_SECOND, 0.5, 32.5),
            )
            ax.set_title(f"{NAMES[c]}: {record} from {start:.1f} s", fontsize=10)
            ax.set_xlabel("time in recording (s)")
            ax.set_ylabel("mel band")
            fig.colorbar(image, ax=ax, label="log-mel power")
        fig.suptitle(f"Example training segments, {examples[0][3]}")
        fig.tight_layout()
        visualization.save_plot("Example log-mel segments")
        plt.close("all")
