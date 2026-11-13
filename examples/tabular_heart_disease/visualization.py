"""Data plots for the tabular_heart_disease template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script with a helper object ``visualization`` (``save_plot``, ``open``,
``listdir``, ``exists``) against the project's registered data and shows up to four plots.
They use the values before imputation and scaling (``X_raw``), all splits together:

1. patients per site by diagnosis,
2. missing values per feature and site,
3. continuous features by site and diagnosis,
4. binary and categorical features by site.

The data are either one folder per FLARE site (``fl-client-1/train.npz``, ...) or one site's
files at the data root, as written by prepare_data.py.
"""

import io
import json
import re

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

SPLITS = ("train", "val", "test")
CONTINUOUS = [
    ("age", "Age (years)"),
    ("trestbps", "Resting blood pressure (mm Hg)"),
    ("chol", "Serum cholesterol (mg/dl)"),
    ("thalach", "Maximum heart rate (beats/min)"),
    ("oldpeak", "ST depression, exercise vs rest"),
]
# Variables as recorded (one row per variable in the missing-value plot).
VARIABLES = [
    ("age", "age"),
    ("trestbps", "resting blood pressure"),
    ("chol", "serum cholesterol"),
    ("thalach", "maximum heart rate"),
    ("oldpeak", "ST depression"),
    ("sex", "sex"),
    ("fbs", "fasting blood sugar > 120"),
    ("exang", "exercise-induced angina"),
    ("cp_1", "chest pain type"),
    ("restecg_0", "resting ECG"),
]
SHARES = [
    ("sex", "male sex"),
    ("fbs", "fasting blood sugar > 120 mg/dl"),
    ("exang", "exercise-induced angina"),
    ("cp_1", "chest pain: typical angina"),
    ("cp_2", "chest pain: atypical angina"),
    ("cp_3", "chest pain: non-anginal"),
    ("cp_4", "chest pain: asymptomatic"),
    ("restecg_0", "resting ECG: normal"),
    ("restecg_1", "resting ECG: ST-T abnormality"),
    ("restecg_2", "resting ECG: LV hypertrophy"),
]
SITE_NAMES = {
    "cleveland": "Cleveland",
    "hungarian": "Budapest",
    "va_long_beach": "Long Beach VA",
    "switzerland": "Switzerland",
}

# Colours: sites in a fixed categorical order, the two classes in two further hues, and one
# blue ramp for magnitudes. Text stays in neutral ink.
SITE_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
CLASS_COLOURS = {0: "#6250d6", 1: "#e34948"}  # no disease, disease
CLASS_LABELS = {0: "no disease (num = 0)", 1: "disease (num > 0)"}
INK, INK_2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
RAMP = LinearSegmentedColormap.from_list(
    "blue_ramp",
    [
        "#fcfcfb",
        "#cde2fb",
        "#9ec5f4",
        "#6da7ec",
        "#3987e5",
        "#256abf",
        "#184f95",
        "#0d366b",
    ],
)

