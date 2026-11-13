"""Per-case explanations for the eICU mortality template, run by SwarmMedHub in its sandbox.

SwarmMedHub provides the `explain` helper (get_model, load_weights, open, exists, listdir,
add_case, add_panel, add_summary, set_info) with the project data and the job's result files
("results/<site>/.../FL_global_model.pt"), and the package `swarmmed_explain`. The script

  1. loads the final global model and the test split (and the training split, for the
     reference patient) of the first site folder it finds, or of the root-level files;
  2. scores the test split and selects 2 patients per outcome (TP, FP, FN, TN) with
     swarmmed_explain.select_cases, the most confident ones per outcome; "positive" means a
     predicted probability of death of at least ALERT_THRESHOLD;
  3. shows each selected patient's recorded values first (a table without any model output:
     the panel that a blinded review shows before the reader saves a read), then explains
     the patient's logit of death with two methods, relative to a
     reference patient (for every input the median of the site's training split, i.e. the
     typical training patient after imputation and scaling):
       - feature ablation: the drop of the logit when one feature (or, for unit type and
         admission source, the whole one-hot group) is set to the reference value,
       - integrated gradients from the reference patient (one-hot groups summed);
  4. adds two run-level figures: permutation importance of the features on the test split
     (drop of the AUROC when a feature is shuffled) and how much the two methods agree on
     each selected patient.

Red means evidence for death (the logit of death rises with the feature), blue evidence for
survival. Each bar is labelled with the feature's value as recorded ("missing" if it was not
charted and the model saw the imputed median). The maps describe what the model used. They
support a reviewer's reading of a case; they are not causes and do not justify a prediction.
"""

import io
import json
import time

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from swarmmed_explain import (
    as_predict_fn,
    feature_ablation,
    integrated_gradients,
    permutation_importance,
    plot_feature_bars,
    rank_agreement,
    roc_auc,
    select_cases,
)
from swarmmed_explain.plotting import AGREEMENT_COLORS, plot_feature_table

N_PER_OUTCOME = 2
ALERT_THRESHOLD = 0.2  # below 0.5: only about 9 % of the patients die
IG_STEPS = 64
PERMUTATION_REPEATS = 30
MAX_BARS = 15
SEED = 0
HIDDEN_UNITS = 32
DROPOUT = 0.2
FEATURE_NAMES = [
    "age", "height_cm", "weight_kg", "pre_icu_days",
    "hr_min", "hr_max", "hr_mean", "rr_min", "rr_max", "rr_mean", "spo2_min", "spo2_mean",
    "sbp_min", "sbp_mean", "map_min", "map_mean", "temp_min", "temp_max", "gcs_min",
    "creat_max", "bun_max", "na_min", "na_max", "k_max", "glucose_max", "hco3_min",
    "wbc_max", "hgb_min", "plt_min", "lactate_max", "bili_max", "albumin_min",
    "male",
    "unit_medsurg", "unit_medical", "unit_cardiac", "unit_surg_neuro",
    "src_ed", "src_or", "src_ward", "src_other",
]  # fmt: skip
# One-hot groups are explained as one item (setting a single column of a one-hot code to
# its reference would give a patient that has no unit type at all).
GROUPS = {
    "unit type": ["unit_medsurg", "unit_medical", "unit_cardiac", "unit_surg_neuro"],
    "admission source": ["src_ed", "src_or", "src_ward", "src_other"],
}
GROUP_VALUE_TEXT = {  # what a set one-hot column means
    "unit_medsurg": "med-surg ICU",
    "unit_medical": "medical ICU",
    "unit_cardiac": "cardiac ICU",
    "unit_surg_neuro": "surgical/neuro ICU",
    "src_ed": "emergency dept.",
    "src_or": "operating room",
    "src_ward": "ward",
    "src_other": "other",
}


class MortalityMLP(nn.Module):
    """Same architecture as MortalityMLP in training.py (keep the two identical)."""

    def __init__(self):
        """Build the layers (keys net.0 and net.3, as in the checkpoint)."""
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(len(FEATURE_NAMES), HIDDEN_UNITS),
            nn.ReLU(),
            nn.Dropout(DROPOUT),
            nn.Linear(HIDDEN_UNITS, 1),
        )

    def forward(self, x):
        """Return one logit of in-hospital death per row."""
        return self.net(x).squeeze(-1)


def read_npz(path):
    """Load a registered .npz file into a dict of arrays."""
    with explain.open(path, "rb") as handle:
        data = handle.read()
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {key: npz[key] for key in npz.files}


