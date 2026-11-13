"""Data checks for the ecg_ptbxl template, run by SwarmMedHub in its sandbox.

SwarmMedHub runs this script with a helper object ``validation`` (``add_check``, ``open``,
``listdir``, ``exists``) against the project's registered data and lists every check with its
status: ok, warning or error. The script checks the files that prepare_data.py writes, in either
layout: one folder per FLARE site (``fl-client-1/train.npz``, ...) or one site's files at the
data root (``train.npz``, ...). It reads one .npz file at a time and processes the ECG array in
chunks, which keeps it within the sandbox's 1 GB of memory and 5 minutes.
"""

import io
import json
import re

import numpy as np

SPLITS = ("train", "val", "test")
N_LEADS, N_SAMPLES = 12, 1000
SUPERCLASSES = ["NORM", "MI", "STTC", "CD", "HYP"]
MIN_CLASS_CASES = 5  # fewer cases of a class make a split's AUROC uninformative
CHUNK = 512
FLAT_RANGE_UV = (
    1  # a lead whose peak-to-peak range is at most this many microvolts is flat
)
LARGE_UV = 30000  # an int16 peak this close to the limit (32,767) suggests clipping
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


def read_npz(path, names):
    with validation.open(path, "rb") as handle:
        data = handle.read()
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        missing = [name for name in names if name not in npz.files]
        if missing:
            return {}, missing
        return {name: npz[name] for name in names}, []


def read_json(path):
    with validation.open(path, "r") as handle:
        return json.load(handle)


def signal_stats(X):
    """Peak amplitude, number of flat leads and the share of records with a flat lead."""
    peak, flat_leads, flat_records = 0, 0, 0
    for start in range(0, len(X), CHUNK):
        block = X[start : start + CHUNK].astype(np.int32)
        peak = max(peak, int(np.abs(block).max()))
        flat = (block.max(axis=2) - block.min(axis=2)) <= FLAT_RANGE_UV
        flat_leads += int(flat.sum())
        flat_records += int(flat.any(axis=1).sum())
    return peak, flat_leads, flat_records


