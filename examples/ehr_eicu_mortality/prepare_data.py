#!/usr/bin/env python3
"""Prepare the eICU Collaborative Research Database Demo for the SwarmMed template ``ehr_eicu_mortality``.

Task: at 24 hours after ICU admission, predict death before hospital discharge from data of the
first 24 hours (adult first ICU stays of at least 24 hours). Sites are US census regions of the
eICU hospitals: Midwest, South and West (fl-client-1, -2, -3); Northeast can be added as a fourth
site with ``--regions Midwest,South,West,Northeast``.

The script converts the public demo tables into the data layout of SwarmMed templates:

    --layout local-test   OUT/fl-client-1/ ... OUT/fl-client-N/: one folder per region, for a
                          local-test network on one host (each client reads its own folder)
    --layout single       OUT/ holds one region's files (--site-index K, 1-based), for a real
                          deployment in which each site registers only its own data

Every site folder holds ``train.npz``, ``val.npz``, ``test.npz`` and ``site_info.json``.
The script

 1. checks the SHA-256 of every raw table against the values PhysioNet publishes,
 2. builds the cohort (first ICU stay of a hospital stay, one stay per patient, age >= 18,
    known discharge status, ICU stay >= 24 h, region known) and labels each stay 1 if the
    patient died in hospital,
 3. computes 41 features from the first 24 hours (labs from 6 hours before admission),
 4. splits each region's patients 60/20/20 into train/val/test, stratified by label, with a
    fixed seed,
 5. imputes missing values and standardises the continuous features with statistics of the
    region's own training split (nothing is pooled across regions),
 6. checks the cohort flow, the counts and a fingerprint of the test cases against the values
    expected for seed 42 and stops before writing anything if they differ,
 7. writes the arrays and a description of the site (``site_info.json``).

Usage:
    python prepare_data.py --raw-dir RAW --out OUT                      # 3 regions, local test
    python prepare_data.py --raw-dir RAW --out OUT --layout single --site-index 2
    python prepare_data.py --raw-dir RAW --out OUT --download           # fetch the tables first

RAW holds the gzipped tables of the demo (patient, hospital, vitalPeriodic, vitalAperiodic,
nurseCharting, lab, apachePatientResult). Only numpy, pandas and the Python standard library are
needed. The demo is licensed under the ODbL: the prepared files are a derived database. Keep OUT
outside this template folder and outside any repository, and do not publish the prepared files
(every file in the template folder is uploaded as training code).
"""

import argparse
import hashlib
import io
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------- dataset facts

DATASET = "eICU Collaborative Research Database Demo"
DATASET_VERSION = "2.0.1"
DATASET_PAGE = "https://physionet.org/content/eicu-crd-demo/2.0.1/"
FILES_URL = "https://physionet.org/files/eicu-crd-demo/2.0.1/"
DATASET_DOI = "https://doi.org/10.13026/4mxk-na84"
LICENCE = "Open Data Commons Open Database License v1.0 (ODbL)"
LICENCE_URL = "https://physionet.org/content/eicu-crd-demo/view-license/2.0.1/"
CITATION = [
    "Johnson A, Pollard T, Badawi O, Raffa J (2021). eICU Collaborative Research Database Demo "
    "(version 2.0.1). PhysioNet. RRID:SCR_007345. https://doi.org/10.13026/4mxk-na84",
    "Pollard TJ, Johnson AEW, Raffa JD, Celi LA, Mark RG, Badawi O. The eICU Collaborative "
    "Research Database, a freely available multi-center database for critical care research. "
    "Scientific Data. 2018;5:180178. https://doi.org/10.1038/sdata.2018.178",
    "Pollard T, Moody BE, Lehman L, Gow B, Fernandes C, Xie C, Johnson A, Mark RG, Heldt T. "
    "PhysioNet as a global platform for biomedical research. Nature Health. 2026;1(8):792-795. "
    "https://doi.org/10.1038/s44360-026-00096-z",
]
TASK = (
    "Binary classification: death before hospital discharge, predicted at 24 h after ICU "
    "admission from data of the first 24 h (adult first ICU stays of at least 24 h)"
)
LABEL_MAP = {"0": "discharged alive", "1": "died in hospital"}