def find_site_folder():
    """First site folder with train.npz and test.npz ('' for root-level files)."""
    names = explain.listdir("")
    folders = sorted(
        {n.split("/", 1)[0] for n in names if "/" in n and not n.startswith("results/")}
    )
    for folder in folders:
        if explain.exists(f"{folder}/test.npz") and explain.exists(
            f"{folder}/train.npz"
        ):
            return f"{folder}/"
    if explain.exists("test.npz") and explain.exists("train.npz"):
        return ""
    raise FileNotFoundError(
        "no train.npz and test.npz in the registered data (looked in "
        f"{folders[:10] or 'no folders'} and the root); see README.md, 'Data layout'"
    )


def item_layout():
    """Explained items: (label, [column indices]) for single features and one-hot groups."""
    column = {name: i for i, name in enumerate(FEATURE_NAMES)}
    grouped = {name for members in GROUPS.values() for name in members}
    items = [(n, [column[n]]) for n in FEATURE_NAMES if n not in grouped]
    items += [
        (label, [column[m] for m in members]) for label, members in GROUPS.items()
    ]
    return items


def display_value(raw_row, items):
    """Recorded value of every item as short text (the imputed ones as 'missing')."""
    texts = []
    for label, columns in items:
        if label in GROUPS:
            members = [FEATURE_NAMES[c] for c in columns]
            active = [GROUP_VALUE_TEXT[m] for m, c in zip(members, columns, strict=True)
                      if raw_row[c] == 1]  # fmt: skip
            texts.append(active[0] if active else "unknown")
        elif np.isnan(raw_row[columns[0]]):
            texts.append("missing")
        elif label == "male":
            texts.append("male" if raw_row[columns[0]] == 1 else "female")
        else:
            texts.append(f"{raw_row[columns[0]]:.3g}")
    return texts


def typical_values(raw_train, items):
    """Per item, the site's training median (numbers) or most common value, as text."""
    rows = [display_value(row, items) for row in raw_train]
    texts = []
    for k, (label, columns) in enumerate(items):
        if label not in GROUPS and label != "male":
            column = raw_train[:, columns[0]]
            column = column[np.isfinite(column)]
            texts.append(f"median {np.median(column):.3g}" if column.size else "")
            continue
        shown = [row[k] for row in rows if row[k] not in ("missing", "unknown")]
        if not shown:
            texts.append("")
            continue
        values, counts = np.unique(shown, return_counts=True)
        best = int(np.argmax(counts))
        texts.append(f"{values[best]} ({counts[best] / len(shown):.0%})")
    return texts


def top_items(values, k=MAX_BARS):
    """Indices of the k items with the largest absolute value, in item order."""
    return sorted(np.argsort(-np.abs(values))[:k].tolist())


