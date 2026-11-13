"""Data checks for the tabular_heart_disease template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script with a helper object ``validation`` (``add_check``, ``open``,
``listdir``, ``exists``) against the project's registered data and lists every check with its
status: ok, warning or error. The script checks the files that prepare_data.py writes, in
either layout: one folder per FLARE site (``fl-client-1/train.npz``, ...) or one site's files
at the data root (``train.npz``, ...). It reads only the small .npz and .json files; the
sandbox allows 1 CPU, 1 GB of memory and 5 minutes.
"""

import io
import json
import re

import numpy as np

SPLITS = ("train", "val", "test")
CONTINUOUS = ["age", "trestbps", "chol", "thalach", "oldpeak"]
BINARY = ["sex", "fbs", "exang"]
ONEHOT_GROUPS = {
    "cp": ["cp_1", "cp_2", "cp_3", "cp_4"],
    "restecg": ["restecg_0", "restecg_1", "restecg_2"],
}
FEATURES = CONTINUOUS + BINARY + ONEHOT_GROUPS["cp"] + ONEHOT_GROUPS["restecg"]
Z_CLIP = 5.0  # prepare_data.py clips standardised values to [-5, 5]
MIN_CLASS_CASES = 5  # fewer cases of a class make a split's AUROC uninformative
HIGH_MISSING = 0.5  # report features missing for more than half of a site's patients
# Case ids must match the pattern of SwarmMedHub's explanation viewer.
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def check(name, status, message, details=None):
    validation.add_check(name, status, message, details or {})


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
    with validation.open(path, "rb") as handle:
        data = handle.read()
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {key: npz[key] for key in npz.files}


def read_json(path):
    with validation.open(path, "r") as handle:
        return json.load(handle)