# The raw tables used, with their size and the SHA-256 published in SHA256SUMS.txt of the
# PhysioNet project (checked on 11 Nov 2026). Together 44,930,940 bytes.
RAW_FILES = {
    "patient.csv.gz": (
        136_791,
        "15e1eb52169828e3cfff3b67132a087298de4d0365bbb31bb294f6043bffd474",
    ),
    "hospital.csv.gz": (
        759,
        "4ed356f9c4e7c8ce80e89241697e94b77f21c2f325cd6ad126794b1e92805ef4",
    ),
    "vitalPeriodic.csv.gz": (
        19_391_070,
        "04dc093a2e77cd4c52ac7f2a47a8356fbb7456c7f15deb20d5abb1ca4303231d",
    ),
    "vitalAperiodic.csv.gz": (
        2_619_980,
        "c294f46647990cb648b3ebfa589e83f330bdc0c2c922b5e8f23aa0ef99954635",
    ),
    "nurseCharting.csv.gz": (
        16_838_725,
        "475000d63d4ef883b2e891d7197997201300bedc13fcf23e67add1abee11dafe",
    ),
    "lab.csv.gz": (
        5_729_763,
        "e06852850abeb65211bc22ac43f17121ba41b14656fac862396449af84d8273a",
    ),
    "apachePatientResult.csv.gz": (
        214_122,
        "ce799692737f7a712c59aa8fbb9a302cee7c719887f0638caed3ae144b166821",
    ),
}

# Regions in fixed order. The position here (0-based) seeds the split of that region; it
# belongs to the region, not to the fl-client number.
REGION_ORDER = ["Midwest", "South", "West", "Northeast"]
DEFAULT_REGIONS = ["Midwest", "South", "West"]

# ----------------------------------------------------------------------------- features

# Feature windows in minutes from ICU admission: [start, end).
VITAL_WINDOW = (0, 1440)  # vital signs and nurse charting: first 24 h
LAB_WINDOW = (-360, 1440)  # laboratory results: 6 h before to 24 h after ICU admission

# Vital signs: (prefix, [(table, column), ...], valid range, statistics, label, unit).
# "vp" = vitalPeriodic, "va" = vitalAperiodic; values of several columns are pooled.
VITALS = [
    (
        "hr",
        [("vp", "heartrate")],
        (20, 250),
        ["min", "max", "mean"],
        "Heart rate",
        "bpm",
    ),
    (
        "rr",
        [("vp", "respiration")],
        (4, 60),
        ["min", "max", "mean"],
        "Respiratory rate",
        "/min",
    ),
    ("spo2", [("vp", "sao2")], (50, 100), ["min", "mean"], "SpO2", "%"),
    (
        "sbp",
        [("va", "noninvasivesystolic"), ("vp", "systemicsystolic")],
        (40, 300),
        ["min", "mean"],
        "Systolic blood pressure",
        "mm Hg",
    ),
    (
        "map",
        [("va", "noninvasivemean"), ("vp", "systemicmean")],
        (20, 200),
        ["min", "mean"],
        "Mean arterial pressure",
        "mm Hg",
    ),
]
# Nurse charting: (prefix, nursingchartcelltypevalname, valid range, statistics, label, unit).
NURSE_CHARTING = [
    ("temp", "Temperature (C)", (30, 45), ["min", "max"], "Temperature", "degC"),
    ("gcs", "GCS Total", (3, 15), ["min"], "Glasgow Coma Scale total", "points"),
]
# Laboratory: (prefix, labname as in lab.csv.gz, valid range, statistics, label, unit).
LABS = [
    ("creat", "creatinine", (0.1, 25), ["max"], "Creatinine", "mg/dL"),
    ("bun", "BUN", (1, 250), ["max"], "Blood urea nitrogen", "mg/dL"),
    ("na", "sodium", (100, 180), ["min", "max"], "Sodium", "mmol/L"),
    ("k", "potassium", (1.5, 10), ["max"], "Potassium", "mmol/L"),
    ("glucose", "glucose", (10, 2000), ["max"], "Glucose (laboratory)", "mg/dL"),
    ("hco3", "bicarbonate", (2, 60), ["min"], "Bicarbonate", "mmol/L"),
    ("wbc", "WBC x 1000", (0.1, 200), ["max"], "White blood cells", "K/mcL"),
    ("hgb", "Hgb", (2, 25), ["min"], "Haemoglobin", "g/dL"),
    ("plt", "platelets x 1000", (1, 2000), ["min"], "Platelets", "K/mcL"),
    ("lactate", "lactate", (0.1, 30), ["max"], "Lactate", "mmol/L"),
    ("bili", "total bilirubin", (0.1, 80), ["max"], "Total bilirubin", "mg/dL"),
    ("albumin", "albumin", (0.5, 7), ["min"], "Albumin", "g/dL"),
]
# ICU type and admission source: eICU value -> one-hot category; anything else -> all 0
# (unit type) or "other" (admission source, also when missing).
UNIT_TYPES = ["medsurg", "medical", "cardiac", "surg_neuro"]
UNIT_MAP = {
    "Med-Surg ICU": "medsurg",
    "MICU": "medical",
    "Cardiac ICU": "cardiac",
    "CCU-CTICU": "cardiac",
    "CSICU": "cardiac",
    "CTICU": "cardiac",
    "SICU": "surg_neuro",
    "Neuro ICU": "surg_neuro",
}
ADMIT_SOURCES = ["ed", "or", "ward", "other"]
SOURCE_MAP = {
    "Emergency Department": "ed",
    "Chest Pain Center": "ed",
    "Operating Room": "or",
    "Recovery Room": "or",
    "PACU": "or",
    "Floor": "ward",
    "Acute Care/Floor": "ward",
    "Step-Down Unit (SDU)": "ward",
    "ICU to SDU": "ward",
}


