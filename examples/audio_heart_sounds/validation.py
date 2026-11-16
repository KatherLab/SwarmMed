"""Data checks for the heart sound template, run by SwarmMedHub in its sandbox.

SwarmMedHub provides the `validation` helper (add_check, open, listdir, exists).
The registered data holds either one site's files at the root (site_info.json,
train.npz, val.npz, test.npz) or one folder per site (fl-client-1/, ...), as written
by prepare_data.py; every site found is checked. Folders holdout-training-x/ hold
only test.npz.

Checks per site:
  1. the expected files exist;
  2. site_info.json can be read, names its source and holds 32 per-band means and
     standard deviations (std > 0) for normalisation;
  3. the preprocessing matches what training.py expects (32 mel bands, 150 frames);
  4. every .npz file has the expected arrays, dtypes and shapes; labels are 0 or 1;
     every segment points to a recording and carries that recording's label; values
     are finite; the counts equal those in site_info.json;
  5. no recording appears in more than one split;
  6. a warning when a split has fewer than 5 recordings of a class.
"""

import io
import json

import numpy as np

SPLITS = ("train", "val", "test")
N_MELS = 32
SEGMENT_FRAMES = 150
MIN_RECORDINGS_PER_CLASS = 5
SEGMENT_DTYPES = {
    "x": np.float16,
    "y": np.int8,
    "rec": np.int32,
    "seg_start_s": np.float32,
}
RECORDING_DTYPES = {
    "rec_ids": np.str_,
    "rec_y": np.int8,
    "rec_sqi": np.int8,
    "rec_dur_s": np.float32,
}

counts = {"ok": 0, "warning": 0, "error": 0}


def check(name, status, message, details=None):
    """Record one check and count it by status."""
    validation.add_check(name, status, message, details or {})
    counts[status] += 1


def read_bytes(path):
    """Read a registered file completely."""
    with validation.open(path, "rb") as handle:
        return handle.read()


def find_sites(keys):
    """Folder prefixes holding a site_info.json: '' (root) or 'name/' (one level down)."""
    prefixes = []
    for key in keys:
        if key.endswith("site_info.json"):
            prefix = key[: -len("site_info.json")]
            if prefix == "" or (prefix.endswith("/") and prefix.count("/") == 1):
                prefixes.append(prefix)

    def order(prefix):  # root first, then fl-client-1, fl-client-2, ..., fl-client-10
        name = prefix.rstrip("/")
        tail = name.rsplit("-", 1)[-1]
        return (
            prefix != "",
            name.rsplit("-", 1)[0],
            int(tail) if tail.isdigit() else 0,
            name,
        )

    return sorted(set(prefixes), key=order)


def check_info(info):
    """Check site_info.json; return a list of problems."""
    problems = []
    for field in (
        "dataset",
        "source",
        "label_map",
        "normalisation",
        "splits",
        "preprocessing",
    ):
        if field not in info:
            problems.append(f"missing field '{field}'")
    norm = info.get("normalisation", {})
    mean = np.asarray(norm.get("mean", []), dtype=np.float64)
    std = np.asarray(norm.get("std", []), dtype=np.float64)
    if mean.shape != (N_MELS,) or std.shape != (N_MELS,):
        problems.append(f"normalisation needs {N_MELS} means and stds")
    elif not (
        np.all(np.isfinite(mean)) and np.all(np.isfinite(std)) and np.all(std > 0)
    ):
        problems.append("normalisation values must be finite with std > 0")
    pre = info.get("preprocessing", {})
    if (
        pre.get("mel", {}).get("n_mels") != N_MELS
        or pre.get("segment_frames") != SEGMENT_FRAMES
    ):
        problems.append(
            f"training.py expects {N_MELS} mel bands and {SEGMENT_FRAMES}-frame segments"
        )
    return problems


def check_npz(arrays, expected):
    """Check one split file's arrays against the format; return (problems, counts)."""
    problems = []
    for key, dtype in {**SEGMENT_DTYPES, **RECORDING_DTYPES}.items():
        if key not in arrays:
            problems.append(f"missing array '{key}'")
        elif not np.issubdtype(arrays[key].dtype, dtype):
            problems.append(
                f"'{key}' has dtype {arrays[key].dtype}, expected {np.dtype(dtype).name}"
            )
    if problems:
        return problems, {}
    x, y, rec, rec_y = arrays["x"], arrays["y"], arrays["rec"], arrays["rec_y"]
    n_seg, n_rec = len(y), len(arrays["rec_ids"])
    if x.shape != (n_seg, N_MELS, SEGMENT_FRAMES):
        problems.append(
            f"x has shape {x.shape}, expected ({n_seg}, {N_MELS}, {SEGMENT_FRAMES})"
        )
    if len(rec) != n_seg or len(arrays["seg_start_s"]) != n_seg:
        problems.append("segment arrays differ in length")
    if any(len(arrays[k]) != n_rec for k in RECORDING_DTYPES):
        problems.append("recording arrays differ in length")
    if n_seg == 0 or n_rec == 0:
        problems.append("no segments or no recordings")
    if problems:
        return problems, {}
    if not (set(np.unique(y)) <= {0, 1} and set(np.unique(rec_y)) <= {0, 1}):
        problems.append("labels other than 0 and 1")
    if not set(np.unique(arrays["rec_sqi"])) <= {0, 1}:
        problems.append("rec_sqi values other than 0 and 1")
    if rec.min() < 0 or rec.max() >= n_rec:
        problems.append("rec points outside rec_ids")
    else:
        if len(np.unique(rec)) != n_rec:
            problems.append("some recordings have no segments")
        if not np.array_equal(y, rec_y[rec]):
            problems.append("segment labels differ from their recording's label")
        if np.any(arrays["seg_start_s"] < 0) or np.any(
            arrays["seg_start_s"] > arrays["rec_dur_s"][rec]
        ):
            problems.append("segment start times outside their recording")
    if not np.all(np.isfinite(x)):
        problems.append("x has non-finite values")
    if len(np.unique(arrays["rec_ids"])) != n_rec:
        problems.append("duplicate recording IDs")
    found = {
        "recordings": n_rec,
        "recordings_abnormal": int(rec_y.sum()),
        "segments": n_seg,
        "segments_abnormal": int(y.sum()),
    }
    for key, value in found.items():
        if expected.get(key) != value:
            problems.append(f"{key} = {value}, site_info.json says {expected.get(key)}")
    return problems, found


