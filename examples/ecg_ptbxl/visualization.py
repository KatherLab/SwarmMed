"""Data plots for the ecg_ptbxl template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script with a helper object ``visualization`` (``save_plot``, ``open``,
``listdir``, ``exists``) against the project's registered data and shows up to four plots:

1. records per site and split, by label (normal or abnormal),
2. the share of each site's records that carry each diagnostic superclass (NORM, MI, STTC, CD, HYP),
3. one normal and one abnormal 12-lead record from the first site's test split,
4. the typical signal amplitude (median over records of each lead's standard deviation) per
   site and lead, from the validation and test splits.

The data are either one folder per FLARE site (``fl-client-1/train.npz``, ...) or one site's
files at the data root, as written by prepare_data.py. Only the label arrays are read from
train.npz.
"""

import io
import re

import matplotlib.pyplot as plt
import numpy as np

SPLITS = ("train", "val", "test")
SUPERCLASSES = ["NORM", "MI", "STTC", "CD", "HYP"]
LEADS = ["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]
FS = 100
CLASS_COLOURS = {0: "#6250d6", 1: "#e34948"}  # normal, abnormal
CLASS_LABELS = {0: "normal", 1: "abnormal"}
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


def natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def find_sites(keys):
    folders = {
        k.split("/")[0] for k in keys if k.count("/") == 1 and k.endswith("/train.npz")
    }
    if folders:
        return {f: f + "/" for f in sorted(folders, key=natural_key)}
    return {"data root": ""} if "train.npz" in keys else {}


def read_arrays(path, names):
    with visualization.open(path, "rb") as handle:
        data = handle.read()
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {name: npz[name] for name in names}


def lead_std(X, chunk=512):
    """Standard deviation over time of every record and lead, in millivolts (N, 12)."""
    out = []
    for start in range(0, len(X), chunk):
        out.append(X[start : start + chunk].astype(np.float32).std(axis=2) / 1000.0)
    return np.concatenate(out)


def main():
    keys = set(visualization.listdir(""))
    sites = find_sites(keys)
    if not sites:
        raise FileNotFoundError(
            "No train.npz in the project data; run prepare_data.py."
        )
    labels = list(sites)

    counts, superclass_share, stds, example = {}, {}, {}, {}
    for label, prefix in sites.items():
        counts[label] = {}
        y_all, super_all, std_parts = [], [], []
        for split in SPLITS:
            names = ("y", "y_super") if split == "train" else ("X", "y", "y_super")
            arrays = read_arrays(f"{prefix}{split}.npz", names)
            y = arrays["y"].astype(int)
            counts[label][split] = [int((y == 0).sum()), int((y == 1).sum())]
            y_all.append(y)
            super_all.append(arrays["y_super"])
            if split != "train":
                std_parts.append(lead_std(arrays["X"]))
            if split == "test" and not example:
                X = arrays["X"]
                chosen = {}
                for cls in (0, 1):
                    # the record whose signal level is closest to the site's median, per class
                    index = np.flatnonzero(y == cls)
                    level = np.median(lead_std(X[index]), axis=1)
                    chosen[cls] = (
                        int(index[np.argmin(np.abs(level - np.median(level)))]),
                        X,
                    )
                example = {
                    "site": label,
                    "records": {
                        cls: chosen[cls][1][chosen[cls][0]].astype(np.float32) / 1000.0
                        for cls in (0, 1)
                    },
                    "super": {cls: arrays["y_super"][chosen[cls][0]] for cls in (0, 1)},
                }
        y_super = np.concatenate(super_all)
        superclass_share[label] = y_super.mean(axis=0)
        stds[label] = np.median(np.concatenate(std_parts), axis=0)
        del y_all, super_all
    n_sites = len(labels)

    # 1. Records per site and split, by label
    fig, axes = plt.subplots(1, len(SPLITS), figsize=(11, 3.8), sharey=False)
    x = np.arange(n_sites)
    for ax, split in zip(axes, SPLITS, strict=True):
        normal = np.array([counts[s][split][0] for s in labels])
        abnormal = np.array([counts[s][split][1] for s in labels])
        ax.bar(x, normal, color=CLASS_COLOURS[0], label=CLASS_LABELS[0], width=0.7)
        ax.bar(
            x,
            abnormal,
            bottom=normal,
            color=CLASS_COLOURS[1],
            label=CLASS_LABELS[1],
            width=0.7,
        )
        for i in range(n_sites):
            ax.text(
                i,
                normal[i] + abnormal[i],
                str(normal[i] + abnormal[i]),
                ha="center",
                va="bottom",
                fontsize=8,
            )
        ax.set_xticks(x, [s.replace("fl-client-", "site ") for s in labels], fontsize=8)
        ax.set_title(f"{split} split", loc="left", fontsize=10)
        ax.yaxis.grid(True)
        ax.set_axisbelow(True)
        ax.set_ylim(0, max((normal + abnormal).max() * 1.12, 1))
    axes[0].set_ylabel("records")
    axes[0].legend(frameon=False, fontsize=8, loc="upper right")
    fig.suptitle("Records per site and split, by label", x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    visualization.save_plot("Records per site and split")
    plt.close("all")

    # 2. Superclass shares per site
    share = np.array([superclass_share[s] for s in labels]) * 100
    fig, ax = plt.subplots(figsize=(7.5, 0.7 * n_sites + 1.8))
    image = ax.imshow(
        share, cmap="Blues", vmin=0, vmax=max(share.max(), 1), aspect="auto"
    )
    for (i, j), value in np.ndenumerate(share):
        ax.text(
            j,
            i,
            f"{value:.0f}%",
            ha="center",
            va="center",
            color="#ffffff" if value > share.max() / 2 else INK,
        )
    ax.set_xticks(range(len(SUPERCLASSES)), SUPERCLASSES)
    ax.set_yticks(range(n_sites), labels)
    ax.spines[:].set_visible(False)
    fig.colorbar(image, ax=ax, label="% of the site's records", fraction=0.04, pad=0.02)
    ax.set_title(
        "Diagnostic superclasses per site (all splits; a record can have several)",
        loc="left",
        fontsize=11,
    )
    fig.tight_layout()
    visualization.save_plot("Superclasses per site")
    plt.close("all")

    # 3. One normal and one abnormal record
    fig, axes = plt.subplots(len(LEADS), 2, figsize=(11, 8.6), sharex=True)
    time = np.arange(example["records"][0].shape[1]) / FS
    for col, cls in enumerate((0, 1)):
        record = example["records"][cls]
        present = [
            name
            for name, flag in zip(SUPERCLASSES, example["super"][cls], strict=True)
            if flag
        ]
        limit = max(float(np.abs(record).max()), 0.5)
        for row, lead in enumerate(LEADS):
            ax = axes[row, col]
            ax.plot(
                time,
                record[row] - np.median(record[row]),
                color=CLASS_COLOURS[cls],
                linewidth=0.8,
            )
            ax.set_ylim(-limit, limit)
            ax.set_yticks([])
            ax.spines[["left", "bottom"]].set_visible(False)
            ax.tick_params(left=False, bottom=False)
            ax.text(
                -0.01,
                0.5,
                lead,
                transform=ax.transAxes,
                ha="right",
                va="center",
                fontsize=8,
                color=INK_2,
            )
        axes[0, col].set_title(
            f"{CLASS_LABELS[cls]} ({', '.join(present)}), {example['site']}",
            loc="left",
            fontsize=10,
        )
        axes[-1, col].tick_params(bottom=True)
        axes[-1, col].set_xlabel("seconds")
    fig.suptitle(
        f"Two test records at {example['site']} (median-centred, same scale within a record)",
        x=0.01,
        ha="left",
        fontsize=11,
    )
    fig.tight_layout()
    visualization.save_plot("Example records")
    plt.close("all")

    # 4. Typical amplitude per site and lead
    level = np.array([stds[s] for s in labels])
    fig, ax = plt.subplots(figsize=(10, 0.6 * n_sites + 1.8))
    image = ax.imshow(level, cmap="Blues", vmin=0, aspect="auto")
    for (i, j), value in np.ndenumerate(level):
        ax.text(
            j,
            i,
            f"{value:.2f}",
            ha="center",
            va="center",
            fontsize=8,
            color="#ffffff" if value > level.max() / 2 else INK,
        )
    ax.set_xticks(range(len(LEADS)), LEADS)
    ax.set_yticks(range(n_sites), labels)
    ax.spines[:].set_visible(False)
    fig.colorbar(image, ax=ax, label="mV", fraction=0.03, pad=0.02)
    ax.set_title(
        "Typical signal level: median standard deviation per lead (validation and test)",
        loc="left",
        fontsize=11,
    )
    fig.tight_layout()
    visualization.save_plot("Signal level per lead and site")
    plt.close("all")


main()