def check_site(label, prefix, keys):
    """Run all checks for one site; return its feature names (or None if unreadable)."""
    needed = [f"{prefix}{split}.npz" for split in SPLITS] + [f"{prefix}site_info.json"]
    missing = [path for path in needed if path not in keys]
    if missing:
        check(
            f"{label}: files",
            "error",
            f"Missing {missing}; run prepare_data.py for this site.",
        )
        if any(path.endswith(".npz") for path in missing):
            return None
    else:
        check(
            f"{label}: files",
            "ok",
            "train.npz, val.npz, test.npz and site_info.json found.",
        )

    # Arrays and their shapes
    data, problems = {}, []
    for split in SPLITS:
        arrays = read_npz(f"{prefix}{split}.npz")
        data[split] = arrays
        absent = {"X", "X_raw", "y", "case_id", "feature_names"} - set(arrays)
        if absent:
            problems.append(f"{split}: arrays {sorted(absent)} missing")
            continue
        x, n = arrays["X"], len(arrays["y"])
        if x.ndim != 2 or x.shape != arrays["X_raw"].shape or x.shape[0] != n:
            problems.append(
                f"{split}: X {x.shape}, X_raw {arrays['X_raw'].shape}, y {n}"
            )
        if len(arrays["case_id"]) != n:
            problems.append(
                f"{split}: {len(arrays['case_id'])} case ids for {n} labels"
            )
        if x.dtype != np.float32 or arrays["y"].dtype.kind not in "iu":
            problems.append(f"{split}: X is {x.dtype}, y is {arrays['y'].dtype}")
    if problems:
        check(f"{label}: arrays", "error", "; ".join(problems))
        return None
    shapes = {split: list(data[split]["X"].shape) for split in SPLITS}
    check(
        f"{label}: arrays",
        "ok",
        "X, X_raw, y, case_id and feature_names are consistent.",
        shapes,
    )

    # Feature names and order (all sites must feed the model the same columns)
    names = [str(name) for name in data["train"]["feature_names"]]
    if names != FEATURES or any(
        [str(n) for n in data[s]["feature_names"]] != names for s in SPLITS
    ):
        check(
            f"{label}: features",
            "error",
            "Feature names or order differ from the template's.",
            {"found": names, "expected": FEATURES},
        )
        return names
    check(
        f"{label}: features", "ok", f"{len(FEATURES)} features in the template's order."
    )

    # Model inputs: finite, in range, one-hot groups complete
    x_all = np.concatenate([data[s]["X"] for s in SPLITS])
    column = {name: j for j, name in enumerate(FEATURES)}
    problems = []
    if not np.isfinite(x_all).all():
        problems.append("X holds missing or infinite values")
    for name in CONTINUOUS:
        values = x_all[:, column[name]]
        if np.abs(values).max() > Z_CLIP + 1e-6:
            problems.append(f"{name} outside [-{Z_CLIP}, {Z_CLIP}]")
    for name in BINARY + ONEHOT_GROUPS["cp"] + ONEHOT_GROUPS["restecg"]:
        if not np.isin(x_all[:, column[name]], (0.0, 1.0)).all():
            problems.append(f"{name} not 0/1")
    for group, members in ONEHOT_GROUPS.items():
        sums = x_all[:, [column[m] for m in members]].sum(axis=1)
        if not np.all(sums == 1):
            problems.append(f"{group} one-hot rows do not sum to 1")
    if problems:
        check(f"{label}: model inputs", "error", "; ".join(problems))
    else:
        check(
            f"{label}: model inputs",
            "ok",
            "Complete, standardised and one-hot encoded.",
        )

    # Labels and class balance
    counts = {s: [len(data[s]["y"]), int(data[s]["y"].sum())] for s in SPLITS}
    if not all(np.isin(data[s]["y"], (0, 1)).all() for s in SPLITS):
        check(f"{label}: labels", "error", "Labels other than 0 and 1.", counts)
    elif min(counts["train"][1], counts["train"][0] - counts["train"][1]) == 0:
        check(
            f"{label}: labels",
            "error",
            "The training split holds one class only.",
            counts,
        )
    else:
        small = [
            f"{s}: {count} {kind}"
            for s in ("val", "test")
            for count, kind in (
                (counts[s][1], "positive"),
                (counts[s][0] - counts[s][1], "negative"),
            )
            if count < MIN_CLASS_CASES
        ]
        prevalence = sum(c[1] for c in counts.values()) / sum(
            c[0] for c in counts.values()
        )
        message = f"[n, positives] per split {counts}; prevalence {prevalence:.0%}."
        if small:
            check(
                f"{label}: labels",
                "warning",
                message
                + " Few cases of a class ("
                + ", ".join(small)
                + "): this site's AUROC is not interpretable.",
                counts,
            )
        else:
            check(f"{label}: labels", "ok", message, counts)

    # Case ids: unique, in one split only, usable by the explanation viewer
    ids = [str(c) for s in SPLITS for c in data[s]["case_id"]]
    duplicated = len(ids) - len(set(ids))
    bad = [c for c in ids if not CASE_ID_PATTERN.match(c)]
    if duplicated:
        check(
            f"{label}: case ids",
            "error",
            f"{duplicated} case id(s) occur more than once.",
        )
    elif bad:
        check(
            f"{label}: case ids",
            "warning",
            f"{len(bad)} case id(s) do not match "
            "the pattern SwarmMedHub's explanation viewer accepts.",
            {"examples": bad[:5]},
        )
    else:
        check(
            f"{label}: case ids",
            "ok",
            f"{len(ids)} unique case ids, each in one split.",
        )

    # Identical patients in two splits would leak information between training and testing
    seen, leaks = {}, 0
    for s in SPLITS:
        rows = np.nan_to_num(data[s]["X_raw"], nan=-999.0)
        for row, y in zip(rows, data[s]["y"], strict=True):
            key = (row.tobytes(), int(y))
            if seen.setdefault(key, s) != s:
                leaks += 1
    if leaks:
        check(
            f"{label}: duplicates",
            "warning",
            f"{leaks} identical record(s) occur in two splits.",
        )
    else:
        check(f"{label}: duplicates", "ok", "No identical record occurs in two splits.")

    # Missing values before imputation (X_raw), all splits
    x_raw = np.concatenate([data[s]["X_raw"] for s in SPLITS])
    share = {name: float(np.isnan(x_raw[:, column[name]]).mean()) for name in FEATURES}
    high = {name: round(v, 2) for name, v in share.items() if v > HIGH_MISSING}
    details = {name: round(v, 3) for name, v in share.items() if v > 0}
    if high:
        check(
            f"{label}: missing values",
            "warning",
            f"Missing for more than half of the patients: {high} (share of patients). "
            "prepare_data.py fills missing values from this site's training split; a "
            "feature never recorded in it is a constant 0 for this site.",
            details,
        )
    else:
        check(
            f"{label}: missing values",
            "ok",
            "No feature is missing for more than half of the patients.",
            details,
        )

    # site_info.json agrees with the arrays
    info_path = f"{prefix}site_info.json"
    if info_path in keys:
        info = read_json(info_path)
        recorded = {
            s: [
                info.get("counts", {}).get(s, {}).get("n"),
                info.get("counts", {}).get(s, {}).get("positives"),
            ]
            for s in SPLITS
        }
        if recorded != counts:
            check(
                f"{label}: site_info.json",
                "warning",
                "Counts differ from the arrays; re-run prepare_data.py.",
                {"site_info": recorded, "arrays": counts},
            )
        else:
            source = (
                info.get("source_description")
                or info.get("source_site")
                or "not stated"
            )
            check(f"{label}: site_info.json", "ok", f"Counts agree. Source: {source}.")
    return names


keys = list(validation.listdir(""))
sites = find_sites(keys)
if not sites:
    check(
        "Data layout",
        "error",
        "No train.npz found, neither in site folders (fl-client-1/"
        "train.npz, ...) nor at the data root. Import the output of prepare_data.py.",
    )
else:
    layout = (
        "one folder per site"
        if "data root" not in sites
        else "one site at the data root"
    )
    check(
        "Data layout",
        "ok",
        f"{len(sites)} site(s), {layout}: {', '.join(sites)}.",
        {"sites": list(sites)},
    )
    feature_lists = {}
    for label, prefix in sites.items():
        try:
            feature_lists[label] = check_site(label, prefix, set(keys))
        except Exception as exc:  # report and continue with the next site
            check(f"{label}: read error", "error", f"{type(exc).__name__}: {exc}")
    readable = {k: v for k, v in feature_lists.items() if v is not None}
    if len(readable) > 1:
        if len({tuple(v) for v in readable.values()}) == 1:
            check(
                "Sites agree",
                "ok",
                "Every site has the same features in the same order.",
            )
        else:
            check(
                "Sites agree",
                "error",
                "Sites differ in features or their order; the swarm "
                "model needs identical inputs. Prepare all sites with the same prepare_data.py.",
            )