def feature_table() -> list[dict]:
    """The 41 model inputs in their fixed order: 32 continuous, 1 binary, 8 one-hot.

    Each entry: name, kind, label, unit, source and, for measurements, the valid range.
    """
    rows = [  # (name, kind, label, unit, source, valid range)
        ("age", "continuous", "Age (> 89 coded as 90)", "years", "patient.age", None),
        ("height_cm", "continuous", "Admission height", "cm",
         "patient.admissionheight", (120, 230)),
        ("weight_kg", "continuous", "Admission weight", "kg",
         "patient.admissionweight", (30, 300)),
        ("pre_icu_days", "continuous", "Hospital days before ICU admission", "days",
         "clip(-patient.hospitaladmitoffset, 0, 43200) / 1440", None),
    ]  # fmt: skip
    table_name = {"vp": "vitalPeriodic", "va": "vitalAperiodic"}
    for prefix, sources, valid, stats, label, unit in VITALS:
        source = " + ".join(f"{table_name[t]}.{column}" for t, column in sources)
        for stat in stats:
            rows.append(
                (f"{prefix}_{stat}", "continuous", f"{label} ({stat}, 0-24 h)", unit,
                 source, valid)
            )  # fmt: skip
    for prefix, valname, valid, stats, label, unit in NURSE_CHARTING:
        for stat in stats:
            rows.append(
                (f"{prefix}_{stat}", "continuous", f"{label} ({stat}, 0-24 h)", unit,
                 f"nurseCharting '{valname}'", valid)
            )  # fmt: skip
    for prefix, labname, valid, stats, label, unit in LABS:
        for stat in stats:
            rows.append(
                (f"{prefix}_{stat}", "continuous", f"{label} ({stat}, -6 to 24 h)", unit,
                 f"lab '{labname}'", valid)
            )  # fmt: skip
    rows.append(
        ("male", "binary", "Male sex", "1 = male",
         "patient.gender (Male 1, Female 0, else missing)", None)
    )  # fmt: skip
    for category in UNIT_TYPES:
        rows.append(
            (f"unit_{category}", "onehot", f"ICU type: {category}", "",
             "patient.unittype", None)
        )  # fmt: skip
    for category in ADMIT_SOURCES:
        rows.append(
            (f"src_{category}", "onehot", f"Admitted from: {category}", "",
             "patient.unitadmitsource", None)
        )  # fmt: skip
    features = []
    for name, kind, label, unit, source, valid in rows:
        entry = {
            "name": name,
            "kind": kind,
            "label": label,
            "unit": unit,
            "source": source,
        }
        if valid is not None:
            entry["valid_range"] = list(valid)
        features.append(entry)
    return features


FEATURES = feature_table()
FEATURE_NAMES = [f["name"] for f in FEATURES]
assert len(FEATURE_NAMES) == 41 and len(set(FEATURE_NAMES)) == 41

SPLITS = ("train", "val", "test")
FRAC_VAL = 0.2
FRAC_TEST = 0.2
Z_CLIP = 5.0
DEFAULT_SEED = 42

# Expected result for seed 42 (reference preparation of 11 Nov 2026). The cohort flow up
# to the region step does not depend on the regions chosen. Per region: (n, positives) per
# split and the first 16 hex digits of sha256("\n".join(sorted(test case_ids))). These depend
# only on the region and the seed, so they also hold for --layout single and other region
# lists. Northeast has no expected values and is not checked.
EXPECTED_SEED = 42
EXPECTED_FLOW = [2520, 2119, 1800, 1793, 1770, 1187]
EXPECTED_FLOW_DEFAULT_REGIONS = 994
EXPECTED = {
    "Midwest": (
        {"train": (226, 17), "val": (75, 6), "test": (75, 6)},
        "5821c1f1feecc6ae",
    ),
    "South": (
        {"train": (218, 19), "val": (73, 7), "test": (73, 7)},
        "bb54c2eac08c6b48",
    ),
    "West": ({"train": (152, 15), "val": (51, 5), "test": (51, 5)}, "56b43acb9bd42806"),
}