check(
    "Validation started",
    "ok",
    "Checking heart sound segment files written by prepare_data.py",
)
keys = [k for k in validation.listdir("") if not k.startswith("results/")]
sites = find_sites(keys)
if not sites:
    check(
        "Data layout",
        "error",
        "No site_info.json found at the root or in a site folder (e.g. fl-client-1/). "
        "Run prepare_data.py and import its output.",
        {"files": sorted(keys)[:20]},
    )
else:
    names = [p.rstrip("/") or "(root)" for p in sites]
    layout = "one folder per site" if sites[0] else "one site's files at the root"
    check("Data layout", "ok", f"{layout}: {', '.join(names)}", {"sites": names})

for prefix in sites:
    site = prefix.rstrip("/") or "(root)"
    try:
        info = json.loads(read_bytes(prefix + "site_info.json").decode("utf-8"))
    except Exception as exc:
        check(f"{site}: site_info.json", "error", f"Cannot read site_info.json: {exc}")
        continue
    problems = check_info(info)
    source = info.get("source", "?")
    if problems:
        check(f"{site}: site_info.json", "error", "; ".join(problems))
        continue
    check(
        f"{site}: site_info.json",
        "ok",
        f"Source {source}; normalisation and preprocessing as expected",
    )

    splits = [s for s in SPLITS if s in info.get("splits", {})]
    missing = [
        prefix + f"{s}.npz"
        for s in splits
        if not validation.exists(prefix + f"{s}.npz")
    ]
    if missing or not splits:
        check(
            f"{site}: files", "error", f"Missing files: {missing or 'no splits listed'}"
        )
        continue
    check(
        f"{site}: files",
        "ok",
        f"{', '.join(s + '.npz' for s in splits)} and site_info.json present",
    )

    record_ids, class_counts = {}, {}
    for split in splits:
        name = f"{site}: {split}.npz"
        try:
            with np.load(
                io.BytesIO(read_bytes(prefix + f"{split}.npz")), allow_pickle=False
            ) as z:
                arrays = {k: z[k] for k in z.files}
        except Exception as exc:
            check(name, "error", f"Cannot read the file: {exc}")
            continue
        problems, found = check_npz(arrays, info["splits"][split])
        if problems:
            check(name, "error", "; ".join(problems))
            continue
        record_ids[split] = set(arrays["rec_ids"].tolist())
        class_counts[split] = (
            found["recordings"] - found["recordings_abnormal"],
            found["recordings_abnormal"],
        )
        check(
            name,
            "ok",
            f"{found['recordings']} recordings ({found['recordings_abnormal']} abnormal), "
            f"{found['segments']} segments ({found['segments_abnormal']} abnormal)",
            found,
        )
        del arrays

    overlap = {
        f"{a}/{b}": sorted(record_ids[a] & record_ids[b])[:5]
        for i, a in enumerate(record_ids)
        for b in list(record_ids)[i + 1 :]
        if record_ids[a] & record_ids[b]
    }
    if overlap:
        check(
            f"{site}: split overlap",
            "error",
            "Recordings appear in more than one split",
            overlap,
        )
    elif len(record_ids) > 1:
        check(
            f"{site}: split overlap",
            "ok",
            "No recording appears in more than one split",
        )

    small = {
        split: {"normal": n0, "abnormal": n1}
        for split, (n0, n1) in class_counts.items()
        if min(n0, n1) < MIN_RECORDINGS_PER_CLASS
    }
    if small:
        check(
            f"{site}: class balance",
            "warning",
            f"Fewer than {MIN_RECORDINGS_PER_CLASS} recordings of a class in {', '.join(small)}; "
            "metrics on these splits are very uncertain",
            small,
        )
    elif class_counts:
        check(
            f"{site}: class balance",
            "ok",
            "Every split has at least 5 recordings of each class",
        )

check(
    "Validation summary",
    "ok" if counts["error"] == 0 else "error",
    f"{len(sites)} site(s) checked: {counts['error']} errors, {counts['warning']} warnings",
)
