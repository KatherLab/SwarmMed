#!/usr/bin/env python3
"""Prepare the UCI Heart Disease data for the SwarmMed template ``tabular_heart_disease``.

The UCI Heart Disease dataset holds one "processed" file per hospital (14 attributes per
patient). This script converts the files into the data layout of SwarmMed templates:

    --layout local-test   OUT/fl-client-1/ ... OUT/fl-client-4/: one folder per hospital, for a
                          local-test network on one host (each client reads its own folder)
    --layout single       OUT/ holds one hospital's files (--site-index K, 1-based), for a real
                          deployment in which each site registers only its own data

Every site folder holds ``train.npz``, ``val.npz``, ``test.npz`` and ``site_info.json``.
For each hospital the script

 1. reads its processed file and checks the file's SHA-256 against the value pinned below,
 2. labels each patient 1 if ``num`` > 0 (angiographic coronary artery disease), else 0,
 3. splits the patients 60/20/20 into train/val/test, stratified by label, with a fixed seed;
    exact duplicate rows are kept in the same split,
 4. imputes missing values and standardises the continuous features with statistics of the
    hospital's own training split (nothing is pooled across hospitals),
 5. writes the arrays and a description of the site (``site_info.json``),
 6. checks the counts and a fingerprint of the test cases against the expected values for
    seed 42, and stops with an error if they differ.

Usage:
    python prepare_data.py --raw-dir RAW --out OUT                    # all four sites, local test
    python prepare_data.py --raw-dir RAW --out OUT --layout single --site-index 2
    python prepare_data.py --raw-dir RAW --out OUT --download         # fetch the zip from UCI first
    python prepare_data.py --raw-dir RAW --out OUT --num-sites 3      # leave out Switzerland

RAW holds the four ``processed.*.data`` files or the UCI zip ``heart+disease.zip``. Only numpy,
pandas and the Python standard library are needed. Keep OUT outside this template folder and
outside any repository: every file in the template folder is uploaded as training code.
"""

import argparse
import hashlib
import io
import json
import math
import sys
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------- dataset facts

DATASET = "UCI Heart Disease, processed files of four hospitals"
DATASET_VERSION = (
    "No version number is given by UCI; DOI 10.24432/C52P4X; "
    "the processed files are pinned by SHA-256"
)
DATASET_PAGE = "https://archive.ics.uci.edu/dataset/45/heart+disease"
DATASET_DOI = "https://doi.org/10.24432/C52P4X"
ZIP_URL = "https://archive.ics.uci.edu/static/public/45/heart+disease.zip"
ZIP_NAME = "heart+disease.zip"
# SHA-256 of the zip as downloaded on 11 Nov 2026. UCI publishes no checksums; if UCI
# repackages the zip, this value changes while the processed files inside may not, so only
# the per-file values in SITES are enforced.
ZIP_SHA256 = "b17cd273da9ce1caa4710fce80227ea454d4dbf9fcbc8e6a9121672751563adc"
NAMES_FILE = "heart-disease.names"  # dataset description, extracted with --download
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
LICENCE = "CC BY 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"
CITATION = (
    "Janosi A, Steinbrunn W, Pfisterer M, Detrano R (1989). Heart Disease [Dataset]. "
    "UCI Machine Learning Repository. https://doi.org/10.24432/C52P4X"
)
# The donors ask that publications name the principal investigators (heart-disease.names).
PRINCIPAL_INVESTIGATORS = [
    "Andras Janosi, M.D. (Hungarian Institute of Cardiology, Budapest)",
    "William Steinbrunn, M.D. (University Hospital, Zurich, Switzerland)",
    "Matthias Pfisterer, M.D. (University Hospital, Basel, Switzerland)",
    "Robert Detrano, M.D., Ph.D. (V.A. Medical Center, Long Beach, and Cleveland Clinic Foundation)",
]
TASK = "Binary classification: angiographic coronary artery disease (num > 0)"
LABEL_MAP = {
    "0": "no disease: num = 0 (< 50% diameter narrowing in every major vessel)",
    "1": "disease: num 1-4 (> 50% diameter narrowing in at least one major vessel)",
}

# Columns of the processed files, in file order (heart-disease.names, attributes 3, 4, 9, 10,
# 12, 16, 19, 32, 38, 40, 41, 44, 51, 58 of the full 76-attribute records).
RAW_COLUMNS = [
    "age",
    "sex",
    "cp",
    "trestbps",
    "chol",
    "fbs",
    "restecg",
    "thalach",
    "exang",
    "oldpeak",
    "slope",
    "ca",
    "thal",
    "num",
]

