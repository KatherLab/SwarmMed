#!/usr/bin/env python3
"""Convert PTB-XL 1.0.3 (100 Hz records) into the data layout of the multimodal_ptbxl template.

A site runs this script before it registers its data in SwarmMed. It reads the PhysioNet
release (optionally downloading the needed files first), assigns each record to a site and a
split, and writes compressed NumPy files:

    OUT/fl-client-k/{train,val,test}.npz + site_info.json    (--layout local-test, k = 1..4)
    OUT/{train,val,test}.npz + site_info.json                (--layout single --site-index k)

Sites follow PTB-XL's pseudonymised recording-site field: site 0 -> fl-client-1, site 1 ->
fl-client-2, site 2 -> fl-client-3, every other site and records without one -> fl-client-4.
Inside each site, strat_fold 1-8 is train, fold 9 is val and fold 10 is test.

Each .npz holds, sorted by ecg_id, the arrays of the ecg_ptbxl template (identical values):
    X        int16 (N, 12, 1000)  microvolts, 10 s at 100 Hz, leads I II III AVR AVL AVF V1-V6
    y        int8  (N,)           0 = normal (NORM is the only diagnostic superclass),
                                  1 = abnormal (any of MI, STTC, CD, HYP)
    y_super  int8  (N, 5)         multi-hot over NORM, MI, STTC, CD, HYP
    ecg_id   int32 (N,)           PTB-XL's own pseudonymous record number
and the patient metadata:
    tab      float32 (N, 6)       age_z, sex_female, height_z, height_missing, weight_z,
                                  weight_missing (fixed constants, see TAB_FEATURES)
    age      float32 (N,)         years; PTB-XL stores ages of 90 or more as 300, mapped to 90
    sex      int8    (N,)         0 = male, 1 = female
    height   float32 (N,)         cm, NaN when missing or implausible
    weight   float32 (N,)         kg, NaN when missing or implausible

The files therefore also work with the ecg_ptbxl template, which ignores the metadata.
No filtering, resampling or scaling is applied to the signals; training.py normalises each
record when it loads it. After writing, the script reads the files back, prints the counts and
checks them (and SHA-256 fingerprints of the arrays) against the reference preparation.

Only numpy, pandas and the Python standard library are used.

Examples:
    python prepare_data.py --raw-dir ptbxl/raw --out ptbxl/prepared/local-test
    python prepare_data.py --raw-dir ptbxl/raw --out site2 --layout single --site-index 2
    python prepare_data.py --raw-dir ptbxl/raw --download --out ptbxl/prepared/local-test
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

TEMPLATE = "multimodal_ptbxl"

# --------------------------------------------------------------------------------------------
# Dataset facts
# --------------------------------------------------------------------------------------------

DATASET = {
    "dataset": "PTB-XL, a large publicly available electrocardiography dataset",
    "version": "1.0.3",
    "doi": "10.13026/kfzx-aw45",
    "licence": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "source_url": "https://physionet.org/content/ptb-xl/1.0.3/",
    "citation": (
        "Wagner P, Strodthoff N, Bousseljot R, Samek W, Schaeffter T. PTB-XL, a large publicly "
        "available electrocardiography dataset (version 1.0.3). PhysioNet; 2022. "
        "https://doi.org/10.13026/kfzx-aw45"
    ),
}

LEADS = ("I", "II", "III", "AVR", "AVL", "AVF", "V1", "V2", "V3", "V4", "V5", "V6")
SUPERCLASSES = ("NORM", "MI", "STTC", "CD", "HYP")
FS_HZ = 100
N_SAMPLES = 1000
SITES = ("fl-client-1", "fl-client-2", "fl-client-3", "fl-client-4")
RECORDING_SITES = {
    "fl-client-1": [0],
    "fl-client-2": [1],
    "fl-client-3": [2],
    "fl-client-4": "other",
}
SPLITS = ("train", "val", "test")
FOLDS = {"train": [1, 2, 3, 4, 5, 6, 7, 8], "val": [9], "test": [10]}
LABEL_MAP = {"0": "normal", "1": "abnormal"}
ARRAYS = ("X", "y", "y_super", "ecg_id")
METADATA_ARRAYS = ("tab", "age", "sex", "height", "weight")

# The tabular input: 6 values per record from fixed constants, identical at every site, so no
# statistics are computed from the data and no site has to share anything.
TAB_FEATURES = (
    "age_z",
    "sex_female",
    "height_z",
    "height_missing",
    "weight_z",
    "weight_missing",
)
TAB_DEFINITIONS = {
    "age_z": "(age_years - 60) / 20, with age_years = 90 where PTB-XL stores 300 (ages of 90 or more)",
    "sex_female": "sex (0 = male, 1 = female)",
    "height_z": "(height_cm - 170) / 10, or 0 when missing",
    "height_missing": "1 when height is missing, else 0",
    "weight_z": "(weight_kg - 75) / 15, or 0 when missing",
    "weight_missing": "1 when weight is missing, else 0",
}
PLAUSIBILITY_RULE = (
    "Applied before height_z, height_missing, weight_z and weight_missing: a value outside its "
    "range counts as missing. Adults (age_years >= 18): height 120-220 cm, weight 30-250 kg. "
    "Under 18: height 50-200 cm, weight 3-150 kg."
)

PREPROCESSING = (
    "Labels: a record's diagnostic superclasses are the diagnostic_class values of the "
    "diagnostic statements (scp_statements.csv, diagnostic == 1) among its scp_codes; "
    "likelihoods are ignored. Records without a diagnostic superclass are dropped. y = 1 if any "
    "of MI, STTC, CD, HYP is present, else 0 (NORM only). Signals: records100/*_lr.dat read as "
    "little-endian int16 (format 16, gain 1000/mV, baseline 0), reshaped to (12, 1000) in "
    "microvolts; the WFDB header layout, initial values and checksums are checked. 100 Hz, all "
    "10 s and all 12 leads are kept; no filtering, resampling or scaling. Records sorted by ecg_id."
)
NORMALISATION = (
    "Applied when the data are loaded (training.py, evaluate.py and the Hub scripts), not stored: "
    "x = X / 1000 (millivolts, float32), then each lead's median over the record is subtracted. "
    "The amplitude is not rescaled, and nothing is computed across records or sites."
)

# Counts of the reference preparation (2026-11-11): per site and split
# (n, abnormal, NORM, MI, STTC, CD, HYP).
EXPECTED_COUNTS = {
    "fl-client-1": {
        "train": (7691, 4453, 3340, 2238, 1713, 1619, 946),
        "val": (497, 175, 330, 74, 65, 65, 32),
        "test": (483, 173, 321, 78, 61, 71, 34),
    },
    "fl-client-2": {
        "train": (5038, 2848, 2330, 1045, 1425, 1127, 777),
        "val": (606, 351, 271, 131, 171, 165, 79),
        "test": (598, 337, 281, 124, 159, 125, 95),
    },
    "fl-client-3": {
        "train": (3318, 1925, 1483, 831, 768, 893, 260),
        "val": (834, 548, 299, 268, 215, 202, 109),
        "test": (867, 584, 300, 283, 210, 250, 83),
    },
    "fl-client-4": {
        "train": (1037, 615, 443, 265, 280, 268, 136),
        "val": (209, 158, 55, 67, 77, 63, 48),
        "test": (210, 152, 61, 65, 91, 50, 50),
    },
}
EXPECTED_RECORDS = 21799
EXPECTED_EXCLUDED = {
    "fl-client-1": 269,
    "fl-client-2": 52,
    "fl-client-3": 56,
    "fl-client-4": 34,
}

# Metadata of the reference preparation, per site and split (fraction with height missing,
# fraction with weight missing, fraction female, median age), rounded to 3 decimals; and the
# number of heights and weights (over all 21,799 records) set missing by the plausibility rule.
EXPECTED_METADATA = {
    "fl-client-1": {
        "train": (0.998, 0.762, 0.390, 56.0),
        "val": (0.996, 0.167, 0.414, 47.0),
        "test": (0.994, 0.178, 0.369, 46.0),
    },
    "fl-client-2": {
        "train": (0.056, 0.078, 0.541, 68.0),
        "val": (0.043, 0.069, 0.531, 67.0),
        "test": (0.062, 0.082, 0.554, 67.0),
    },
    "fl-client-3": {
        "train": (0.985, 0.985, 0.584, 68.0),
        "val": (0.964, 0.964, 0.494, 67.0),
        "test": (0.978, 0.978, 0.491, 68.0),
    },
    "fl-client-4": {
        "train": (0.336, 0.352, 0.511, 67.0),
        "val": (0.545, 0.569, 0.440, 66.0),
        "test": (0.486, 0.505, 0.538, 67.0),
    },
}
EXPECTED_IMPLAUSIBLE = {"height": 12, "weight": 3}

# SHA-256 over the arrays X, y, y_super and ecg_id of each file (see array_fingerprint()), from
# the reference preparation of 2026-11-11 (numpy 2.4.3, pandas 2.3.3). Equal fingerprints mean
# the arrays are identical, byte for byte.
EXPECTED_FINGERPRINTS: dict[str, dict[str, str]] = {
    "fl-client-1": {
        "train": "c87a58020851873cf429642169f4bfd6f5d834c2bbe7494f61e9d4ce6ad448f4",
        "val": "a0137ac376e3b344cdaf450e33ee44fdd3eb5776e220da46bd50ff36bfba7466",
        "test": "15eacc8893b234e0a44fa52eb0673ef7520cf5825b2d1f6426b3f9ca48a4303a",
    },
    "fl-client-2": {
        "train": "54a6cb2163587cfa8088e519f60d59290c08d501ce19adb03aded4db2793f628",
        "val": "e582154d1d3902cb3ca3bbbe1855e947b3bb10d31ce0b5b397137c7aa33a5305",
        "test": "6267a5546cde2cf6d9e31bbe2315d5d34ebc2081662ab523cbacb45a8e510a5d",
    },
    "fl-client-3": {
        "train": "111fab367768f45251ab6eb31c697436575c8c70c5d22cbc917472e90983eec8",
        "val": "9a5853e2d027734cc13ad9ba30291fe82262b0a75d2a7dd4a4ebd49f35117933",
        "test": "c6e1e9d2f4f4532e94954b93535da3261e159bd8cb88f8b64b6d369f45fcca62",
    },
    "fl-client-4": {
        "train": "96db66a86258164eed67cb11f59fd5e79fb4a8da4aa51fa7f9965a8ed06cd582",
        "val": "107f7c83c541bdf59524f1b3801bc7b7c7b85a73a0d422949ca63bb4980be74d",
        "test": "7efdb7da3a401589f78fd764255ecf85b541879c031f38b156825e7f65f65b57",
    },
}
# The same over tab, age, sex, height and weight.
EXPECTED_METADATA_FINGERPRINTS: dict[str, dict[str, str]] = {
    "fl-client-1": {
        "train": "5e97959047a9fac89e3e7b1ecfb6317ff46de72a7c79b989fa773fafda8778a9",
        "val": "6e0bcbcc827f4b67dd60602974c41a68d8a8569e071fac2668b22983e99aeb8b",
        "test": "dff1c1c137bfec585ab538dbe8f7830bcc5904ff74bb6a9f4a971f8bdcc703d9",
    },
    "fl-client-2": {
        "train": "91ae631d9bcf3d5f8125932cba683884dfe1e6f5400ed67582a2542ea2865ccd",
        "val": "3629b531e244efefc5b5d6234cb5ecbd10b418729e204266d9afc0959f29f5d3",
        "test": "391b2625b2cfac6cdc5c11c19c063f38942fc4be621e229ef1c6055c8cbeb0af",
    },
    "fl-client-3": {
        "train": "da56c9b31a3258b4323f67336b8e70b8eb83830685ae2fda006493158def9ec0",
        "val": "9b6677cf0261f728f301484af1b5f2de40daedd4e12e48956adbd9feb97e2fdd",
        "test": "557ddcfe37269260a9e3a16cd8ec31f85d12c31df6b0791e4702960d9d3b73ee",
    },
    "fl-client-4": {
        "train": "ae0dbd0f992238b39c00b0c945ce31451a679e1bdd3b76611b8676459a44cbac",
        "val": "b9d3c3b66b25f31a0271a47b6cb60f207724ffdda98b6e6f08127ea5ed5d6a8f",
        "test": "a26688bdce525e3cd88f1388c925da3d7404f0810b403deef902e2c16518ec09",
    },
}

# --------------------------------------------------------------------------------------------
# Optional download (PhysioNet; standard library only)
# --------------------------------------------------------------------------------------------

ZIP_URL = "https://physionet.org/content/ptb-xl/get-zip/1.0.3/"
FILES_URL = "https://physionet.org/files/ptb-xl/1.0.3/"
ROOT_FILES = (
    "ptbxl_database.csv",
    "scp_statements.csv",
    "LICENSE.txt",
    "SHA256SUMS.txt",
)
RECORDS_PREFIX = "records100/"
CHUNK_BYTES = 8 * 1024 * 1024
TAIL_BYTES = 32 * 1024 * 1024
USER_AGENT = "swarmmed-template-prepare-data"


def _http_get(
    url: str, byte_range: tuple[int, int] | None = None, retries: int = 5
) -> bytes:
    """GET a URL (optionally bytes start..end inclusive) with retries."""
    headers = {"User-Agent": USER_AGENT}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=300) as response:
                data = response.read()
                if byte_range is not None:
                    if response.status != 206:
                        raise OSError(f"HTTP {response.status} for a range request")
                    if len(data) != byte_range[1] - byte_range[0] + 1:
                        raise OSError("short read")
                return data
        except Exception as exc:  # noqa: BLE001 - network errors are retried, then raised
            if attempt == retries - 1:
                raise
            print(
                f"  retry {attempt + 1} for {url} {byte_range or ''}: {exc}", flush=True
            )
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def _safe_destination(raw_dir: Path, relative: str) -> Path:
    """Return raw_dir/relative, refusing absolute paths and '..' components."""
    norm = posixpath.normpath(relative)
    if norm.startswith(("/", "../")) or norm == "..":
        raise ValueError(f"unsafe path in the archive: {relative!r}")
    destination = (raw_dir / norm).resolve()
    if not str(destination).startswith(str(raw_dir.resolve()) + os.sep):
        raise ValueError(f"unsafe path in the archive: {relative!r}")
    return destination


def _download_zip_members(raw_dir: Path, workers: int) -> bool:
    """Fetch only the needed members of the official zip with HTTP range requests.

    Returns False when the server does not accept range requests.
    """
    request = urllib.request.Request(
        ZIP_URL, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        url = response.geturl()
        size = int(response.headers["Content-Length"])
        accepts_ranges = response.headers.get("Accept-Ranges", "").lower() == "bytes"
    print(f"zip: {url} ({size:,} bytes), range requests: {accepts_ranges}", flush=True)
    if not accepts_ranges:
        return False

    sparse = raw_dir / ".ptbxl-1.0.3.zip.partial"
    with open(sparse, "wb") as fh:
        fh.truncate(size)
    fd = os.open(sparse, os.O_WRONLY)
    try:
        # The central directory sits at the end of the zip.
        tail_start = max(0, size - TAIL_BYTES)
        os.pwrite(fd, _http_get(url, (tail_start, size - 1)), tail_start)
        with zipfile.ZipFile(sparse) as archive:
            start_dir = archive.start_dir
        if start_dir < tail_start:
            os.pwrite(fd, _http_get(url, (start_dir, tail_start - 1)), start_dir)
        with zipfile.ZipFile(sparse) as archive:
            infos = sorted(archive.infolist(), key=lambda info: info.header_offset)
            start_dir = archive.start_dir

        # Members are stored under one top-level folder; select by the path below it.
        bounds = [info.header_offset for info in infos] + [start_dir]
        selected, spans = [], []
        for index, info in enumerate(infos):
            relative = (
                info.filename.split("/", 1)[1]
                if "/" in info.filename
                else info.filename
            )
            if not relative or relative.endswith("/"):
                continue
            if relative not in ROOT_FILES and not relative.startswith(RECORDS_PREFIX):
                continue
            selected.append((info, relative))
            begin, end = bounds[index], bounds[index + 1]
            if spans and spans[-1][1] == begin:
                spans[-1][1] = end
            else:
                spans.append([begin, end])
        chunks = [
            (start, min(end, start + CHUNK_BYTES) - 1)
            for begin, end in spans
            for start in range(begin, end, CHUNK_BYTES)
        ]
        total = sum(end - begin for begin, end in spans)
        print(
            f"zip: fetching {len(selected):,} members, {total / 1e6:.1f} MB", flush=True
        )
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            futures = {pool.submit(_http_get, url, chunk): chunk for chunk in chunks}
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                os.pwrite(fd, future.result(), futures[future][0])
                if done % 10 == 0 or done == len(chunks):
                    print(f"  {done}/{len(chunks)} chunks", flush=True)
    finally:
        os.close(fd)

    # zipfile checks each member's CRC-32 while extracting.
    with zipfile.ZipFile(sparse) as archive:
        for info, relative in selected:
            destination = _safe_destination(raw_dir, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(destination, "wb") as target:
                while block := source.read(1 << 20):
                    target.write(block)
    sparse.unlink()
    return True


def _download_files(raw_dir: Path, workers: int) -> None:
    """Fallback: fetch the root files and every 100 Hz record one file at a time."""
    for name in ROOT_FILES:
        (raw_dir / name).write_bytes(_http_get(FILES_URL + name))
    database = pd.read_csv(raw_dir / "ptbxl_database.csv", usecols=["filename_lr"])
    relatives = [
        f"{rel}{ext}" for rel in database["filename_lr"] for ext in (".hea", ".dat")
    ]

    def fetch(relative: str) -> None:
        destination = _safe_destination(raw_dir, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_http_get(FILES_URL + relative))

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for done, _ in enumerate(pool.map(fetch, relatives), 1):
            if done % 2000 == 0 or done == len(relatives):
                print(f"  {done}/{len(relatives)} files", flush=True)


def verify_sha256(raw_dir: Path, relatives: list[str]) -> None:
    """Compare files with the dataset's SHA256SUMS.txt; raise on any mismatch."""
    sums = {}
    for line in (raw_dir / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]
    bad = [rel for rel in relatives if rel not in sums]
    for relative in relatives:
        if relative in sums:
            digest = hashlib.sha256((raw_dir / relative).read_bytes()).hexdigest()
            if digest != sums[relative]:
                bad.append(relative)
    if bad:
        raise SystemExit(
            f"SHA-256 check failed or file not listed for {len(bad)} files, e.g. {bad[:5]}"
        )
    print(f"SHA-256: {len(relatives):,} files match SHA256SUMS.txt", flush=True)


