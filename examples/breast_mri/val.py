"""Pre-flight validation for a breast-MRI site (ODELIA/Duke layout).

Runs in the SwarmMed sandbox before a site contributes to a study. It checks that
this site's local data is complete, consistently labelled and usable by the
training pipeline — without any of it leaving the site. Only the check results
(counts, class balance, names of offending cases) are recorded.

Expected layout at the site, relative to the registered data directory:

    data_unilateral/<UID>/{Pre,Post_1..Post_4}.nii.gz
    metadata_unilateral/annotation.csv   UID,PatientID,Age,Lesion
    metadata_unilateral/split.csv        UID,Fold,Split

`validation` is injected by the platform: validation.open(), validation.exists(),
validation.add_check(name, status, message, details).

Adapt the constants below for your own cohort; the structure of the checks is the
part that transfers.
"""

import csv
import io
import re

DATA_DIR_NAME = "data_unilateral"
META_DIR_NAME = "metadata_unilateral"
REQUIRED_SERIES = ["Pre.nii.gz", "Post_1.nii.gz"]
OPTIONAL_SERIES = ["Post_2.nii.gz", "Post_3.nii.gz", "Post_4.nii.gz"]
# Lesion coding in the ODELIA/Duke annotation files.
POSITIVE_CLASS = 2
EXPECTED_CLASSES = {0, 1, 2}
MIN_CASES = 20
MIN_POSITIVE_FRACTION = 0.05
DEEP_CHECK_LIMIT = 40  # how many cases to open on disk


def discover(folder_name, manifest):
    """Locate a folder in the uploaded tree.

    A site may register the cohort at the top level, or nested inside a parent
    folder named after the centre (for example ``CAM/metadata_unilateral/...``).
    Find it wherever it is, rather than assuming the layout.
    """
    candidates = set()
    for file_path in manifest:
        parts = file_path.split("/")
        for index, part in enumerate(parts):
            if part == folder_name:
                candidates.add("/".join(parts[: index + 1]))
    return sorted(candidates)[0] if candidates else None


def read_csv(path):
    with validation.open(path, "r") as handle:  # noqa: F821
        return list(csv.DictReader(io.StringIO(handle.read())))


