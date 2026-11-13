"""Per-case explanations for the tabular_heart_disease template, run by SwarmMedHub's sandbox.

SwarmMedHub runs this script for a finished training job with a helper object ``explain``
(see ``swarmmed_explain/README.md``). The helper's manifest holds the project data
(``fl-client-1/test.npz``, ...) or a single site's files at the root, and the job's result
files under ``results/``. The script

1. loads the final global model (``FL_global_model.pt``) and the test split of the first site
   folder found (or the root data),
2. picks the two most confident cases of each outcome (true and false positives and
   negatives) with the model's probability threshold of 0.5,
3. shows each case's recorded values first (a table without any model output: the panel
   that a blinded review shows before the reader saves a read), then explains the case with
   two methods, drawn as bar panels in the log-odds of heart disease:
   - feature ablation: the drop of the output when a feature is set to the site's training
     mean,
   - integrated gradients from the same reference,
   with the 15 model inputs grouped into the 10 clinical variables (the one-hot columns of
   chest pain type and resting ECG form one group each, so both methods give one value per
   variable),
4. adds two run-level figures: permutation importance (drop in test AUROC when one variable is
   shuffled across the site's test cases) and the mean absolute feature-ablation effect over
   all test cases.

Red bars support the prediction "heart disease", blue bars oppose it. The maps describe what
this model does with its inputs. They do not show causes, and a clinician should not treat them
as a justification of a prediction. Runs in a few seconds on one CPU.
"""

import io
import json
import re

import numpy as np
import torch
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
from swarmmed_explain.plotting import plot_feature_table
from torch import nn

HIDDEN_UNITS = 32  # as in training.py
DROPOUT = 0.2
N_FEATURES = 15
THRESHOLD = 0.5
CASES_PER_OUTCOME = 2
IG_STEPS = 64
PERMUTATION_REPEATS = 30
SEED = 0

# Model inputs in training order, and the clinical variable each one belongs to.
FEATURE_NAMES = (
    "age",
    "trestbps",
    "chol",
    "thalach",
    "oldpeak",
    "sex",
    "fbs",
    "exang",
    "cp_1",
    "cp_2",
    "cp_3",
    "cp_4",
    "restecg_0",
    "restecg_1",
    "restecg_2",
)
GROUPS = (
    ("Age", (0,)),
    ("Resting blood pressure", (1,)),
    ("Serum cholesterol", (2,)),
    ("Max. heart rate", (3,)),
    ("ST depression", (4,)),
    ("Male sex", (5,)),
    ("Fasting blood sugar > 120", (6,)),
    ("Exercise-induced angina", (7,)),
    ("Chest pain type", (8, 9, 10, 11)),
    ("Resting ECG", (12, 13, 14)),
)
GROUP_NAMES = [name for name, _ in GROUPS]
GROUP_COLUMNS = [list(columns) for _, columns in GROUPS]
UNITS = {0: "y", 1: "mm Hg", 2: "mg/dl", 3: "/min", 4: ""}
CATEGORIES = {
    "Chest pain type": ("typical angina", "atypical", "non-anginal", "asymptomatic"),
    "Resting ECG": ("normal", "ST-T abnormal", "LV hypertrophy"),
}
SEX_NAMES = {0: "female", 1: "male"}


class HeartDiseaseMLP(nn.Module):
    """Same layers and names as in training.py: 15 -> 32 (ReLU, dropout) -> 1 logit."""

    def __init__(self, n_features=N_FEATURES):
        super().__init__()
        self.hidden = nn.Linear(n_features, HIDDEN_UNITS)
        self.dropout = nn.Dropout(DROPOUT)
        self.output = nn.Linear(HIDDEN_UNITS, 1)

    def forward(self, x):
        return self.output(self.dropout(torch.relu(self.hidden(x)))).squeeze(-1)


def natural_key(text):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]


def find_site_prefix(keys):
    """Prefix of the first site folder that holds test.npz ('' for root-level data)."""
    folders = sorted(
        {k.split("/")[0] for k in keys if k.count("/") == 1 and k.endswith("/test.npz")}
        - {"results"},
        key=natural_key,
    )
    if folders:
        return folders[0] + "/"
    if "test.npz" in keys:
        return ""
    raise FileNotFoundError(
        "No test.npz found: expected <site folder>/test.npz or test.npz at the root. "
        f"Folders seen: {sorted({k.split('/')[0] for k in keys})[:10]}"
    )