# The four hospitals in fixed order. ``site_index`` (0-based position here) seeds the split and
# belongs to the hospital, not to the fl-client number. Records and positives are the counts
# in the processed files (checked before anything is written).
SITES = [
    {
        "key": "cleveland",
        "file": "processed.cleveland.data",
        "description": "Cleveland Clinic Foundation, Cleveland, Ohio, USA",
        "sha256": "a74b7efa387bc9d108d7d0115d831fe9b414b29ae7124f331b622b4efa0427c8",
        "records": 303,
        "positives": 139,
    },
    {
        "key": "hungarian",
        "file": "processed.hungarian.data",
        "description": "Hungarian Institute of Cardiology, Budapest, Hungary",
        "sha256": "d1ad108f785768cd3d7e82dc522e6f5a61eea93cccfb3a46ee8076f73fc3d796",
        "records": 294,
        "positives": 106,
    },
    {
        "key": "va_long_beach",
        "file": "processed.va.data",
        "description": "V.A. Medical Center, Long Beach, California, USA",
        "sha256": "e7c93d8d0d2acdadfa4c5e8de768e2191e7f618b952e29623f1f0d5949ff6b8f",
        "records": 200,
        "positives": 149,
    },
    {
        "key": "switzerland",
        "file": "processed.switzerland.data",
        "description": (
            "University Hospital, Zurich, Switzerland (the dataset description also "
            "credits University Hospital, Basel)"
        ),
        "sha256": "834a405ccf5b66ab4056bb77794adc8df0b7125186454c0a1d002d33c6c3b314",
        "records": 123,
        "positives": 115,
    },
]

# Model inputs (d = 15), in this order at every site. slope, ca and thal are not used: they are
# missing for most patients outside Cleveland, so they would mainly tell the model the site.
# (name, kind, label, unit); kind is continuous, binary or onehot.
FEATURES = [
    ("age", "continuous", "Age", "years"),
    ("trestbps", "continuous", "Resting blood pressure on admission", "mm Hg"),
    ("chol", "continuous", "Serum cholesterol", "mg/dl"),
    ("thalach", "continuous", "Maximum heart rate achieved", "beats/min"),
    ("oldpeak", "continuous", "ST depression induced by exercise relative to rest", ""),
    ("sex", "binary", "Male sex", "1 = male, 0 = female"),
    ("fbs", "binary", "Fasting blood sugar > 120 mg/dl", "1 = true, 0 = false"),
    ("exang", "binary", "Exercise-induced angina", "1 = yes, 0 = no"),
    ("cp_1", "onehot", "Chest pain type: typical angina", ""),
    ("cp_2", "onehot", "Chest pain type: atypical angina", ""),
    ("cp_3", "onehot", "Chest pain type: non-anginal pain", ""),
    ("cp_4", "onehot", "Chest pain type: asymptomatic", ""),
    ("restecg_0", "onehot", "Resting ECG: normal", ""),
    ("restecg_1", "onehot", "Resting ECG: ST-T wave abnormality", ""),
    (
        "restecg_2",
        "onehot",
        "Resting ECG: probable or definite left ventricular hypertrophy",
        "",
    ),
]
CATEGORICAL_LEVELS = {"cp": [1, 2, 3, 4], "restecg": [0, 1, 2]}
ZERO_MEANS_MISSING = ("trestbps", "chol")  # 0 mm Hg and 0 mg/dl are missing-value codes

SPLITS = ("train", "val", "test")
FRAC_VAL = 0.2
FRAC_TEST = 0.2
Z_CLIP = 5.0
DEFAULT_SEED = 42

# Expected result for seed 42: (n, positives) per split and the first 16 hex digits of
# sha256("\n".join(sorted(test case_ids))). The values depend only on the hospital and the
# seed, so they also hold for --num-sites 3 and for --layout single.
EXPECTED_SEED = 42
EXPECTED = {
    "cleveland": (
        {"train": (181, 83), "val": (61, 28), "test": (61, 28)},
        "5af821aaf48625d9",
    ),
    "hungarian": (
        {"train": (178, 64), "val": (58, 21), "test": (58, 21)},
        "eb9800552852cd01",
    ),
    "va_long_beach": (
        {"train": (120, 89), "val": (40, 30), "test": (40, 30)},
        "cd9d803a76144419",
    ),
    "switzerland": (
        {"train": (73, 69), "val": (25, 23), "test": (25, 23)},
        "a047caba1a7d739f",
    ),
}