def main():
    # 0. Find the cohort folders wherever the site placed them
    manifest = list(validation.manifest.keys())  # noqa: F821
    meta_dir = discover(META_DIR_NAME, manifest)
    data_dir = discover(DATA_DIR_NAME, manifest)
    if not meta_dir or not data_dir:
        validation.add_check(  # noqa: F821
            "Folder discovery", "error",
            f"Could not locate '{META_DIR_NAME}' and '{DATA_DIR_NAME}' in the registered data "
            f"(found metadata: {meta_dir}, data: {data_dir}).",
            details={"files_seen": manifest[:20]},
        )
        return
    validation.add_check(  # noqa: F821
        "Folder discovery", "success", f"Using {meta_dir} and {data_dir}"
    )

    # 1. Metadata present and parseable
    annotation_path = f"{meta_dir}/annotation.csv"
    split_path = f"{meta_dir}/split.csv"
    for path in (annotation_path, split_path):
        if not validation.exists(path):  # noqa: F821
            validation.add_check(  # noqa: F821
                "Metadata present", "error", f"Missing {path}. Cannot continue."
            )
            return
    annotation = read_csv(annotation_path)
    split = read_csv(split_path)
    validation.add_check(  # noqa: F821
        "Metadata present",
        "success",
        f"annotation.csv: {len(annotation)} rows; split.csv: {len(split)} rows",
    )

    # 2. Required columns
    missing_cols = {
        "annotation.csv": {"UID", "PatientID", "Lesion"} - set(annotation[0]),
        "split.csv": {"UID", "Fold", "Split"} - set(split[0]),
    }
    if any(missing_cols.values()):
        validation.add_check(  # noqa: F821
            "Required columns", "error", "Missing columns", details=
            {k: sorted(v) for k, v in missing_cols.items() if v}
        )
        return
    validation.add_check("Required columns", "success", "All required columns present")  # noqa: F821

    # 3. Cohort size and class balance
    labels = []
    unexpected = {}
    for row in annotation:
        raw = (row.get("Lesion") or "").strip()
        try:
            labels.append(int(float(raw)))
        except ValueError:
            unexpected[row["UID"]] = raw
    positives = sum(1 for v in labels if v == POSITIVE_CLASS)
    fraction = positives / len(labels) if labels else 0.0
    distribution = {str(v): labels.count(v) for v in sorted(set(labels))}

    validation.add_check(  # noqa: F821
        "Cohort size",
        "success" if len(annotation) >= MIN_CASES else "warning",
        f"{len(annotation)} exams from {len({r['PatientID'] for r in annotation})} patients"
        + ("" if len(annotation) >= MIN_CASES else f" — below the agreed minimum of {MIN_CASES}"),
        details={"exams": len(annotation), "patients": len({r["PatientID"] for r in annotation})},
    )
    validation.add_check(  # noqa: F821
        "Class balance",
        "success" if fraction >= MIN_POSITIVE_FRACTION else "warning",
        f"{positives} positive exams ({fraction:.1%})"
        + ("" if fraction >= MIN_POSITIVE_FRACTION else " — very few positives; the site will contribute little signal"),
        details={"distribution": distribution},
    )
    if unexpected:
        validation.add_check(  # noqa: F821
            "Label values", "error",
            f"{len(unexpected)} labels are not integers; training will fail",
            details={"examples": dict(list(unexpected.items())[:10])},
        )
    else:
        unknown = set(labels) - EXPECTED_CLASSES
        validation.add_check(  # noqa: F821
            "Label values",
            "success" if not unknown else "error",
            "All labels within the agreed coding" if not unknown
            else f"Unexpected label values: {sorted(unknown)} (agreed: {sorted(EXPECTED_CLASSES)})",
        )

    # 4. UID conventions and duplicates
    uids = [r["UID"] for r in annotation]
    duplicates = sorted({u for u in uids if uids.count(u) > 1})
    malformed = [u for u in uids if not re.search(r"_(left|right)$", u)]
    validation.add_check(  # noqa: F821
        "Unique exam identifiers",
        "success" if not duplicates else "error",
        "No duplicates" if not duplicates else f"{len(duplicates)} duplicated UIDs",
        details={"duplicates": duplicates[:10]},
    )
    validation.add_check(  # noqa: F821
        "Laterality suffix",
        "success" if not malformed else "warning",
        "Every UID ends in _left/_right" if not malformed
        else f"{len(malformed)} UIDs without a laterality suffix; the unilateral pipeline expects one",
        details={"examples": malformed[:10]},
    )

    # 5. Split coverage — every annotated exam must be assigned, and only once
    split_by_uid = {}
    split_dupes = []
    for row in split:
        if row["UID"] in split_by_uid:
            split_dupes.append(row["UID"])
        split_by_uid[row["UID"]] = row["Split"]
    unassigned = sorted(set(uids) - set(split_by_uid))
    orphan = sorted(set(split_by_uid) - set(uids))
    counts = {}
    for uid in uids:
        counts[split_by_uid.get(uid, "MISSING")] = counts.get(split_by_uid.get(uid, "MISSING"), 0) + 1
    validation.add_check(  # noqa: F821
        "Split assignment",
        "success" if not (unassigned or orphan or split_dupes) else "error",
        f"train/val/test counts: {counts}",
        details={
            "annotated_but_unassigned": unassigned[:10],
            "assigned_but_not_annotated": orphan[:10],
            "duplicated_in_split": split_dupes[:10],
        },
    )

    # 6. Patient-level leakage between splits
    patient_splits = {}
    for row in annotation:
        patient_splits.setdefault(row["PatientID"], set()).add(split_by_uid.get(row["UID"], "MISSING"))
    leaking = sorted(p for p, s in patient_splits.items() if len(s - {"MISSING"}) > 1)
    validation.add_check(  # noqa: F821
        "Patient-level split integrity",
        "success" if not leaking else "error",
        "No patient appears in more than one split" if not leaking
        else f"{len(leaking)} patients appear in more than one split — results would be optimistic",
        details={"examples": leaking[:10]},
    )

    # 7. Imaging present for each exam (sampled). The manifest lists files only, so
    # exists() is false for any folder; look for the exam's folder among file parents.
    folders = {path.rsplit("/", 1)[0] for path in manifest if "/" in path}
    missing_dir, missing_series = [], {}
    for uid in uids[:DEEP_CHECK_LIMIT]:
        if f"{data_dir}/{uid}" not in folders:
            missing_dir.append(uid)
            continue
        absent = [s for s in REQUIRED_SERIES if not validation.exists(f"{data_dir}/{uid}/{s}")]  # noqa: F821
        if absent:
            missing_series[uid] = absent
    checked = min(len(uids), DEEP_CHECK_LIMIT)
    ok = not missing_dir and not missing_series
    validation.add_check(  # noqa: F821
        "Imaging files present",
        "success" if ok else "error",
        f"Checked {checked} of {len(uids)} exams; "
        + ("all required series found" if ok
           else f"{len(missing_dir)} without a folder, {len(missing_series)} missing a required series"),
        details={
            "checked": checked,
            "required_series": REQUIRED_SERIES,
            "missing_folder": missing_dir[:10],
            "missing_series": dict(list(missing_series.items())[:10]),
        },
    )

    # 8. Optional series availability — affects which model configurations are usable
    have_optional = {s: 0 for s in OPTIONAL_SERIES}
    for uid in uids[:DEEP_CHECK_LIMIT]:
        for s in OPTIONAL_SERIES:
            if validation.exists(f"{data_dir}/{uid}/{s}"):  # noqa: F821
                have_optional[s] += 1
    validation.add_check(  # noqa: F821
        "Dynamic series coverage",
        "success",
        "Post-contrast series available per exam (of "
        f"{min(len(uids), DEEP_CHECK_LIMIT)} checked): "
        + ", ".join(f"{k.replace('.nii.gz', '')}={v}" for k, v in have_optional.items()),
        details=have_optional,
    )

    validation.add_check(  # noqa: F821
        "Validation complete", "success",
        "Review any warnings with the site's data steward before joining the study.",
    )


main()
