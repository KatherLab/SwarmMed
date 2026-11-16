"""Data checks for the EEG sleep-staging template (run by SwarmMedHub in its sandbox).

The sandbox defines the object `validation` (add_check, open, listdir, exists).
The registered data holds one site's files at its root, or one folder per site
(fl-client-1/, ...). For every site found, the checks are:

1. train.npz, val.npz, test.npz and site_info.json are present and readable.
2. site_info.json names Sleep-EDF Expanded 1.0.0, 100 Hz and the two EEG channels.
3. Each split has the arrays X (float16, N x 2 x 3000), y (int8, 0-4), subject,
   night and epoch of equal length; X is finite and within [-20, 20].
4. Counts per stage and the array fingerprints equal those in site_info.json.
5. No subject is in more than one split, or at more than one site.
6. Warning when a stage has fewer than 20 epochs in a validation or test split.

Files are read one at a time and checked in chunks (sandbox: 1 CPU, 1 GB RAM).
"""

import hashlib
import io
import json

import numpy as np

CLASS_NAMES = ["W", "N1", "N2", "N3", "REM"]
SPLITS = ["train", "val", "test"]
REQUIRED_FILES = [f"{split}.npz" for split in SPLITS] + ["site_info.json"]
ARRAY_DTYPES = {
    "X": "float16",
    "y": "int8",
    "subject": "int16",
    "night": "int8",
    "epoch": "int32",
}
N_CHANNELS = 2
EPOCH_SAMPLES = 3000
CLIP = 20.0
MIN_EVAL_STAGE_EPOCHS = 20
CHUNK = 1024  # epochs per chunk in the value checks

summary = {"ok": 0, "warning": 0, "error": 0}


def check(name, status, message, details=None):
    """Report one check and count it."""
    validation.add_check(name, status, message, details or {})
    summary[status] += 1


def site_locations(keys):
    """Return [(label, prefix)] for the root and each top-level folder with site files."""
    locations = []
    if any(key in REQUIRED_FILES for key in keys):
        locations.append(("data root", ""))
    folders = sorted(
        {
            key.split("/")[0]
            for key in keys
            if key.count("/") == 1 and key.split("/")[1] in REQUIRED_FILES
        }
    )
    locations += [(folder, folder + "/") for folder in folders]
    return locations


def read_arrays(path):
    """Read one .npz file completely, then return its arrays."""
    with validation.open(path, "rb") as handle:
        raw = handle.read()
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def fingerprint(array):
    """SHA-256 of the array's bytes in C order (as written by prepare_data.py)."""
    contiguous = np.ascontiguousarray(array)
    return hashlib.sha256(contiguous.view(np.uint8).reshape(-1)).hexdigest()


def check_arrays(label, split, arrays, info):
    """Check one split's arrays; return its subjects and stage counts."""
    name = f"{label}: {split}.npz"
    problems = []
    for key, dtype in ARRAY_DTYPES.items():
        if key not in arrays:
            problems.append(f"missing array {key}")
        elif str(arrays[key].dtype) != dtype:
            problems.append(f"{key} is {arrays[key].dtype}, expected {dtype}")
    if problems:
        check(name, "error", "; ".join(problems))
        return None
    x, y = arrays["X"], arrays["y"]
    n = len(y)
    if x.shape != (n, N_CHANNELS, EPOCH_SAMPLES):
        problems.append(f"X has shape {x.shape}, expected ({n}, 2, 3000)")
    if any(len(arrays[key]) != n for key in ("subject", "night", "epoch")):
        problems.append("subject, night or epoch length differs from y")
    if n == 0:
        problems.append("no epochs")
    elif y.min() < 0 or y.max() >= len(CLASS_NAMES):
        problems.append(f"labels outside 0-4 (min {y.min()}, max {y.max()})")
    if problems:
        check(name, "error", "; ".join(problems))
        return None

    finite, low, high, at_limit = True, np.inf, -np.inf, 0
    for start in range(0, n, CHUNK):
        block = x[start : start + CHUNK]
        finite = finite and bool(np.isfinite(block).all())
        low = min(low, float(block.min()))
        high = max(high, float(block.max()))
        at_limit += int(np.count_nonzero(np.abs(block) >= CLIP))
    counts = np.bincount(y.astype(np.int64), minlength=len(CLASS_NAMES))
    stage_counts = {c: int(v) for c, v in zip(CLASS_NAMES, counts, strict=True)}
    subjects = sorted({int(s) for s in arrays["subject"]})
    details = {"epochs": n, "subjects": subjects, **stage_counts}
    if not finite or low < -CLIP or high > CLIP:
        check(
            name,
            "error",
            f"X values not finite or outside [-{CLIP:g}, {CLIP:g}] "
            f"(min {low:.2f}, max {high:.2f})",
            details,
        )
    else:
        share = 100.0 * at_limit / x.size
        check(
            name,
            "ok",
            f"{n} epochs from subject(s) {subjects}; stages {stage_counts}; "
            f"{share:.3f}% of samples at the clip limit",
            details,
        )

    expected_counts = (info.get("counts") or {}).get(split)
    expected_prints = (info.get("fingerprints") or {}).get(split)
    if expected_counts is None or expected_prints is None:
        check(
            f"{label}: {split} vs site_info.json",
            "warning",
            "no counts or fingerprints",
        )
    else:
        found_counts = {"n": n, **stage_counts}
        found_prints = {"X_sha256": fingerprint(x), "y_sha256": fingerprint(y)}
        if found_counts != expected_counts:
            check(
                f"{label}: {split} vs site_info.json",
                "error",
                f"counts {found_counts} differ from site_info.json {expected_counts}",
            )
        elif found_prints != expected_prints:
            check(
                f"{label}: {split} vs site_info.json",
                "error",
                "arrays differ from the fingerprints in site_info.json "
                "(file changed, or files from different runs of prepare_data.py)",
            )
        else:
            check(
                f"{label}: {split} vs site_info.json",
                "ok",
                "counts and array fingerprints match",
            )

    if split in ("val", "test"):
        small = {c: v for c, v in stage_counts.items() if v < MIN_EVAL_STAGE_EPOCHS}
        if small:
            check(
                f"{label}: {split} stage balance",
                "warning",
                f"stages with fewer than {MIN_EVAL_STAGE_EPOCHS} epochs: {small}. "
                "Per-site F1 for these stages is unstable; use pooled metrics.",
                stage_counts,
            )
    return {"subjects": subjects, "counts": stage_counts}