def check_site(label, prefix, keys, seen_ids):
    """Run all checks for one site; return {split: n} or None if its files are unreadable."""
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

    info = {}
    if f"{prefix}site_info.json" in keys:
        try:
            info = read_json(f"{prefix}site_info.json")
        except ValueError as error:
            check(f"{label}: site_info.json", "error", f"Not valid JSON: {error}")

    counts, ids = {}, {}
    for split in SPLITS:
        arrays, absent = read_npz(
            f"{prefix}{split}.npz", ("X", "y", "y_super", "ecg_id")
        )
        if absent:
            check(
                f"{label}: {split} arrays",
                "error",
                f"Arrays {absent} missing; run prepare_data.py.",
            )
            continue
        X, y, y_super, ecg_id = (
            arrays["X"],
            arrays["y"],
            arrays["y_super"],
            arrays["ecg_id"],
        )
        n = len(y)
        counts[split] = n
        ids[split] = ecg_id
        problems = []
        if (
            X.dtype != np.int16
            or X.ndim != 3
            or X.shape[1:] != (N_LEADS, N_SAMPLES)
            or len(X) != n
        ):
            problems.append(
                f"X is {X.dtype} {X.shape}, expected int16 (N, {N_LEADS}, {N_SAMPLES})"
            )
        if y.ndim != 1 or not np.isin(y, (0, 1)).all():
            problems.append("y must hold one 0/1 label per record")
        if (
            y_super.shape != (n, len(SUPERCLASSES))
            or not np.isin(y_super, (0, 1)).all()
        ):
            problems.append(f"y_super must be 0/1 with shape (N, {len(SUPERCLASSES)})")
        elif not np.array_equal(y_super[:, 1:].max(axis=1), y):
            problems.append("y is not 'any abnormal superclass' of y_super")
        if ecg_id.shape != (n,) or len(np.unique(ecg_id)) != n:
            problems.append("ecg_id must be one unique number per record")
        if problems:
            check(f"{label}: {split} arrays", "error", "; ".join(problems))
            continue
        check(
            f"{label}: {split} arrays",
            "ok",
            f"{n} records; X, y, y_super and ecg_id are consistent.",
        )

        # Signal sanity
        peak, flat_leads, flat_records = signal_stats(X)
        if peak >= LARGE_UV:
            check(
                f"{label}: {split} amplitude",
                "warning",
                f"Peak amplitude {peak} uV is close to the int16 limit.",
            )
        if flat_records:
            check(
                f"{label}: {split} flat leads",
                "warning",
                f"{flat_records} of {n} records have a flat lead ({flat_leads} leads in all).",
                {"records": flat_records, "leads": flat_leads},
            )
        if peak < LARGE_UV and not flat_records:
            check(
                f"{label}: {split} signal",
                "ok",
                f"Peak amplitude {peak} uV; no flat leads.",
            )

        # Classes
        n_abnormal = int(y.sum())
        small = min(n_abnormal, n - n_abnormal)
        status = "ok" if small >= MIN_CLASS_CASES else "warning"
        message = f"{n - n_abnormal} normal, {n_abnormal} abnormal."
        if status == "warning":
            message += f" Fewer than {MIN_CLASS_CASES} cases of one class: the AUROC is uninformative."
        check(
            f"{label}: {split} classes",
            status,
            message,
            {"normal": n - n_abnormal, "abnormal": n_abnormal},
        )

    # Records and splits must not overlap (PTB-XL folds keep a patient in one fold)
    for a in range(len(SPLITS)):
        for b in range(a + 1, len(SPLITS)):
            if SPLITS[a] in ids and SPLITS[b] in ids:
                shared = len(np.intersect1d(ids[SPLITS[a]], ids[SPLITS[b]]))
                if shared:
                    check(
                        f"{label}: {SPLITS[a]}/{SPLITS[b]} overlap",
                        "error",
                        f"{shared} records in both splits.",
                    )
    if len(ids) == len(SPLITS) and not any(
        len(np.intersect1d(ids[SPLITS[a]], ids[SPLITS[b]]))
        for a in range(len(SPLITS))
        for b in range(a + 1, len(SPLITS))
    ):
        check(f"{label}: split overlap", "ok", "No record appears in two splits.")

    # Case ids must fit the explanation viewer's pattern and stay unique across sites
    flat = np.concatenate(list(ids.values())) if ids else np.array([], dtype=np.int64)
    bad = [int(i) for i in flat[:50] if not CASE_ID_PATTERN.match(str(int(i)))]
    if bad:
        check(
            f"{label}: case ids",
            "error",
            f"Record numbers {bad[:5]} cannot be used as case ids.",
        )
    clash = [int(i) for i in flat if int(i) in seen_ids]
    if clash:
        check(
            f"{label}: record overlap",
            "warning",
            f"{len(clash)} records also appear at an earlier site.",
        )
    seen_ids.update(int(i) for i in flat)

    # site_info.json against the arrays
    if info and counts:
        stated = {
            split: info.get("counts", {}).get(split, {}).get("n") for split in counts
        }
        wrong = {
            s: (stated[s], counts[s])
            for s in counts
            if stated[s] is not None and stated[s] != counts[s]
        }
        if wrong:
            check(
                f"{label}: site_info.json",
                "error",
                f"Record counts differ from the arrays: {wrong}.",
            )
        else:
            check(
                f"{label}: site_info.json",
                "ok",
                f"Source PTB-XL {info.get('version', '?')}; counts agree with the arrays.",
            )
    return counts


def main():
    keys = set(validation.listdir(""))
    sites = find_sites(keys)
    if not sites:
        check(
            "data layout",
            "error",
            "No train.npz at the root and no <site>/train.npz folders; run prepare_data.py and register its output folder.",
        )
        return
    layout = (
        "one folder per site"
        if "data root" not in sites
        else "one site at the data root"
    )
    check("data layout", "ok", f"{len(sites)} site(s), {layout}: {', '.join(sites)}.")
    seen_ids, totals = set(), {}
    for label, prefix in sites.items():
        counts = check_site(label, prefix, keys, seen_ids)
        if counts:
            totals[label] = counts
    if totals:
        pooled = {s: sum(c.get(s, 0) for c in totals.values()) for s in SPLITS}
        check(
            "totals",
            "ok",
            f"Records over {len(totals)} site(s): "
            + ", ".join(f"{s} {n}" for s, n in pooled.items())
            + ".",
            pooled,
        )


main()