# ----------------------------------------------------------------------------- raw files


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_raw(raw_dir: Path) -> None:
    """Download the UCI zip into raw_dir and extract the four processed files and the names file.

    Only these members are extracted, by base name, so a crafted archive cannot write outside
    raw_dir. Each processed file must match its pinned SHA-256.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {ZIP_URL}")
    request = urllib.request.Request(
        ZIP_URL, headers={"User-Agent": "swarmmed-prepare-data"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        sys.exit(f"Download larger than {MAX_DOWNLOAD_BYTES} bytes; stopped.")
    digest = sha256_hex(data)
    if digest != ZIP_SHA256:
        print(
            f"Note: the zip's SHA-256 is {digest}, not the value recorded on 11 Nov 2026 "
            f"({ZIP_SHA256}). UCI may have repackaged it; the processed files are checked "
            "one by one below."
        )
    tmp = raw_dir / (ZIP_NAME + ".part")
    tmp.write_bytes(data)
    tmp.replace(raw_dir / ZIP_NAME)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = {
            Path(info.filename).name: info.filename for info in archive.infolist()
        }
        for name in [site["file"] for site in SITES] + [NAMES_FILE]:
            if name not in members:
                sys.exit(f"{name} is missing from the downloaded zip.")
            content = archive.read(members[name])
            expected = next((s["sha256"] for s in SITES if s["file"] == name), None)
            if expected is not None and sha256_hex(content) != expected:
                sys.exit(f"SHA-256 mismatch for {name} in the downloaded zip; stopped.")
            (raw_dir / name).write_bytes(content)
    print(
        f"Saved {ZIP_NAME} ({len(data):,} bytes) and extracted the processed files into {raw_dir}"
    )


def read_raw_file(raw_dir: Path, site: dict) -> bytes:
    """Return the bytes of a site's processed file (extracted file, else read from the zip).

    Stops with an error if the file is missing or its SHA-256 differs from the pinned value.
    """
    path = raw_dir / site["file"]
    if path.is_file():
        data = path.read_bytes()
    elif (raw_dir / ZIP_NAME).is_file():
        with zipfile.ZipFile(raw_dir / ZIP_NAME) as archive:
            members = {Path(i.filename).name: i.filename for i in archive.infolist()}
            if site["file"] not in members:
                sys.exit(f"{site['file']} is neither in {raw_dir} nor in {ZIP_NAME}.")
            data = archive.read(members[site["file"]])
    else:
        sys.exit(
            f"{site['file']} not found in {raw_dir}. Run with --download, or download "
            f"{ZIP_URL} (dataset page {DATASET_PAGE}) and extract it into {raw_dir}."
        )
    digest = sha256_hex(data)
    if digest != site["sha256"]:
        sys.exit(
            f"SHA-256 mismatch for {site['file']}: {digest}, expected {site['sha256']}. "
            "The file differs from the one this template was built and checked with; stopped."
        )
    return data


# ----------------------------------------------------------------------------- per-site steps


def load_site(data: bytes, site: dict) -> tuple[pd.DataFrame, np.ndarray]:
    """Parse one processed file; return the table (with case_id, group, y) and X_raw.

    X_raw holds the 15 model inputs before imputation and scaling, NaN where missing; the
    one-hot columns of a patient whose cp or restecg is missing are all NaN.
    """
    df = pd.read_csv(io.BytesIO(data), header=None, names=RAW_COLUMNS, na_values=["?"])
    if df.shape[1] != len(RAW_COLUMNS) or len(df) != site["records"]:
        sys.exit(
            f"{site['file']}: {df.shape}, expected {site['records']} rows x 14 columns."
        )

    # Pseudonymous case id: hospital key and 0-based line number in the processed file.
    df["case_id"] = [f"{site['key']}-{row:03d}" for row in range(len(df))]
    # Exact duplicate rows (all 14 values as read) form one group, keyed by the case id of the
    # group's first row, so that the split keeps them together.
    signature = df[RAW_COLUMNS].astype(str).agg("|".join, axis=1)
    first_case = {}
    for sig, case_id in zip(signature, df["case_id"], strict=True):
        first_case.setdefault(sig, case_id)
    df["group"] = signature.map(first_case)

    for column in ZERO_MEANS_MISSING:
        df.loc[df[column] == 0, column] = np.nan
    df["y"] = (df["num"] > 0).astype(np.int64)
    if int(df["y"].sum()) != site["positives"]:
        sys.exit(
            f"{site['file']}: {int(df['y'].sum())} positives, expected {site['positives']}."
        )

    columns = []
    for name, kind, _, _ in FEATURES:
        if kind == "onehot":
            variable, level = name.rsplit("_", 1)
            values = df[variable]
            unknown = sorted(set(values.dropna()) - set(CATEGORICAL_LEVELS[variable]))
            if unknown:
                sys.exit(f"{site['file']}: unexpected {variable} values {unknown}.")
            columns.append(
                np.where(values.isna(), np.nan, (values == float(level)).astype(float))
            )
        else:
            values = df[name].to_numpy(dtype=float)
            if (
                kind == "binary"
                and not np.isin(values[~np.isnan(values)], (0.0, 1.0)).all()
            ):
                sys.exit(f"{site['file']}: {name} has values other than 0 and 1.")
            columns.append(values)
    return df, np.column_stack(columns)


def split_site(
    groups: np.ndarray, y: np.ndarray, seed: int, site_index: int
) -> np.ndarray:
    """Patient-level split stratified by label; return 'train'/'val'/'test' for every row.

    Groups are sorted, then shuffled per class (class 0 first, then class 1) with one generator
    seeded by (seed, site_index). Of each class, round(20%) goes to test, round(20%) to val and
    the rest to train. math.floor(x + 0.5) rounds halves up (Python's round() would round them
    to even).
    """
    group_label = pd.Series(y).groupby(pd.Series(groups)).max()  # sorted by group key
    rng = np.random.default_rng([seed, site_index])
    assignment = {}
    for label in (0, 1):
        ids = np.array(
            sorted(group_label.index[group_label.to_numpy() == label]), dtype=object
        )
        ids = ids[rng.permutation(len(ids))]
        n_test = math.floor(FRAC_TEST * len(ids) + 0.5)
        n_val = math.floor(FRAC_VAL * len(ids) + 0.5)
        for position, group in enumerate(ids):
            if position < n_test:
                assignment[group] = "test"
            elif position < n_test + n_val:
                assignment[group] = "val"
            else:
                assignment[group] = "train"
    return np.array([assignment[g] for g in groups])


def preprocess(x_raw: np.ndarray, split: np.ndarray) -> tuple[np.ndarray, list[dict]]:
    """Impute and scale with this site's training rows only; return X (float32) and feature info.

    continuous: missing -> median of the observed training values; then z-score with the mean
                and population standard deviation of the filled training values, clipped to
                [-5, 5] (a column with no observed training value becomes all 0).
    binary:     missing -> 1 if more than half of the observed training values are 1, else 0.
    one-hot:    a missing category -> the most frequent training category (ties: lowest level).
    """
    train = split == "train"
    x = x_raw.astype(float).copy()
    index = {name: j for j, (name, _, _, _) in enumerate(FEATURES)}
    info = []
    for j, (name, kind, label, unit) in enumerate(FEATURES):
        missing = np.isnan(x_raw[:, j])
        info.append(
            {
                "name": name,
                "kind": kind,
                "label": label,
                "unit": unit,
                "impute": None,
                "mean": None,
                "std": None,
                "missing_train": int((missing & train).sum()),
                "missing_all": int(missing.sum()),
            }
        )

    # Categorical variables first: fill a missing category, then the one-hot columns are complete.
    for variable, levels in CATEGORICAL_LEVELS.items():
        cols = [index[f"{variable}_{level}"] for level in levels]
        block = x[:, cols]
        missing = np.isnan(block).any(axis=1)
        counts = block[train & ~missing].sum(axis=0)
        mode = int(np.argmax(counts))  # first maximum = lowest level
        fill = np.zeros(len(levels))
        fill[mode] = 1.0
        block[missing] = fill
        x[:, cols] = block
        for k, col in enumerate(cols):
            info[col]["impute"] = float(fill[k])
            info[col]["variable"] = variable
            info[col]["level"] = levels[k]

    for j, (_name, kind, _, _) in enumerate(FEATURES):
        if kind == "onehot":
            continue
        column = x[:, j]
        missing = np.isnan(column)
        observed = column[train & ~missing]
        if kind == "continuous":
            if len(observed) == 0:
                # Never observed in this site's training split (Switzerland's chol).
                x[:, j] = 0.0
                continue
            median = float(np.median(observed))
            filled = np.where(missing, median, column)
            mean, std = float(filled[train].mean()), float(filled[train].std())
            if std == 0:
                x[:, j] = 0.0
            else:
                x[:, j] = np.clip((filled - mean) / std, -Z_CLIP, Z_CLIP)
            info[j].update(impute=median, mean=mean, std=std)
        else:  # binary
            fill = (
                0.0 if len(observed) == 0 else (1.0 if observed.mean() > 0.5 else 0.0)
            )
            x[:, j] = np.where(missing, fill, column)
            info[j]["impute"] = fill
    return x.astype(np.float32), info


def fingerprint(case_ids) -> str:
    """First 16 hex digits of the SHA-256 of the sorted case ids, one per line."""
    return sha256_hex("\n".join(sorted(str(c) for c in case_ids)).encode())[:16]


def prepare_site(raw_dir: Path, site_index: int, seed: int) -> dict:
    """Run steps 1-4 for one hospital; return its arrays per split and its site_info fields."""
    site = SITES[site_index]
    data = read_raw_file(raw_dir, site)
    df, x_raw = load_site(data, site)
    y = df["y"].to_numpy(dtype=np.int64)
    split = split_site(df["group"].to_numpy(), y, seed, site_index)
    x, features = preprocess(x_raw, split)
    names = np.array([name for name, _, _, _ in FEATURES], dtype=str)
    case_ids = df["case_id"].to_numpy(dtype=str)
    arrays, counts = {}, {}
    for part in SPLITS:
        rows = split == part
        arrays[part] = {
            "X": x[rows],
            "X_raw": x_raw[rows].astype(np.float32),
            "y": y[rows],
            "case_id": case_ids[rows],
            "feature_names": names,
        }
        counts[part] = {"n": int(rows.sum()), "positives": int(y[rows].sum())}
    info = {
        "dataset": DATASET,
        "dataset_version": DATASET_VERSION,
        "source_url": DATASET_PAGE,
        "download_url": ZIP_URL,
        "doi": DATASET_DOI,
        "licence": LICENCE,
        "licence_url": LICENCE_URL,
        "citation": CITATION,
        "principal_investigators": PRINCIPAL_INVESTIGATORS,
        "site_name": f"fl-client-{site_index + 1}",
        "source_site": site["key"],
        "source_description": site["description"],
        "source_file": site["file"],
        "task": TASK,
        "label_map": LABEL_MAP,
        "seed": seed,
        "site_index": site_index,
        "split": {
            "train": round(1 - FRAC_VAL - FRAC_TEST, 2),
            "val": FRAC_VAL,
            "test": FRAC_TEST,
            "unit": "patient; exact duplicate rows stay together",
            "stratified_by": "label",
        },
        "counts": counts,
        "test_fingerprint": fingerprint(arrays["test"]["case_id"]),
        "preprocessing": {
            "label": "y = 1 if num > 0 else 0",
            "missing_codes": "'?' and 0 for trestbps and chol are treated as missing",
            "not_used": ["slope", "ca", "thal"],
            "statistics": "from this site's training split only",
            "continuous": "median imputation, z-score (population std), clipped to [-5, 5]",
            "binary": "imputed with the training majority value",
            "categorical": "imputed with the most frequent training category, then one-hot",
            "missing_indicators": False,
        },
        "features": features,
        "raw_files": {site["file"]: {"bytes": len(data), "sha256": sha256_hex(data)}},
        "case_id": "'<source_site>-<0-based line number in the processed file>'",
        "arrays": {
            "X": "float32 [n, 15] model input",
            "X_raw": "float32 [n, 15] the same columns before imputation and scaling (NaN = missing)",
            "y": "int64 [n] label",
            "case_id": "str [n] pseudonymous case id",
            "feature_names": "str [15]",
        },
        "generator": "examples/tabular_heart_disease/prepare_data.py (SwarmMed)",
    }
    return {"arrays": arrays, "info": info}


def write_site(folder: Path, prepared: dict) -> int:
    """Write train/val/test.npz and site_info.json into folder; return the bytes written."""
    folder.mkdir(parents=True, exist_ok=True)
    total = 0
    for part in SPLITS:
        path = folder / f"{part}.npz"
        np.savez_compressed(path, **prepared["arrays"][part])
        total += path.stat().st_size
    path = folder / "site_info.json"
    path.write_text(json.dumps(prepared["info"], indent=2) + "\n", encoding="utf-8")
    return total + path.stat().st_size


def check_expected(results: dict, seed: int) -> list[str]:
    """Compare counts and test fingerprints with EXPECTED; return a list of differences."""
    if seed != EXPECTED_SEED:
        return []
    problems = []
    for name, prepared in results.items():
        info = prepared["info"]
        counts, fp = EXPECTED[info["source_site"]]
        for part in SPLITS:
            got = (info["counts"][part]["n"], info["counts"][part]["positives"])
            if got != counts[part]:
                problems.append(
                    f"{name} {part}: [n, positives] {list(got)}, expected {list(counts[part])}"
                )
        if info["test_fingerprint"] != fp:
            problems.append(
                f"{name} test fingerprint {info['test_fingerprint']}, expected {fp}"
            )
    return problems


# ----------------------------------------------------------------------------- command line


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare UCI Heart Disease (four hospitals) for the SwarmMed tabular template."
    )
    parser.add_argument(
        "--raw-dir",
        required=True,
        type=Path,
        help="folder with the processed.*.data files or heart+disease.zip",
    )
    parser.add_argument(
        "--out",
        "--out-dir",
        dest="out",
        required=True,
        type=Path,
        help="output folder (outside this template folder and any repository)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help=f"download {ZIP_NAME} from UCI into --raw-dir first",
    )
    parser.add_argument(
        "--layout",
        choices=["local-test", "single"],
        default="local-test",
        help="local-test: OUT/fl-client-k/ for every site; single: one site in OUT/",
    )
    parser.add_argument(
        "--site-index",
        type=int,
        default=None,
        help="with --layout single: which site, 1-based (1 Cleveland, 2 Hungarian, "
        "3 VA Long Beach, 4 Switzerland)",
    )
    parser.add_argument(
        "--num-sites",
        type=int,
        choices=[3, 4],
        default=4,
        help="3 leaves out Switzerland (8 patients without disease)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="split seed (the expected counts are checked for 42 only)",
    )
    args = parser.parse_args(argv)
    if args.layout == "single":
        if args.site_index is None:
            parser.error("--layout single needs --site-index K")
        if not 1 <= args.site_index <= args.num_sites:
            parser.error(f"--site-index must be between 1 and {args.num_sites}")
    elif args.site_index is not None:
        parser.error("--site-index is only used with --layout single")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    raw_dir = args.raw_dir.expanduser().resolve()
    out = args.out.expanduser().resolve()
    template_dir = Path(__file__).resolve().parent
    if out == template_dir or template_dir in out.parents:
        sys.exit(
            "--out is inside the template folder, which is uploaded as training code; "
            "choose a folder outside it (and outside any repository)."
        )

    if args.download:
        download_raw(raw_dir)

    if args.layout == "local-test":
        site_indices = list(range(args.num_sites))
    else:
        site_indices = [args.site_index - 1]
    results = {}
    for site_index in site_indices:
        results[f"fl-client-{site_index + 1}"] = prepare_site(
            raw_dir, site_index, args.seed
        )

    # Check before writing anything.
    problems = check_expected(results, args.seed)
    if problems:
        print("The prepared data differ from the expected values for seed 42:")
        for problem in problems:
            print("  " + problem)
        sys.exit("Nothing was written.")

    print(
        f"{'site':<12} {'hospital':<14} {'split':<6} {'n':>4} {'pos':>4} {'prev':>6}  X shape"
    )
    total_bytes = 0
    for name, prepared in results.items():
        info = prepared["info"]
        folder = out / name if args.layout == "local-test" else out
        total_bytes += write_site(folder, prepared)
        for part in SPLITS:
            c = info["counts"][part]
            shape = prepared["arrays"][part]["X"].shape
            print(
                f"{name:<12} {info['source_site']:<14} {part:<6} {c['n']:>4} {c['positives']:>4} "
                f"{c['positives'] / c['n']:>6.1%}  {shape}"
            )
        print(f"{'':<12} test fingerprint {info['test_fingerprint']}  -> {folder}")
    if args.seed == EXPECTED_SEED:
        print("Counts and test fingerprints match the expected values for seed 42.")
    else:
        print(
            f"Seed {args.seed}: expected values exist for seed {EXPECTED_SEED} only; not checked."
        )
    print(f"Wrote {total_bytes:,} bytes to {out}")

    if args.layout == "local-test" and out.is_dir():
        extra = sorted(
            p.name
            for p in out.glob("fl-client-*")
            if p.is_dir() and p.name not in results
        )
        if extra:
            print(
                f"Warning: {out} also holds {extra} from an earlier run; remove them before "
                "you import the fl-client-* folders into SwarmMed."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