def download(raw_dir: Path, workers: int = 8) -> None:
    """Download the CSV files, the licence, the checksums and records100/ from PhysioNet."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    if not _download_zip_members(raw_dir, workers):
        print("zip: range requests not available; downloading file by file", flush=True)
        _download_files(raw_dir, workers)
    database = pd.read_csv(raw_dir / "ptbxl_database.csv", usecols=["filename_lr"])
    relatives = [name for name in ROOT_FILES if name != "SHA256SUMS.txt"]
    relatives += [
        f"{rel}{ext}" for rel in database["filename_lr"] for ext in (".hea", ".dat")
    ]
    verify_sha256(raw_dir, relatives)
    print(f"download finished in {time.time() - started:.0f} s", flush=True)


# --------------------------------------------------------------------------------------------
# Metadata: labels, sites, splits
# --------------------------------------------------------------------------------------------


def load_metadata(raw_dir: Path) -> pd.DataFrame:
    """Return one row per record (index ecg_id, sorted) with site, split, labels and file name."""
    database = pd.read_csv(
        raw_dir / "ptbxl_database.csv", index_col="ecg_id"
    ).sort_index()
    statements = pd.read_csv(raw_dir / "scp_statements.csv", index_col=0)
    diagnostic_class = statements.loc[
        statements["diagnostic"] == 1, "diagnostic_class"
    ].to_dict()

    # Same aggregation as the dataset's example_physionet.py: likelihoods are ignored.
    codes = database["scp_codes"].apply(ast.literal_eval)
    present = codes.apply(
        lambda d: {diagnostic_class[k] for k in d if k in diagnostic_class}
    )
    y_super = np.array(
        [[int(c in s) for c in SUPERCLASSES] for s in present], dtype=np.int8
    )

    folds = database["strat_fold"].to_numpy()
    if not np.isin(folds, range(1, 11)).all():
        raise ValueError("strat_fold outside 1..10 in ptbxl_database.csv")
    split_of_fold = {fold: split for split, fs in FOLDS.items() for fold in fs}

    site = database["site"].to_numpy(dtype=float)  # NaN for 17 records without a site
    site_index = np.select([site == 0, site == 1, site == 2], [1, 2, 3], default=4)

    meta = pd.DataFrame(index=database.index)
    meta["filename_lr"] = database["filename_lr"]
    meta["site"] = [SITES[k - 1] for k in site_index]
    meta["split"] = [split_of_fold[f] for f in folds]
    meta["labelled"] = y_super.sum(axis=1) > 0
    meta["y"] = (y_super[:, 1:].sum(axis=1) > 0).astype(np.int8)
    for k, name in enumerate(SUPERCLASSES):
        meta[name] = y_super[:, k]

    # Patient metadata. Ages of 90 or more are stored as 300 in PTB-XL.
    age = database["age"].where(database["age"] != 300, 90.0).to_numpy(dtype=float)
    height = database["height"].to_numpy(dtype=float)
    weight = database["weight"].to_numpy(dtype=float)
    adult = age >= 18
    height_ok = np.where(
        adult, (height >= 120) & (height <= 220), (height >= 50) & (height <= 200)
    )
    weight_ok = np.where(
        adult, (weight >= 30) & (weight <= 250), (weight >= 3) & (weight <= 150)
    )
    meta.attrs["implausible"] = {
        "height": int((np.isfinite(height) & ~height_ok).sum()),
        "weight": int((np.isfinite(weight) & ~weight_ok).sum()),
    }
    height = np.where(
        height_ok, height, np.nan
    )  # comparisons with NaN are False, so NaN stays NaN
    weight = np.where(weight_ok, weight, np.nan)
    sex = database["sex"].to_numpy().astype(np.int8)  # 0 = male, 1 = female
    tab = np.stack(
        [
            (age - 60.0) / 20.0,
            sex.astype(float),
            np.where(np.isfinite(height), (height - 170.0) / 10.0, 0.0),
            (~np.isfinite(height)).astype(float),
            np.where(np.isfinite(weight), (weight - 75.0) / 15.0, 0.0),
            (~np.isfinite(weight)).astype(float),
        ],
        axis=1,
    ).astype(np.float32)
    meta["age"] = age.astype(np.float32)
    meta["sex"] = sex
    meta["height"] = height.astype(np.float32)
    meta["weight"] = weight.astype(np.float32)
    for k, name in enumerate(TAB_FEATURES):
        meta[name] = tab[:, k]
    return meta


# --------------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------------

GAIN_RE = re.compile(
    r"^(?P<gain>[0-9.eE+-]+)(\((?P<baseline>-?\d+)\))?(/(?P<units>\S+))?$"
)


def read_record(raw_dir: Path, filename_lr: str) -> np.ndarray:
    """Read one 100 Hz record as int16 microvolts, shape (12, 1000), checking its WFDB header."""
    base = raw_dir / filename_lr
    name = base.name
    lines = [
        line.split()
        for line in base.with_suffix(".hea").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if lines[0][:4] != [name, "12", str(FS_HZ), str(N_SAMPLES)] or len(lines) != 13:
        raise ValueError(f"{filename_lr}.hea: unexpected record line {lines[0]}")
    signal = np.fromfile(base.with_suffix(".dat"), dtype="<i2")
    if signal.size != 12 * N_SAMPLES:
        raise ValueError(
            f"{filename_lr}.dat: {signal.size} samples, expected {12 * N_SAMPLES}"
        )
    signal = signal.reshape(N_SAMPLES, 12).T  # stored interleaved by sample
    for lead, fields in enumerate(lines[1:]):
        gain = GAIN_RE.match(fields[2])
        baseline = (
            int(gain["baseline"]) if gain and gain["baseline"] else int(fields[4])
        )
        if (
            fields[0] != f"{name}.dat"
            or fields[1] != "16"
            or gain is None
            or float(gain["gain"]) != 1000.0
            or baseline != 0
            or (gain["units"] or "mV") != "mV"
            or fields[8] != LEADS[lead]
        ):
            raise ValueError(f"{filename_lr}.hea: unexpected signal line {fields}")
        # Initial value and 16-bit checksum of the samples, as written by WFDB.
        if int(fields[5]) != int(signal[lead, 0]) or (
            int(signal[lead].astype(np.int64).sum()) & 0xFFFF
        ) != (int(fields[6]) & 0xFFFF):
            raise ValueError(
                f"{filename_lr}.dat: lead {LEADS[lead]} does not match its header"
            )
    return np.ascontiguousarray(signal)


# --------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------


def array_fingerprint(arrays: dict, names=ARRAYS) -> str:
    """SHA-256 over the named arrays (name, dtype, shape and bytes, in that order)."""
    digest = hashlib.sha256()
    for name in names:
        array = np.ascontiguousarray(arrays[name])
        digest.update(f"{name}|{array.dtype.str}|{array.shape}|".encode())
        digest.update(array.tobytes())
    return digest.hexdigest()


def split_counts(y: np.ndarray, y_super: np.ndarray) -> dict:
    counts = {"n": int(len(y)), "n_abnormal": int(y.sum())}
    counts.update(
        {name: int(y_super[:, k].sum()) for k, name in enumerate(SUPERCLASSES)}
    )
    return counts


def save_npz(path: Path, arrays: dict) -> None:
    """Write a compressed .npz atomically (temporary file, then rename)."""
    partial = path.with_name(path.name + ".partial")
    with open(partial, "wb") as fh:
        np.savez_compressed(fh, **arrays)
    os.replace(partial, path)


def build_split(raw_dir: Path, rows: pd.DataFrame) -> dict:
    """Read the signals of the given rows and return the arrays of one .npz file."""
    X = np.empty((len(rows), 12, N_SAMPLES), dtype=np.int16)
    for i, filename_lr in enumerate(rows["filename_lr"]):
        X[i] = read_record(raw_dir, filename_lr)
    return {
        "X": X,
        "y": rows["y"].to_numpy(dtype=np.int8),
        "y_super": rows[list(SUPERCLASSES)].to_numpy(dtype=np.int8),
        "ecg_id": rows.index.to_numpy(dtype=np.int32),
        "tab": rows[list(TAB_FEATURES)].to_numpy(dtype=np.float32),
        "age": rows["age"].to_numpy(dtype=np.float32),
        "sex": rows["sex"].to_numpy(dtype=np.int8),
        "height": rows["height"].to_numpy(dtype=np.float32),
        "weight": rows["weight"].to_numpy(dtype=np.float32),
    }


def metadata_summary(arrays: dict) -> dict:
    """Missingness, sex and age of one split (rounded to 3 decimals)."""
    return {
        "height_missing": round(float(np.isnan(arrays["height"]).mean()), 3),
        "weight_missing": round(float(np.isnan(arrays["weight"]).mean()), 3),
        "female": round(float(arrays["sex"].mean()), 3),
        "age_median": float(np.median(arrays["age"])),
    }


def write_site(
    raw_dir: Path, meta: pd.DataFrame, site: str, folder: Path, args
) -> None:
    """Write train/val/test .npz and site_info.json of one site into folder."""
    folder.mkdir(parents=True, exist_ok=True)
    rows = meta[(meta["site"] == site) & meta["labelled"]]
    counts, fingerprints, metadata_fingerprints = {}, {}, {}
    for split in SPLITS:
        started = time.time()
        arrays = build_split(raw_dir, rows[rows["split"] == split])
        save_npz(folder / f"{split}.npz", arrays)
        counts[split] = {
            **split_counts(arrays["y"], arrays["y_super"]),
            **metadata_summary(arrays),
        }
        fingerprints[split] = array_fingerprint(arrays)
        metadata_fingerprints[split] = array_fingerprint(arrays, METADATA_ARRAYS)
        print(
            f"  wrote {folder.name}/{split}.npz: {counts[split]['n']} records "
            f"({time.time() - started:.1f} s)",
            flush=True,
        )
        del arrays
    info = {
        **DATASET,
        "template": TEMPLATE,
        "site": site,
        "site_index": SITES.index(site) + 1,
        "recording_sites": RECORDING_SITES[site],
        "recording_sites_note": (
            "PTB-XL 'site' field (pseudonymised recording site); 'other' = every site except "
            "0, 1 and 2, plus records without a site"
        ),
        "task": "binary: normal (NORM is the only diagnostic superclass) vs abnormal (any of MI, STTC, CD, HYP)",
        "label_map": LABEL_MAP,
        "superclasses": list(SUPERCLASSES),
        "leads": list(LEADS),
        "fs": FS_HZ,
        "n_samples": N_SAMPLES,
        "units": "uV, int16",
        "folds": FOLDS,
        "tab_features": list(TAB_FEATURES),
        "tab_definitions": TAB_DEFINITIONS,
        "plausibility_rule": PLAUSIBILITY_RULE,
        "arrays": {
            "X": "int16 (N, 12, 1000), microvolts",
            "y": "int8 (N,), 0 = normal, 1 = abnormal",
            "y_super": "int8 (N, 5), multi-hot over superclasses",
            "ecg_id": "int32 (N,), PTB-XL record number",
            "tab": "float32 (N, 6), tab_features",
            "age": "float32 (N,), years (300 mapped to 90)",
            "sex": "int8 (N,), 0 = male, 1 = female",
            "height": "float32 (N,), cm, NaN when missing or implausible",
            "weight": "float32 (N,), kg, NaN when missing or implausible",
        },
        "counts": counts,
        "excluded_without_superclass": int(
            ((meta["site"] == site) & ~meta["labelled"]).sum()
        ),
        "fingerprints": {
            "sha256_X_y_ysuper_ecgid": fingerprints,
            "sha256_tab_age_sex_height_weight": metadata_fingerprints,
        },
        "preprocessing": PREPROCESSING,
        "normalisation": NORMALISATION,
        "created": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prepared_with": {
            "script": f"{TEMPLATE}/prepare_data.py",
            "layout": args.layout,
            "site_index": args.site_index,
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
    }
    (folder / "site_info.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------------


def verify(targets: list[tuple[str, Path]], meta: pd.DataFrame) -> bool:
    """Read the written files back, print the counts and compare them with the reference."""
    ok = len(meta) == EXPECTED_RECORDS
    print(f"\nrecords in ptbxl_database.csv: {len(meta)} (expected {EXPECTED_RECORDS})")
    implausible = meta.attrs["implausible"]
    ok &= implausible == EXPECTED_IMPLAUSIBLE
    print(
        f"set missing by the plausibility rule: {implausible['height']} heights, "
        f"{implausible['weight']} weights (expected {EXPECTED_IMPLAUSIBLE['height']} and "
        f"{EXPECTED_IMPLAUSIBLE['weight']})"
    )
    header = f"{'site':<12} {'split':<5} {'n':>5} {'abn':>5} {'prev':>5} " + " ".join(
        f"{c:>5}" for c in SUPERCLASSES
    )
    print(header + f" {'h_mis':>5} {'w_mis':>5} {'fem':>5} {'age':>4} {'MB':>6}  check")
    total_mb = 0.0
    for site, folder in targets:
        excluded = int(((meta["site"] == site) & ~meta["labelled"]).sum())
        site_ok = excluded == EXPECTED_EXCLUDED[site]
        for split in SPLITS:
            path = folder / f"{split}.npz"
            with np.load(path, allow_pickle=False) as npz:
                arrays = {name: npz[name] for name in ARRAYS + METADATA_ARRAYS}
            counts = split_counts(arrays["y"], arrays["y_super"])
            row = (
                counts["n"],
                counts["n_abnormal"],
                *(counts[c] for c in SUPERCLASSES),
            )
            summary = metadata_summary(arrays)
            summary_row = tuple(
                summary[k]
                for k in ("height_missing", "weight_missing", "female", "age_median")
            )
            checks = [row == EXPECTED_COUNTS[site][split]]
            checks.append(summary_row == EXPECTED_METADATA[site][split])
            checks.append(
                arrays["X"].dtype == np.int16
                and arrays["X"].shape[1:] == (12, N_SAMPLES)
            )
            checks.append(
                arrays["tab"].dtype == np.float32 and arrays["tab"].shape == (row[0], 6)
            )
            checks.append(bool(np.isfinite(arrays["tab"]).all()))
            checks.append(bool(np.all(np.diff(arrays["ecg_id"]) > 0)))
            for expected, names in (
                (EXPECTED_FINGERPRINTS, ARRAYS),
                (EXPECTED_METADATA_FINGERPRINTS, METADATA_ARRAYS),
            ):
                expected_fp = expected.get(site, {}).get(split)
                if expected_fp is not None:
                    checks.append(array_fingerprint(arrays, names) == expected_fp)
            mb = path.stat().st_size / 1e6
            total_mb += mb
            status = "ok" if all(checks) else "MISMATCH"
            site_ok &= all(checks)
            prevalence = counts["n_abnormal"] / max(counts["n"], 1)
            print(
                f"{site:<12} {split:<5} {row[0]:>5} {row[1]:>5} {prevalence:>5.3f} "
                + " ".join(f"{v:>5}" for v in row[2:])
                + f" {summary_row[0]:>5.3f} {summary_row[1]:>5.3f} {summary_row[2]:>5.3f} {summary_row[3]:>4.0f}"
                + f" {mb:>6.1f}  {status}"
            )
        print(
            f"{site:<12} excluded without a diagnostic superclass: {excluded} "
            f"(expected {EXPECTED_EXCLUDED[site]})"
        )
        ok &= site_ok
    print(
        f"total size on disk: {total_mb:.1f} MB; checked: counts, metadata, dtype and shape, order, fingerprints"
    )
    return ok


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--raw-dir",
        required=True,
        type=Path,
        help="PTB-XL 1.0.3 folder (ptbxl_database.csv, scp_statements.csv, records100/)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="download the needed files from PhysioNet into --raw-dir first",
    )
    parser.add_argument("--out", required=True, type=Path, help="output folder")
    parser.add_argument(
        "--layout",
        choices=("local-test", "single"),
        default="local-test",
        help="local-test: OUT/fl-client-1..4/; single: one site's files in OUT/",
    )
    parser.add_argument(
        "--site-index",
        type=int,
        choices=(1, 2, 3, 4),
        help="site to write with --layout single",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="parallel HTTP requests for --download (default 8)",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="do not compare the output with the reference counts",
    )
    args = parser.parse_args(argv)
    if args.layout == "single" and args.site_index is None:
        parser.error("--layout single needs --site-index (1-4)")
    if args.layout == "local-test" and args.site_index is not None:
        parser.error("--site-index is only used with --layout single")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    raw_dir = args.raw_dir.resolve()
    started = time.time()
    if args.download:
        download(raw_dir, workers=args.workers)
    for required in ("ptbxl_database.csv", "scp_statements.csv", "records100"):
        if not (raw_dir / required).exists():
            raise SystemExit(
                f"{raw_dir / required} not found; use --download or point --raw-dir at PTB-XL 1.0.3"
            )
    if not args.download and (raw_dir / "SHA256SUMS.txt").exists():
        verify_sha256(raw_dir, ["ptbxl_database.csv", "scp_statements.csv"])

    meta = load_metadata(raw_dir)
    if args.layout == "local-test":
        targets = [(site, args.out / site) for site in SITES]
    else:
        targets = [(SITES[args.site_index - 1], args.out)]
    for site, folder in targets:
        print(f"{site} -> {folder}", flush=True)
        write_site(raw_dir, meta, site, folder, args)
    print(f"written in {time.time() - started:.1f} s")

    if args.skip_verify:
        print("verification skipped (--skip-verify)")
        return 0
    if verify(targets, meta):
        print("verification: counts and fingerprints match the reference preparation")
        return 0
    print("verification FAILED: see the rows marked MISMATCH")
    return 1


if __name__ == "__main__":
    sys.exit(main())