# ----------------------------------------------------------------------------- raw files


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_raw(raw_dir: Path) -> None:
    """Download the tables in RAW_FILES from PhysioNet into raw_dir.

    A file that is already there with the right checksum is kept. Each download goes to a
    ``.part`` file first and is renamed only after its size and SHA-256 match.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in RAW_FILES.items():
        target = raw_dir / name
        if target.is_file() and sha256_hex(target.read_bytes()) == digest:
            print(f"{name}: present, checksum OK")
            continue
        url = FILES_URL + name
        print(f"Downloading {url} ({size:,} bytes)")
        request = urllib.request.Request(
            url, headers={"User-Agent": "swarmmed-prepare-data"}
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            data = response.read(size + 1)
        if len(data) != size or sha256_hex(data) != digest:
            sys.exit(
                f"{name}: got {len(data):,} bytes with SHA-256 {sha256_hex(data)}; expected "
                f"{size:,} bytes and {digest}. Stopped; nothing was replaced."
            )
        part = raw_dir / (name + ".part")
        part.write_bytes(data)
        part.replace(target)
    print(f"All {len(RAW_FILES)} tables are in {raw_dir} (licence: {LICENCE_URL})")


def read_raw_files(raw_dir: Path) -> dict[str, bytes]:
    """Return the bytes of every table in RAW_FILES; stop if one is missing or differs."""
    data = {}
    for name, (_size, digest) in RAW_FILES.items():
        path = raw_dir / name
        if not path.is_file():
            sys.exit(
                f"{name} not found in {raw_dir}. Run with --download, or download it from "
                f"{FILES_URL} (project page {DATASET_PAGE})."
            )
        content = path.read_bytes()
        if sha256_hex(content) != digest:
            sys.exit(
                f"SHA-256 mismatch for {path}: {sha256_hex(content)}, expected {digest}. The "
                "file differs from the published version 2.0.1; stopped."
            )
        data[name] = content
    return data


def read_table(data: dict[str, bytes], name: str, **kwargs) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(data[name]), compression="gzip", **kwargs)


# ----------------------------------------------------------------------------- cohort and features


def build_cohort(data: dict[str, bytes]) -> tuple[pd.DataFrame, list[dict]]:
    """Apply the cohort rules; return one row per ICU stay (index patientunitstayid) and the flow.

    The rules up to "ICU stay >= 24 h" do not depend on the region; the region filter is
    applied per site later. Step 2 keeps the first stay of each patient in the sort order
    (uniquepid, hospitaldischargeyear, patienthealthsystemstayid, patientunitstayid); the
    database has no chronological order for one patient's hospital stays within a year, so
    this choice is deterministic but arbitrary.
    """
    patient = read_table(data, "patient.csv.gz")
    hospital = read_table(data, "hospital.csv.gz")
    flow = [dict(rule="all unit stays in patient.csv.gz", remaining=len(patient))]

    cohort = patient[patient["unitvisitnumber"] == 1]
    flow.append(
        dict(
            rule="first ICU stay of the hospital stay (unitvisitnumber == 1)",
            remaining=len(cohort),
        )
    )
    cohort = cohort.sort_values(
        [
            "uniquepid",
            "hospitaldischargeyear",
            "patienthealthsystemstayid",
            "patientunitstayid",
        ]
    )
    cohort = cohort.drop_duplicates("uniquepid", keep="first").copy()
    flow.append(dict(rule="one stay per patient (uniquepid)", remaining=len(cohort)))
    cohort["age_years"] = pd.to_numeric(
        cohort["age"].replace({"> 89": "90"}), errors="coerce"
    )
    cohort = cohort[cohort["age_years"] >= 18]
    flow.append(
        dict(
            rule="age >= 18 ('> 89' = 90; missing age excluded)", remaining=len(cohort)
        )
    )
    cohort = cohort[cohort["hospitaldischargestatus"].isin(["Alive", "Expired"])]
    flow.append(
        dict(
            rule="hospital discharge status known (Alive or Expired)",
            remaining=len(cohort),
        )
    )
    cohort = cohort[cohort["unitdischargeoffset"] >= 1440]
    flow.append(
        dict(
            rule="ICU stay of at least 24 h (unitdischargeoffset >= 1440)",
            remaining=len(cohort),
        )
    )

    if hospital["hospitalid"].duplicated().any():
        sys.exit("hospital.csv.gz lists a hospitalid twice; stopped.")
    cohort = cohort.merge(
        hospital[["hospitalid", "region"]], on="hospitalid", how="left"
    )
    cohort = cohort.set_index("patientunitstayid").sort_index()
    return cohort, flow


def aggregate(
    values: pd.DataFrame, prefix: str, valid: tuple, stats: list[str]
) -> pd.DataFrame:
    """Per-stay statistics of the values inside the valid range (inclusive)."""
    low, high = valid
    kept = values[(values["value"] >= low) & (values["value"] <= high)]
    table = kept.groupby("patientunitstayid")["value"].agg(stats)
    table.columns = [f"{prefix}_{stat}" for stat in stats]
    return table


def in_window(
    table: pd.DataFrame, offset: str, window: tuple, ids: set
) -> pd.DataFrame:
    start, end = window
    return table[
        table["patientunitstayid"].isin(ids)
        & (table[offset] >= start)
        & (table[offset] < end)
    ]


def build_features(data: dict[str, bytes], cohort: pd.DataFrame) -> pd.DataFrame:
    """Return X_raw: the 41 features per stay before imputation and scaling (NaN = missing)."""
    ids = set(cohort.index)
    f = pd.DataFrame(index=cohort.index)
    f["age"] = cohort["age_years"]
    f["height_cm"] = cohort["admissionheight"].where(
        cohort["admissionheight"].between(120, 230)
    )
    f["weight_kg"] = cohort["admissionweight"].where(
        cohort["admissionweight"].between(30, 300)
    )
    f["pre_icu_days"] = (-cohort["hospitaladmitoffset"]).clip(
        lower=0, upper=43200
    ) / 1440.0

    tables = {
        "vp": read_table(
            data,
            "vitalPeriodic.csv.gz",
            usecols=[
                "patientunitstayid",
                "observationoffset",
                "heartrate",
                "respiration",
                "sao2",
                "systemicsystolic",
                "systemicmean",
            ],
        ),
        "va": read_table(
            data,
            "vitalAperiodic.csv.gz",
            usecols=[
                "patientunitstayid",
                "observationoffset",
                "noninvasivesystolic",
                "noninvasivemean",
            ],
        ),
    }
    tables = {
        k: in_window(t, "observationoffset", VITAL_WINDOW, ids)
        for k, t in tables.items()
    }
    for prefix, sources, valid, stats, _label, _unit in VITALS:
        values = pd.concat(
            [
                tables[t][["patientunitstayid", column]]
                .rename(columns={column: "value"})
                .dropna()
                for t, column in sources
            ]
        )
        f = f.join(aggregate(values, prefix, valid, stats))

    nurse = read_table(
        data,
        "nurseCharting.csv.gz",
        usecols=[
            "patientunitstayid",
            "nursingchartoffset",
            "nursingchartcelltypevalname",
            "nursingchartvalue",
        ],
        dtype={"nursingchartvalue": str},
    )
    nurse = in_window(nurse, "nursingchartoffset", VITAL_WINDOW, ids)
    # Text values such as "Unable to score due to medication" become missing.
    nurse = nurse.assign(
        value=pd.to_numeric(nurse["nursingchartvalue"], errors="coerce")
    )
    nurse = nurse.dropna(subset=["value"])
    for prefix, valname, valid, stats, _label, _unit in NURSE_CHARTING:
        f = f.join(
            aggregate(
                nurse[nurse["nursingchartcelltypevalname"] == valname],
                prefix,
                valid,
                stats,
            )
        )

    lab = read_table(
        data,
        "lab.csv.gz",
        usecols=["patientunitstayid", "labresultoffset", "labname", "labresult"],
    )
    lab = in_window(lab, "labresultoffset", LAB_WINDOW, ids)
    lab = lab.rename(columns={"labresult": "value"}).dropna(subset=["value"])
    for prefix, labname, valid, stats, _label, _unit in LABS:
        f = f.join(aggregate(lab[lab["labname"] == labname], prefix, valid, stats))

    f["male"] = cohort["gender"].map({"Male": 1.0, "Female": 0.0})
    unit_type = cohort["unittype"].map(UNIT_MAP)
    for category in UNIT_TYPES:
        f[f"unit_{category}"] = (unit_type == category).astype(float)
    admit_source = cohort["unitadmitsource"].map(SOURCE_MAP).fillna("other")
    for category in ADMIT_SOURCES:
        f[f"src_{category}"] = (admit_source == category).astype(float)

    for (
        name
    ) in FEATURE_NAMES:  # a measurement that no stay has becomes an all-NaN column
        if name not in f:
            f[name] = np.nan
    return f[FEATURE_NAMES]


def apache_reference(data: dict[str, bytes], index: pd.Index) -> np.ndarray:
    """APACHE IVa predicted hospital mortality per stay (NaN if absent or -1); never an input."""
    apache = read_table(
        data,
        "apachePatientResult.csv.gz",
        usecols=["patientunitstayid", "apacheversion", "predictedhospitalmortality"],
    )
    apache = apache[apache["apacheversion"] == "IVa"].drop_duplicates(
        "patientunitstayid"
    )
    predicted = pd.to_numeric(
        apache.set_index("patientunitstayid")["predictedhospitalmortality"],
        errors="coerce",
    ).reindex(index)
    return predicted.where(predicted >= 0).to_numpy(dtype=float)


# ----------------------------------------------------------------------------- per-site steps


def split_site(
    groups: np.ndarray, y: np.ndarray, seed: int, site_index: int
) -> np.ndarray:
    """Patient-level split stratified by label; return 'train'/'val'/'test' for every row.

    Groups (here: one uniquepid per row) are sorted, then shuffled per class (class 0 first,
    then class 1) with one generator seeded by (seed, site_index). Of each class, round(20%)
    goes to test, round(20%) to val and the rest to train. math.floor(x + 0.5) rounds halves
    up (Python's round() would round them to even).
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
                [-5, 5] (0 if the standard deviation is 0; a column with no observed training
                value becomes all 0).
    binary:     missing -> 1 if more than half of the observed training values are 1, else 0.
    one-hot:    complete by construction; used as is.
    No missing-value indicators are added.
    """
    train = split == "train"
    columns, info = [], []
    for j, feature in enumerate(FEATURES):
        x = x_raw[:, j].astype(float)
        missing = np.isnan(x)
        observed = x[train & ~missing]
        entry = dict(
            feature,
            impute=None,
            mean=None,
            std=None,
            missing_train=int((missing & train).sum()),
            missing_all=int(missing.sum()),
        )
        if feature["kind"] == "continuous":
            if len(observed) == 0:
                z = np.zeros_like(x)
            else:
                median = float(np.median(observed))
                filled = np.where(missing, median, x)
                mean, std = float(filled[train].mean()), float(filled[train].std())
                if std == 0:
                    z = np.zeros_like(x)
                else:
                    z = np.clip((filled - mean) / std, -Z_CLIP, Z_CLIP)
                entry.update(impute=median, mean=mean, std=std)
        elif feature["kind"] == "binary":
            fill = (
                0.0 if len(observed) == 0 else (1.0 if observed.mean() > 0.5 else 0.0)
            )
            z = np.where(missing, fill, x)
            entry["impute"] = fill
        else:  # one-hot
            if missing.any():
                sys.exit(
                    f"one-hot column {feature['name']} has missing values; stopped."
                )
            z = x.copy()
        columns.append(z)
        info.append(entry)
    return np.column_stack(columns).astype(np.float32), info


def fingerprint(case_ids) -> str:
    """First 16 hex digits of the SHA-256 of the sorted case ids, one per line."""
    return sha256_hex("\n".join(sorted(str(c) for c in case_ids)).encode())[:16]


def prepare_site(cohort, x_raw_all, apache, region, k, seed, flow, raw_files) -> dict:
    """Split, preprocess and describe one region (fl-client-k); return its arrays and info."""
    rows = (cohort["region"] == region).to_numpy()
    site = cohort[rows]
    x_raw = x_raw_all[rows]
    y = (site["hospitaldischargestatus"] == "Expired").to_numpy().astype(np.int64)
    site_index = REGION_ORDER.index(region)
    split = split_site(site["uniquepid"].to_numpy(), y, seed, site_index)
    x, features = preprocess(x_raw, split)
    names = np.array(FEATURE_NAMES, dtype=str)
    case_ids = site.index.astype(str).to_numpy(dtype=str)
    reference = apache[rows]
    arrays, counts = {}, {}
    for part in SPLITS:
        r = split == part
        arrays[part] = {
            "X": x[r],
            "X_raw": x_raw[r].astype(np.float32),
            "y": y[r],
            "case_id": case_ids[r],
            "feature_names": names,
            "ref_apache_iva": reference[r].astype(np.float32),
        }
        counts[part] = {"n": int(r.sum()), "positives": int(y[r].sum())}
    region_flow = flow + [dict(rule=f"region == {region}", remaining=int(rows.sum()))]
    info = {
        "dataset": DATASET,
        "dataset_version": DATASET_VERSION,
        "source_url": DATASET_PAGE,
        "download_url": FILES_URL,
        "doi": DATASET_DOI,
        "licence": LICENCE,
        "licence_url": LICENCE_URL,
        "licence_note": (
            "The prepared files are a derived database of the eICU-CRD Demo under the ODbL: "
            "do not publish them except under the ODbL, and attribute the source."
        ),
        "citation": CITATION,
        "site_name": f"fl-client-{k}",
        "source_site": region,
        "source_description": (
            f"ICU stays at eICU hospitals in the US census region {region} "
            f"({site['hospitalid'].nunique()} hospitals in this cohort; hospital.region)"
        ),
        "task": TASK,
        "label_map": LABEL_MAP,
        "seed": seed,
        "site_index": site_index,
        "split": {
            "train": round(1 - FRAC_VAL - FRAC_TEST, 2),
            "val": FRAC_VAL,
            "test": FRAC_TEST,
            "unit": "patient (uniquepid); one ICU stay per patient",
            "stratified_by": "label",
        },
        "counts": counts,
        "test_fingerprint": fingerprint(arrays["test"]["case_id"]),
        "cohort_flow": region_flow,
        "windows_minutes": {
            "vitals_and_nurse_charting": list(VITAL_WINDOW),
            "labs": list(LAB_WINDOW),
        },
        "preprocessing": {
            "label": "y = 1 if patient.hospitaldischargestatus == 'Expired' else 0",
            "statistics": "from this site's training split only",
            "continuous": "values outside the valid range dropped before aggregation; median "
            "imputation; z-score (population std), clipped to [-5, 5]",
            "binary": "imputed with the training majority value",
            "onehot": "unit type and admission source; complete",
            "missing_indicators": False,
        },
        "features": features,
        "raw_files": raw_files,
        "case_id": "str(patient.patientunitstayid) (the demo's pseudonymous stay id)",
        "arrays": {
            "X": "float32 [n, 41] model input",
            "X_raw": "float32 [n, 41] the same columns before imputation and scaling "
            "(NaN = missing)",
            "y": "int64 [n] label",
            "case_id": "str [n] pseudonymous case id",
            "feature_names": "str [41]",
            "ref_apache_iva": "float32 [n] APACHE IVa predicted hospital mortality from "
            "apachePatientResult (NaN if absent); a reference score, never a model input",
        },
        "generator": "examples/ehr_eicu_mortality/prepare_data.py (SwarmMed)",
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


def check_expected(
    flow: list[dict], regions: list[str], results: dict, seed: int
) -> list[str]:
    """Compare the cohort flow, counts and test fingerprints with the expected values."""
    problems = []
    got_flow = [step["remaining"] for step in flow]
    if got_flow != EXPECTED_FLOW:
        problems.append(f"cohort flow {got_flow}, expected {EXPECTED_FLOW}")
    if seed != EXPECTED_SEED:
        return problems
    if regions == DEFAULT_REGIONS:
        total = sum(
            sum(c["n"] for c in p["info"]["counts"].values()) for p in results.values()
        )
        # With --layout single only one region is prepared, so the total is checked only for
        # the full local-test layout.
        if (
            len(results) == len(DEFAULT_REGIONS)
            and total != EXPECTED_FLOW_DEFAULT_REGIONS
        ):
            problems.append(
                f"{total} stays in the 3 regions, expected "
                f"{EXPECTED_FLOW_DEFAULT_REGIONS}"
            )
    for name, prepared in results.items():
        info = prepared["info"]
        if info["source_site"] not in EXPECTED:
            continue
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


def parse_regions(text: str) -> list[str]:
    regions = [r.strip() for r in text.split(",") if r.strip()]
    unknown = [r for r in regions if r not in REGION_ORDER]
    if unknown or not regions or len(set(regions)) != len(regions):
        raise argparse.ArgumentTypeError(
            f"a comma-separated list of distinct regions from {REGION_ORDER}, got {text!r}"
        )
    return regions


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare the eICU-CRD Demo 2.0.1 for the SwarmMed EHR template "
        "(in-hospital mortality from the first 24 h, one site per US region)."
    )
    parser.add_argument(
        "--raw-dir",
        required=True,
        type=Path,
        help="folder with the gzipped demo tables (patient.csv.gz, ...)",
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
        help=f"download the {len(RAW_FILES)} tables from PhysioNet into "
        "--raw-dir first (44.9 MB; files already there are kept)",
    )
    parser.add_argument(
        "--layout",
        choices=["local-test", "single"],
        default="local-test",
        help="local-test: OUT/fl-client-k/ for every region; single: one "
        "region in OUT/",
    )
    parser.add_argument(
        "--site-index",
        type=int,
        default=None,
        help="with --layout single: which region of --regions, 1-based "
        "(default regions: 1 Midwest, 2 South, 3 West)",
    )
    parser.add_argument(
        "--regions",
        type=parse_regions,
        default=list(DEFAULT_REGIONS),
        help="sites in fl-client order (default Midwest,South,West; append "
        "Northeast for a fourth site with 13 deaths)",
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
        if not 1 <= args.site_index <= len(args.regions):
            parser.error(f"--site-index must be between 1 and {len(args.regions)}")
    elif args.site_index is not None:
        parser.error("--site-index is only used with --layout single")
    return args


def inside_git_work_tree(path: Path) -> Path | None:
    """Return the repository root if path lies inside a git work tree, else None."""
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def main(argv=None) -> int:
    started = time.perf_counter()
    args = parse_args(argv)
    raw_dir = args.raw_dir.expanduser().resolve()
    out = args.out.expanduser().resolve()
    template_dir = Path(__file__).resolve().parent
    if out == template_dir or template_dir in out.parents:
        sys.exit(
            "--out is inside the template folder, which is uploaded as training code; "
            "choose a folder outside it (and outside any repository)."
        )
    repo = inside_git_work_tree(out)
    if repo is not None:
        print(
            f"Warning: {out} is inside the git work tree {repo}. The prepared files are a "
            "derived database under the ODbL; make sure they are git-ignored and never "
            "committed or published."
        )

    if args.download:
        download_raw(raw_dir)
    data = read_raw_files(raw_dir)
    raw_files = {
        name: {"bytes": len(content), "sha256": sha256_hex(content)}
        for name, content in data.items()
    }
    print(f"Checked the SHA-256 of {len(data)} tables in {raw_dir}")

    cohort, flow = build_cohort(data)
    x_raw_all = build_features(data, cohort).to_numpy(dtype=float)
    apache = apache_reference(data, cohort.index)

    if args.layout == "local-test":
        positions = list(range(1, len(args.regions) + 1))
    else:
        positions = [args.site_index]
    results = {}
    for k in positions:
        region = args.regions[k - 1]
        results[f"fl-client-{k}"] = prepare_site(
            cohort, x_raw_all, apache, region, k, args.seed, flow, raw_files
        )

    print("Cohort flow (region-independent steps):")
    for step, expected in zip(flow, EXPECTED_FLOW, strict=True):
        print(f"  {step['remaining']:>5}  (expected {expected:>5})  {step['rule']}")

    # Check before writing anything.
    problems = check_expected(flow, args.regions, results, args.seed)
    if problems:
        print("The prepared data differ from the expected values:")
        for problem in problems:
            print("  " + problem)
        sys.exit("Nothing was written.")

    print(
        f"{'site':<12} {'region':<10} {'split':<6} {'n':>4} {'deaths':>6} {'prev':>6}  X shape"
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
                f"{name:<12} {info['source_site']:<10} {part:<6} {c['n']:>4} "
                f"{c['positives']:>6} {c['positives'] / c['n']:>6.1%}  {shape}"
            )
        note = (
            ""
            if info["source_site"] in EXPECTED
            else " (no expected values for this region)"
        )
        print(
            f"{'':<12} test fingerprint {info['test_fingerprint']}{note}  -> {folder}"
        )
    unchecked = [p["info"]["source_site"] for p in results.values()]
    unchecked = [region for region in unchecked if region not in EXPECTED]
    if args.seed == EXPECTED_SEED:
        print(
            "Cohort flow, counts and test fingerprints match the expected values for seed 42"
            + (f" (not checked: {', '.join(unchecked)})." if unchecked else ".")
        )
    else:
        print(
            f"Seed {args.seed}: only the cohort flow was checked (counts and fingerprints "
            f"are known for seed {EXPECTED_SEED})."
        )
    print(
        f"Wrote {total_bytes:,} bytes to {out} in {time.perf_counter() - started:.1f} s"
    )

    # Site folders that this run did not write (all of them for --layout single) would be
    # imported too and mix data of different runs or layouts.
    written = set(results) if args.layout == "local-test" else set()
    stale = sorted(
        p.name for p in out.glob("fl-client-*") if p.is_dir() and p.name not in written
    )
    if stale:
        print(
            f"Warning: {out} also holds {stale} from an earlier run; remove them before "
            "you import the data into SwarmMed (fl-client-* or * would include them)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