plt.rcParams.update(
    {
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": "#c3c2b7",
        "axes.linewidth": 0.8,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": False,
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


def read_npz(path):
    with visualization.open(path, "rb") as handle:
        data = handle.read()
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {key: npz[key] for key in npz.files}


def load_site(prefix, keys):
    """X_raw, y and feature names of all splits of one site, plus a display label."""
    parts = [
        read_npz(f"{prefix}{split}.npz")
        for split in SPLITS
        if f"{prefix}{split}.npz" in keys
    ]
    names = [str(n) for n in parts[0]["feature_names"]]
    source = None
    if f"{prefix}site_info.json" in keys:
        with visualization.open(f"{prefix}site_info.json", "r") as handle:
            source = json.load(handle).get("source_site")
    return {
        "x": np.concatenate([p["X_raw"] for p in parts]).astype(float),
        "y": np.concatenate([p["y"] for p in parts]).astype(int),
        "column": {name: j for j, name in enumerate(names)},
        "source": SITE_NAMES.get(source, source),
    }


def site_tick(label, site):
    return f"{label}\n{site['source']}" if site["source"] else label


def heatmap(ax, values, row_labels, col_labels, title):
    """Annotated heatmap of percentages (0-100) on the blue ramp; NaN cells left blank."""
    image = ax.imshow(
        np.nan_to_num(values, nan=0.0), cmap=RAMP, vmin=0, vmax=100, aspect="auto"
    )
    ax.set_xticks(range(len(col_labels)), col_labels)
    ax.set_yticks(range(len(row_labels)), row_labels)
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            if np.isnan(values[i, j]):
                ax.text(j, i, "n/a", ha="center", va="center", fontsize=8, color=MUTED)
            else:
                colour = "#ffffff" if values[i, j] > 55 else INK
                if values[i, j] == 0:
                    colour = MUTED
                ax.text(
                    j,
                    i,
                    f"{values[i, j]:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=8,
                    color=colour,
                )
    ax.set_title(title, loc="left", fontsize=11)
    return image


keys = set(visualization.listdir(""))
site_prefixes = find_sites(keys)
if not site_prefixes:
    print(
        "No train.npz found in site folders or at the data root; run prepare_data.py first."
    )
else:
    sites = {label: load_site(prefix, keys) for label, prefix in site_prefixes.items()}
    labels = list(sites)
    ticks = [site_tick(label, sites[label]) for label in labels]
    sizes = ", ".join(
        f"{label} ({len(site['y'])} patients)" for label, site in sites.items()
    )
    print(f"Loaded {len(sites)} site(s): {sizes}")

    # 1. Patients per site by diagnosis -------------------------------------------------------
    fig, ax = plt.subplots(figsize=(8, 4.5))
    neg = np.array([(sites[s]["y"] == 0).sum() for s in labels])
    pos = np.array([(sites[s]["y"] == 1).sum() for s in labels])
    xs = np.arange(len(labels))
    ax.bar(
        xs,
        neg,
        width=0.6,
        color=CLASS_COLOURS[0],
        label=CLASS_LABELS[0],
        edgecolor="#fcfcfb",
        linewidth=2,
    )
    ax.bar(
        xs,
        pos,
        width=0.6,
        bottom=neg,
        color=CLASS_COLOURS[1],
        label=CLASS_LABELS[1],
        edgecolor="#fcfcfb",
        linewidth=2,
    )
    for x, n_neg, n_pos in zip(xs, neg, pos, strict=True):
        total = n_neg + n_pos
        ax.text(
            x,
            total + max(neg + pos) * 0.02,
            f"n = {total}, {n_pos / total:.0%} disease",
            ha="center",
            va="bottom",
            fontsize=9,
            color=INK_2,
        )
    ax.set_xticks(xs, ticks)
    ax.set_ylabel("patients (all splits)")
    ax.set_ylim(0, max(neg + pos) * 1.15)
    ax.yaxis.grid(True)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper right")
    ax.set_title("Patients per site by diagnosis", loc="left", fontsize=11)
    fig.tight_layout()
    visualization.save_plot("Patients per site by diagnosis")
    plt.close("all")

    # 2. Missing values per feature and site (before imputation) ------------------------------
    values = np.array(
        [
            [
                100 * np.isnan(sites[s]["x"][:, sites[s]["column"][name]]).mean()
                for s in labels
            ]
            for name, _ in VARIABLES
        ]
    )
    fig, ax = plt.subplots(figsize=(1.6 * len(labels) + 3.2, 5))
    image = heatmap(
        ax,
        values,
        [label for _, label in VARIABLES],
        ticks,
        "Missing values before imputation (% of patients)",
    )
    fig.colorbar(image, ax=ax, fraction=0.04, pad=0.02).outline.set_visible(False)
    fig.tight_layout()
    visualization.save_plot("Missing values per feature and site")
    plt.close("all")

    # 3. Continuous features by site and diagnosis --------------------------------------------
    fig, axes = plt.subplots(1, len(CONTINUOUS), figsize=(3.1 * len(CONTINUOUS), 4.4))
    for ax, (name, title) in zip(axes, CONTINUOUS, strict=True):
        for i, s in enumerate(labels):
            col = sites[s]["x"][:, sites[s]["column"][name]]
            for cls, offset in ((0, -0.18), (1, 0.18)):
                data = col[(sites[s]["y"] == cls) & ~np.isnan(col)]
                if len(data) == 0:
                    continue
                box = ax.boxplot(
                    data,
                    positions=[i + offset],
                    widths=0.3,
                    patch_artist=True,
                    showfliers=True,
                    flierprops={"markersize": 2, "markeredgecolor": MUTED},
                )
                for patch in box["boxes"]:
                    patch.set(
                        facecolor=CLASS_COLOURS[cls],
                        edgecolor=CLASS_COLOURS[cls],
                        alpha=0.75,
                    )
                for part in ("whiskers", "caps", "medians"):
                    for line in box[part]:
                        line.set(
                            color=INK_2 if part != "medians" else "#ffffff", linewidth=1
                        )
            if np.isnan(col).all():
                ax.text(
                    i,
                    0.5,
                    "not recorded",
                    transform=ax.get_xaxis_transform(),
                    rotation=90,
                    ha="center",
                    va="center",
                    fontsize=8,
                    color=MUTED,
                )
        ax.set_xticks(
            range(len(labels)),
            [t.split("\n")[-1] for t in ticks],
            rotation=45,
            ha="right",
        )
        ax.set_xlim(-0.6, len(labels) - 0.4)
        ax.set_title(title, fontsize=9, loc="left")
        ax.yaxis.grid(True)
        ax.set_axisbelow(True)
    handles = [plt.Rectangle((0, 0), 1, 1, color=CLASS_COLOURS[c]) for c in (0, 1)]
    fig.legend(
        handles,
        [CLASS_LABELS[0], CLASS_LABELS[1]],
        frameon=False,
        loc="upper right",
        ncol=2,
    )
    fig.suptitle(
        "Continuous features by site and diagnosis (values as recorded)",
        x=0.01,
        ha="left",
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    visualization.save_plot("Continuous features by site and diagnosis")
    plt.close("all")

    # 4. Binary and categorical features by site ----------------------------------------------
    values = np.array(
        [
            [
                100 * np.nanmean(sites[s]["x"][:, sites[s]["column"][name]])
                if (~np.isnan(sites[s]["x"][:, sites[s]["column"][name]])).any()
                else np.nan
                for s in labels
            ]
            for name, _ in SHARES
        ]
    )
    fig, ax = plt.subplots(figsize=(1.6 * len(labels) + 3.6, 5.2))
    image = heatmap(
        ax,
        values,
        [label for _, label in SHARES],
        ticks,
        "Binary and categorical features (% of patients with a recorded value)",
    )
    fig.colorbar(image, ax=ax, fraction=0.04, pad=0.02).outline.set_visible(False)
    fig.tight_layout()
    visualization.save_plot("Binary and categorical features by site")
    plt.close("all")
    print("Saved 4 plots.")
