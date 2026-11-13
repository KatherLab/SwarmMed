"""Data plots for the ehr_eicu_mortality template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script with a helper object ``visualization`` (``save_plot``, ``open``,
``listdir``, ``exists``) against the project's registered data and shows up to four plots.
They use the values before imputation and scaling (``X_raw``), all splits of a site together:

1. patients per site by outcome and split,
2. missing values per measured feature and site,
3. eight continuous features by site and outcome,
4. unit type, admission source and sex by site.

The data are either one folder per FLARE site (``fl-client-1/train.npz``, ...) or one site's
files at the data root, as written by prepare_data.py.
"""

import io
import json
import re

import matplotlib.pyplot as plt
import numpy as np

SPLITS = ("train", "val", "test")
ALIVE_COLOR, DIED_COLOR = "#4c72b0", "#c44e52"
CONTINUOUS = [  # (column, axis label)
    ("age", "Age (years)"),
    ("gcs_min", "Lowest GCS"),
    ("map_min", "Lowest mean arterial pressure (mm Hg)"),
    ("spo2_min", "Lowest SpO2 (%)"),
    ("creat_max", "Highest creatinine (mg/dl)"),
    ("bun_max", "Highest BUN (mg/dl)"),
    ("lactate_max", "Highest lactate (mmol/l)"),
    ("hr_max", "Highest heart rate (beats/min)"),
]
MEASURED = [  # the 30 features that can be missing (age and pre_icu_days are always known)
    "height_cm", "weight_kg", "hr_min", "hr_max", "hr_mean", "rr_min", "rr_max", "rr_mean",
    "spo2_min", "spo2_mean", "sbp_min", "sbp_mean", "map_min", "map_mean", "temp_min",
    "temp_max", "gcs_min", "creat_max", "bun_max", "na_min", "na_max", "k_max",
    "glucose_max", "hco3_min", "wbc_max", "hgb_min", "plt_min", "lactate_max", "bili_max",
    "albumin_min",
]  # fmt: skip
SHARES = [  # (column, label)
    ("male", "male"),
    ("unit_medsurg", "unit: med-surg ICU"),
    ("unit_medical", "unit: medical ICU"),
    ("unit_cardiac", "unit: cardiac ICU"),
    ("unit_surg_neuro", "unit: surgical / neuro ICU"),
    ("src_ed", "from: emergency dept."),
    ("src_or", "from: operating room"),
    ("src_ward", "from: ward"),
    ("src_other", "from: other"),
]
REGIONS = ("Midwest", "South", "West", "Northeast")


def natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def find_sites(keys):
    """Return {site label: key prefix}: site folders holding train.npz, else the data root."""
    folders = {
        k.split("/")[0] for k in keys if k.count("/") == 1 and k.endswith("/train.npz")
    }
    if folders:
        return {f: f + "/" for f in sorted(folders, key=natural_key)}
    if "train.npz" in keys:
        return {"data root": ""}
    return {}


def read_npz(path):
    with visualization.open(path, "rb") as handle:
        data = handle.read()
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {key: npz[key] for key in npz.files}


def load_site(label, prefix, keys):
    """All splits of a site together: dict with names, x_raw, y, split_of_case, tick label."""
    parts = [read_npz(f"{prefix}{split}.npz") for split in SPLITS]
    names = [str(n) for n in parts[0]["feature_names"]]
    region = None
    if f"{prefix}site_info.json" in keys:
        with visualization.open(f"{prefix}site_info.json", "r") as handle:
            region = json.load(handle).get("source_site")
    return {
        "names": names,
        "x": np.concatenate([p["X_raw"] for p in parts]).astype(float),
        "y": np.concatenate([p["y"] for p in parts]).astype(int),
        "split": np.concatenate(
            [[s] * len(p["y"]) for s, p in zip(SPLITS, parts, strict=True)]
        ),
        "tick": f"{label}\n({region})" if region else label,
    }


def column(site, name):
    return site["x"][:, site["names"].index(name)]


def plot_counts(sites):
    fig, ax = plt.subplots(figsize=(1.8 * len(sites) + 4.5, 4.6))
    width = 0.8 / len(SPLITS)
    for k, split in enumerate(SPLITS):
        for i, site in enumerate(sites):
            mask = site["split"] == split
            died = int(site["y"][mask].sum())
            alive = int(mask.sum()) - died
            x = i + (k - 1) * width
            ax.bar(x, alive, width * 0.92, color=ALIVE_COLOR, label="discharged alive")
            ax.bar(x, died, width * 0.92, bottom=alive, color=DIED_COLOR, label="died")
            ax.text(
                x,
                alive + died + 3,
                f"{died}",
                ha="center",
                fontsize=8,
                color=DIED_COLOR,
            )
            ax.text(x, -14, split, ha="center", va="top", fontsize=7.5)
    ax.set_xticks(range(len(sites)))
    ax.set_xticklabels([s["tick"] for s in sites])
    ax.tick_params(axis="x", pad=18)
    ax.set_ylabel("ICU stays (one per patient)")
    unique = dict(zip(*ax.get_legend_handles_labels()[::-1], strict=True))
    ax.legend(unique.values(), unique.keys(), frameon=False)
    ax.set_title(
        "Patients per site and split; red number = deaths in the split", fontsize=10
    )
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    visualization.save_plot("Patients per site by outcome and split")