def check_site(label, prefix):
    """Run all checks for one site; return {split: subjects}."""
    missing = [f for f in REQUIRED_FILES if not validation.exists(prefix + f)]
    if missing:
        check(f"{label}: files", "error", f"missing {missing}")
        if "site_info.json" in missing:
            return {}
    else:
        check(f"{label}: files", "ok", "train.npz, val.npz, test.npz, site_info.json")

    try:
        with validation.open(prefix + "site_info.json", "r") as handle:
            info = json.load(handle)
    except Exception as exc:
        check(f"{label}: site_info.json", "error", f"cannot read: {exc}")
        return {}
    expected = {
        "version": "1.0.0",
        "fs": 100,
        "channels": ["EEG Fpz-Cz", "EEG Pz-Oz"],
    }
    differs = {k: info.get(k) for k, v in expected.items() if info.get(k) != v}
    if "Sleep-EDF" not in str(info.get("dataset", "")) or differs:
        check(
            f"{label}: site_info.json",
            "warning",
            f"not the expected Sleep-EDF Expanded 1.0.0 setup: {differs}",
        )
    else:
        message = f"Sleep-EDF Expanded 1.0.0, site {info.get('site')}"
        if prefix and info.get("site") not in (None, label):
            check(
                f"{label}: site_info.json",
                "warning",
                f"{message}, but the folder is named {label}",
            )
        else:
            check(f"{label}: site_info.json", "ok", message)

    subjects = {}
    for split in SPLITS:
        path = f"{prefix}{split}.npz"
        if not validation.exists(path):
            continue
        try:
            arrays = read_arrays(path)
        except Exception as exc:
            check(f"{label}: {split}.npz", "error", f"cannot read: {exc}")
            continue
        result = check_arrays(label, split, arrays, info)
        del arrays
        if result is not None:
            subjects[split] = result["subjects"]

    overlap = {
        f"{a}/{b}": sorted(set(subjects[a]) & set(subjects[b]))
        for i, a in enumerate(subjects)
        for b in list(subjects)[i + 1 :]
        if set(subjects[a]) & set(subjects[b])
    }
    if overlap:
        check(f"{label}: subject split", "error", f"subjects in two splits: {overlap}")
    elif subjects:
        check(
            f"{label}: subject split",
            "ok",
            "no subject in more than one split: "
            + ", ".join(f"{s} {len(v)}" for s, v in subjects.items()),
        )
    return subjects


check("Validation started", "ok", "EEG sleep staging (Sleep-EDF Expanded) data checks")
keys = validation.listdir("")
locations = site_locations(keys)
if not locations:
    nested = sorted({k.rsplit("/", 1)[0] for k in keys if k.endswith("/train.npz")})
    hint = (
        f" Found train.npz deeper in: {nested[:5]}; import each site folder itself "
        "(fl-client-1/train.npz) or one site's files at the root."
        if nested
        else " Run prepare_data.py and import its output."
    )
    check(
        "Site data",
        "error",
        "No train.npz, val.npz, test.npz or site_info.json at the root of the data or "
        "in a top-level folder." + hint,
    )
else:
    check(
        "Site data",
        "ok",
        f"{len(locations)} location(s): " + ", ".join(label for label, _ in locations),
    )
    per_site = {label: check_site(label, prefix) for label, prefix in locations}
    if len(per_site) > 1:
        owners = {}
        for label, splits in per_site.items():
            for split_subjects in splits.values():
                for subject in split_subjects:
                    owners.setdefault(subject, set()).add(label)
        shared = {s: sorted(v) for s, v in sorted(owners.items()) if len(v) > 1}
        if shared:
            check("Subjects across sites", "error", f"subjects at two sites: {shared}")
        else:
            check(
                "Subjects across sites",
                "ok",
                f"{len(owners)} subjects, each at one site only",
            )

check(
    "Validation summary",
    "error" if summary["error"] else ("warning" if summary["warning"] else "ok"),
    f"{summary['error']} error(s), {summary['warning']} warning(s), "
    f"{summary['ok']} check(s) passed",
)