def main():
    started = time.perf_counter()
    torch.set_num_threads(max(1, torch.get_num_threads()))
    torch.manual_seed(SEED)
    np.random.seed(SEED)

    folder = find_site_folder()
    train, test = read_npz(f"{folder}train.npz"), read_npz(f"{folder}test.npz")
    names = [str(n) for n in test["feature_names"]]
    if names != FEATURE_NAMES:
        raise ValueError(
            "the feature columns differ from the ones this template expects; prepare the "
            "data with this template's prepare_data.py"
        )
    site = folder.strip("/") or "root"
    X = test["X"].astype(np.float32)
    X_raw, y, ids = (
        test["X_raw"],
        test["y"].astype(int),
        [str(c) for c in test["case_id"]],
    )
    reference = np.median(train["X"].astype(np.float32), axis=0)

    model = MortalityMLP()
    explain.load_weights(
        model, client_name=site, model_filename="FL_global_model.pt", strict=True
    )
    model.eval()

    items = item_layout()
    labels = [label for label, _ in items]
    groups = [columns for _, columns in items]

    typical = typical_values(train["X_raw"], items)

    probability = as_predict_fn(model, output="probability")
    scores = probability(X)
    print(
        f"{site}: {len(y)} test patients, {int(y.sum())} deaths, scores "
        f"{scores.min():.3f}..{scores.max():.3f}, test AUROC "
        f"{roc_auc(y, scores):.3f}, alert threshold {ALERT_THRESHOLD}"
    )
    chosen = select_cases(
        y, scores, n_per_outcome=N_PER_OUTCOME, threshold=ALERT_THRESHOLD
    )
    if not chosen:
        raise RuntimeError("no test patient could be selected")

    agreement = []
    for case in chosen:
        case_id = ids[case.index]
        x, raw = X[case.index], X_raw[case.index]
        ablation = feature_ablation(model, x, baseline=reference, groups=groups)
        ig = integrated_gradients(model, x, baseline=reference, steps=IG_STEPS)
        ig_items = np.array([ig.attributions[columns].sum() for columns in groups])
        match = rank_agreement(ablation, ig_items, k=5, absolute=True)
        agreement.append((case_id, case.outcome, match))
        texts = display_value(raw, items)
        explain.add_case(
            case_id,
            label="died" if case.label else "discharged alive",
            prediction="death" if case.prediction else "survival",
            score=case.score,
            target="logit of in-hospital death",
            outcome=case.outcome,
            details={
                "site": site,
                "ig_completeness_gap": round(float(ig.completeness_gap), 4),
                "spearman_ablation_vs_ig": round(float(match["spearman"]), 3),
                "top5_overlap": round(float(match["top_k_overlap"]), 3),
                "n_missing_values": int(sum(t == "missing" for t in texts)),
            },
        )
        # Input panel first: the recorded values only (no score, prediction or map), so
        # it can stay visible in a blinded review.
        plot_feature_table(
            labels,
            texts,
            reference=typical,
            value_header="recorded (first 24 h)",
            reference_header=f"{site} training split",
            title=f"Recorded values of patient {case_id} (missing: not charted)",
            rows_per_column=18,
        )
        explain.add_panel(case_id, "input", "Recorded values")
        for values, title, method in (
            (
                ablation,
                "Feature ablation (logit drop when set to the reference)",
                "feature_ablation",
            ),
            (
                ig_items,
                "Integrated gradients (from the reference patient)",
                "integrated_gradients",
            ),
        ):
            keep = top_items(values)
            plot_feature_bars(
                values[keep],
                [labels[i] for i in keep],
                feature_values=[texts[i] for i in keep],
                title=f"{title}\n{case.outcome}, predicted probability of death {case.score:.2f}",
                xlabel="effect on the logit of death (red: death, blue: survival)",
                max_features=MAX_BARS,
            )
            explain.add_panel(case_id, method, title)

    # Run-level figure 1: permutation importance on the test split, by item
    result = permutation_importance(
        model, X, y, roc_auc, n_repeats=PERMUTATION_REPEATS, seed=SEED, groups=groups
    )
    keep = top_items(result.mean)
    plot_feature_bars(
        result.mean[keep],
        [labels[i] for i in keep],
        errors=result.std[keep],
        title=f"Permutation importance on the {len(y)} test patients of {site} "
        f"(baseline AUROC {result.baseline_score:.2f}, {int(y.sum())} deaths)",
        xlabel="AUROC drop when shuffled (mean, SD; below 0 = no signal)",
        max_features=MAX_BARS,
    )
    explain.add_summary("Permutation importance on the test split")

    # Run-level figure 2: agreement of the two methods per selected patient
    fig, ax = plt.subplots(figsize=(7.5, 0.5 * len(agreement) + 1.8))
    rho = [a[2]["spearman"] for a in agreement]
    ypos = np.arange(len(agreement))[::-1]
    ax.barh(ypos, rho, color=AGREEMENT_COLORS["spearman"])
    ax.set_yticks(ypos, [f"{c} ({o})" for c, o, _ in agreement], fontsize=8)
    for yy, (_, _, match) in zip(ypos, agreement, strict=True):
        ax.text(0.02, yy, f"top-5 overlap {match['top_k_overlap']:.1f}", va="center",
                fontsize=7, color="white")  # fmt: skip
    ax.set_xlim(-1, 1)
    ax.axvline(0, color="#898781", linewidth=0.8)
    ax.set_xlabel(
        "Spearman correlation of |ablation| and |integrated gradients| over the items"
    )
    ax.set_title("Agreement of the two methods per selected patient", fontsize=10)
    fig.tight_layout()
    explain.add_summary("Agreement between feature ablation and integrated gradients")

    explain.set_info(
        methods=["feature_ablation", "integrated_gradients", "permutation_importance"],
        target="logit of in-hospital death",
        notes=(
            f"Site {site}, test split ({len(y)} patients, {int(y.sum())} deaths). Cases: 2 per "
            f"outcome at an alert threshold of {ALERT_THRESHOLD} on the predicted probability, "
            "the most confident ones per outcome. Reference patient: the median of the site's "
            "training split in the scaled input space. One-hot groups (unit type, admission "
            "source) are explained as one item. Maps describe what the model used; they are "
            "not causes and do not justify a prediction."
        ),
        metrics={
            "test_auroc": round(float(roc_auc(y, scores)), 4),
            "alert_threshold": ALERT_THRESHOLD,
            "n_test": int(len(y)),
            "n_deaths": int(y.sum()),
            "seconds": round(time.perf_counter() - started, 1),
        },
    )
    print(
        json.dumps(
            {"cases": len(chosen), "seconds": round(time.perf_counter() - started, 1)}
        )
    )


main()