def plot_missing(sites):
    present = [m for m in MEASURED if all(m in s["names"] for s in sites)]
    values = np.array(
        [[np.isnan(column(s, m)).mean() * 100 for s in sites] for m in present]
    )
    fig, ax = plt.subplots(figsize=(1.5 * len(sites) + 4.8, 0.27 * len(present) + 2))
    image = ax.imshow(values, aspect="auto", cmap="Blues", vmin=0, vmax=100)
    ax.set_xticks(range(len(sites)))
    ax.set_xticklabels([s["tick"] for s in sites])
    ax.set_yticks(range(len(present)))
    ax.set_yticklabels(present, fontsize=8)
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            if values[i, j] >= 1:
                color = "white" if values[i, j] > 55 else "black"
                ax.text(j, i, f"{values[i, j]:.0f}", ha="center", va="center",
                        fontsize=7, color=color)  # fmt: skip
    fig.colorbar(image, ax=ax, label="% of patients without a value in the first 24 h")
    ax.set_title("Missing values before imputation", fontsize=10)
    fig.tight_layout()
    visualization.save_plot("Missing values per feature and site")


def plot_continuous(sites):
    fig, axes = plt.subplots(2, 4, figsize=(14, 7.2))
    rng = np.random.default_rng(0)
    for ax, (name, label) in zip(axes.ravel(), CONTINUOUS, strict=True):
        for i, site in enumerate(sites):
            values = column(site, name)
            for outcome, color, shift in ((0, ALIVE_COLOR, -0.2), (1, DIED_COLOR, 0.2)):
                v = values[(site["y"] == outcome) & ~np.isnan(values)]
                if len(v) == 0:
                    continue
                ax.boxplot(v, positions=[i + shift], widths=0.3, showfliers=False,
                           medianprops={"color": "black"},
                           boxprops={"color": color}, whiskerprops={"color": color},
                           capprops={"color": color})  # fmt: skip
                if outcome == 1:  # few deaths: show every one
                    ax.scatter(i + shift + rng.uniform(-0.07, 0.07, len(v)), v, s=9,
                               color=color, alpha=0.7, zorder=3)  # fmt: skip
        ax.set_xticks(range(len(sites)))
        ax.set_xticklabels(
            [s["tick"].split("\n")[-1].strip("()") for s in sites], fontsize=8
        )
        ax.set_title(label, fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0, 0].plot([], [], color=ALIVE_COLOR, label="discharged alive")
    axes[0, 0].plot([], [], color=DIED_COLOR, label="died")
    axes[0, 0].legend(frameon=False, fontsize=8, loc="lower left")
    fig.suptitle("First 24 h by site and outcome (before imputation; boxes without outliers, "
                 "dots = every death)", fontsize=10)  # fmt: skip
    fig.tight_layout()
    visualization.save_plot("Continuous features by site and outcome")


def plot_shares(sites):
    values = np.array(
        [[np.nanmean(column(s, c)) * 100 for s in sites] for c, _ in SHARES]
    )
    fig, ax = plt.subplots(figsize=(1.5 * len(sites) + 4.6, 0.45 * len(SHARES) + 2))
    image = ax.imshow(values, aspect="auto", cmap="Blues", vmin=0, vmax=100)
    ax.set_xticks(range(len(sites)))
    ax.set_xticklabels([s["tick"] for s in sites])
    ax.set_yticks(range(len(SHARES)))
    ax.set_yticklabels([label for _, label in SHARES])
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            color = "white" if values[i, j] > 55 else "black"
            ax.text(j, i, f"{values[i, j]:.0f}", ha="center", va="center", fontsize=8,
                    color=color)  # fmt: skip
    fig.colorbar(image, ax=ax, label="% of the site's patients")
    ax.set_title("Case mix by site", fontsize=10)
    fig.tight_layout()
    visualization.save_plot("Case mix by site")


keys = set(visualization.listdir(""))
found = find_sites(keys)
if not found:
    raise RuntimeError(
        "No train.npz found, neither in site folders (fl-client-1/train.npz, ...) nor at the "
        "data root. Import the output of prepare_data.py."
    )
loaded = [load_site(label, prefix, keys) for label, prefix in found.items()]
for plot in (plot_counts, plot_missing, plot_continuous, plot_shares):
    plot(loaded)