def read_npz(path):
    with explain.open(path, "rb") as handle:
        data = handle.read()
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {name: npz[name] for name in npz.files}


def read_site_info(path):
    if not explain.exists(path):
        return {}
    with explain.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def format_value(group_index, raw_row):
    """Human-readable value of one clinical variable for the bar labels."""
    columns = GROUP_COLUMNS[group_index]
    values = raw_row[columns]
    if np.isnan(values).any():
        return "missing"
    name = GROUP_NAMES[group_index]
    if name in CATEGORIES:
        return CATEGORIES[name][int(np.argmax(values))]
    value = float(values[0])
    if name == "Male sex":
        return SEX_NAMES[int(round(value))]
    if name in ("Fasting blood sugar > 120", "Exercise-induced angina"):
        return "yes" if value >= 0.5 else "no"
    unit = UNITS.get(columns[0], "")
    return f"{value:g} {unit}".strip()


def age_group(age):
    if not np.isfinite(age):
        return "unknown"
    low = int(age // 10) * 10
    return f"{low}-{low + 9}"


def typical_values(raw_train):
    """Per clinical variable, the site's training median or most common value, as text."""
    texts = []
    for g, columns in enumerate(GROUP_COLUMNS):
        if GROUP_NAMES[g] in CATEGORIES or columns[0] not in UNITS:
            shown = [format_value(g, row) for row in raw_train]
            shown = [text for text in shown if text != "missing"]
            if not shown:
                texts.append("")
                continue
            values, counts = np.unique(shown, return_counts=True)
            best = int(np.argmax(counts))
            texts.append(f"{values[best]} ({counts[best] / len(shown):.0%})")
            continue
        column = raw_train[:, columns[0]]
        column = column[np.isfinite(column)]
        if column.size == 0:
            texts.append("")
            continue
        unit = UNITS.get(columns[0], "")
        texts.append(f"median {float(np.median(column)):g} {unit}".strip())
    return texts


def grouped(values):
    """Sum the per-input values of each clinical variable."""
    values = np.asarray(values, dtype=float)
    return np.array([values[columns].sum() for columns in GROUP_COLUMNS])


def main():
    keys = set(explain.listdir(""))
    prefix = find_site_prefix(keys)
    site = prefix.rstrip("/") or "root"

    model = HeartDiseaseMLP()
    explain.load_weights(model, model_filename="FL_global_model.pt", strict=True)
    model.eval()

    train = read_npz(f"{prefix}train.npz")
    test = read_npz(f"{prefix}test.npz")
    info = read_site_info(f"{prefix}site_info.json")
    names = [f["name"] for f in info.get("features", [])]
    if names and tuple(names) != FEATURE_NAMES:
        raise ValueError(
            f"Feature order {names} differs from training.py {FEATURE_NAMES}"
        )
    x_test = test["X"].astype(np.float32)
    raw_test = test["X_raw"].astype(np.float64)
    y_test = test["y"].astype(int)
    case_ids = [str(c) for c in test["case_id"]]
    # Reference for "feature absent": the site's mean training case (z-scored continuous
    # variables: 0; binary and one-hot variables: their training prevalence).
    reference = train["X"].astype(np.float32).mean(axis=0)
    typical = typical_values(train["X_raw"].astype(np.float64))

    scores = as_predict_fn(model, output="raw")(x_test)
    prob = 1.0 / (1.0 + np.exp(-scores))
    auroc = roc_auc(y_test, prob) if len(set(y_test.tolist())) == 2 else float("nan")
    print(f"site {site}: {len(y_test)} test cases, AUROC {auroc:.3f}")

    selected = select_cases(
        y_test, prob, n_per_outcome=CASES_PER_OUTCOME, threshold=THRESHOLD
    )
    age_index = FEATURE_NAMES.index("age")
    for case in selected:
        i = case.index
        x = x_test[i]
        ablation_groups = feature_ablation(
            model, x, baseline=reference, groups=GROUP_COLUMNS
        )
        ig = integrated_gradients(
            model, x, baseline=reference, steps=IG_STEPS, output="raw"
        )
        ig_groups = grouped(ig.attributions)
        agreement = rank_agreement(ig_groups, ablation_groups, k=3, absolute=True)
        shown = [format_value(g, raw_test[i]) for g in range(len(GROUPS))]
        n_missing = int(
            sum(np.isnan(raw_test[i][columns]).any() for columns in GROUP_COLUMNS)
        )
        raw_age = raw_test[i][age_index]
        case_id = case_ids[i]
        explain.add_case(
            case_id,
            label=case.label,
            prediction=case.prediction,
            score=case.score,
            target="heart disease present (num > 0)",
            outcome=case.outcome,
            details={
                "site": site,
                "age_group": age_group(raw_age),
                "sex": SEX_NAMES.get(int(round(raw_test[i][5])), "unknown")
                if np.isfinite(raw_test[i][5])
                else "unknown",
                "variables_missing_before_imputation": n_missing,
                "log_odds": round(float(scores[i]), 3),
                "ig_completeness_gap": round(float(ig.completeness_gap), 4),
                "ig_vs_ablation_spearman_abs": round(float(agreement["spearman"]), 3),
                "ig_vs_ablation_top3_overlap": round(
                    float(agreement["top_k_overlap"]), 3
                ),
            },
        )
        # Input panel first: the recorded values only (no score, prediction or map), so
        # it can stay visible in a blinded review.
        plot_feature_table(
            GROUP_NAMES,
            shown,
            reference=typical,
            value_header="recorded value",
            reference_header=f"{site} training split",
            title=f"Recorded values of {case_id} (missing: not recorded)",
        )
        explain.add_panel(case_id, "input", "Recorded values")
        plot_feature_bars(
            ablation_groups,
            GROUP_NAMES,
            feature_values=shown,
            title="Feature ablation (drop in log-odds when set to the site mean)",
            xlabel="drop in log-odds when set to the site mean (red supports, blue opposes)",
        )
        explain.add_panel(case_id, "feature_ablation", "Feature ablation")
        plot_feature_bars(
            ig_groups,
            GROUP_NAMES,
            feature_values=shown,
            title="Integrated gradients (from the site mean)",
            xlabel="contribution to log-odds (red supports, blue opposes)",
        )
        explain.add_panel(case_id, "integrated_gradients", "Integrated gradients")

    predict = as_predict_fn(model, output="raw")
    importance = permutation_importance(
        predict,
        x_test,
        y_test,
        roc_auc,
        n_repeats=PERMUTATION_REPEATS,
        seed=SEED,
        groups=GROUP_COLUMNS,
    )
    plot_feature_bars(
        importance.mean,
        GROUP_NAMES,
        errors=importance.std,
        title=f"Permutation importance on the {site} test split (n={len(y_test)})",
        xlabel=f"drop in test AUROC when shuffled (baseline {importance.baseline_score:.2f})",
    )
    explain.add_summary("Permutation importance on the test split")

    mean_abs = np.zeros(len(GROUPS))
    for row in x_test:
        mean_abs += np.abs(
            feature_ablation(model, row, baseline=reference, groups=GROUP_COLUMNS)
        )
    mean_abs /= len(x_test)
    plot_feature_bars(
        mean_abs,
        GROUP_NAMES,
        title=f"Mean absolute ablation effect, {len(x_test)} test cases",
        xlabel="mean |change in log-odds| (size only, not direction)",
    )
    explain.add_summary("Mean absolute feature-ablation effect")

    explain.set_info(
        methods=["feature_ablation", "integrated_gradients", "permutation_importance"],
        target="heart disease present (num > 0), log-odds output of the model",
        notes=(
            f"Cases: the two most confident of each outcome from the {site} test split, "
            "threshold 0.5. Reference for ablation and integrated gradients: the mean "
            "training case of the site (z-scored variables 0, binary variables their "
            "prevalence); the 15 model inputs are grouped into 10 clinical variables. "
            "Maps show what the model does with its inputs, not causes; they support a "
            "review and do not justify a prediction."
        ),
        metrics={"auroc_test": auroc, "n_test": int(len(y_test))},
    )


main()
