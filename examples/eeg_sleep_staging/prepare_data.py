"""Prepare Sleep-EDF Expanded (sleep cassette) for the EEG sleep-staging template.

Run this at a site before importing data into SwarmMedHub. It reads the original
EDF files of 36 sleep-cassette nights, cuts them into labelled 30-s epochs of the
two EEG channels (Fpz-Cz, Pz-Oz) and writes compact NumPy files:

    --layout local-test   OUT/fl-client-1/ ... OUT/fl-client-4/  (all four sites)
    --layout single       OUT/  (one site's files, chosen with --site-index K)

Each site folder holds train.npz (7 subjects), val.npz (1), test.npz (1) and
site_info.json. With --download, the needed EDF files and SHA256SUMS.txt are
fetched from PhysioNet into --raw-dir first. Every EDF file is checked against
the dataset's SHA256SUMS.txt, and the epoch counts are checked against the
values this script was written for.

Only the Python standard library, numpy and scipy are used (no MNE, pyedflib or
xlrd): a small EDF/EDF+ reader is included below.

Examples:
    python prepare_data.py --raw-dir /data/sleep-edfx --out /data/eeg-prepared
    python prepare_data.py --raw-dir /data/sleep-edfx --download \\
        --layout single --site-index 2 --out /data/eeg-site-2
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import platform
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import scipy
from scipy.signal import butter, sosfiltfilt

# --- Dataset ---------------------------------------------------------------------

DATASET = {
    "dataset": "Sleep-EDF Database Expanded (sleep-edfx), sleep cassette study",
    "version": "1.0.0",
    "doi": "10.13026/C2X676",
    "licence": "Open Data Commons Attribution License v1.0 (ODC-By 1.0)",
    "source_url": "https://physionet.org/content/sleep-edfx/1.0.0/",
}
DOWNLOAD_BASE_URL = "https://physionet.org/files/sleep-edfx/1.0.0/"
CHECKSUM_FILE = "SHA256SUMS.txt"
SUBFOLDER = "sleep-cassette"

# 36 of the 78 sleep-cassette subjects, at evenly spaced age ranks (ages 25 to
# 101), one night each (night 2 for subjects 36 and 52, whose first night was
# lost). Sites: subjects sorted by (age, subject) and dealt round-robin to four
# sites. Splits: one test and one validation subject per site, drawn once with
# numpy default_rng(20261111) and fixed here.
# (site, split, subject, night, PSG file, hypnogram file)
SELECTION = [
    ("fl-client-1", "test", 46, 1, "SC4461F0-PSG.edf", "SC4461FA-Hypnogram.edf"),
    ("fl-client-1", "train", 1, 1, "SC4011E0-PSG.edf", "SC4011EH-Hypnogram.edf"),
    ("fl-client-1", "train", 5, 1, "SC4051E0-PSG.edf", "SC4051EC-Hypnogram.edf"),
    ("fl-client-1", "train", 8, 1, "SC4081E0-PSG.edf", "SC4081EC-Hypnogram.edf"),
    ("fl-client-1", "train", 36, 2, "SC4362F0-PSG.edf", "SC4362FC-Hypnogram.edf"),
    ("fl-client-1", "train", 52, 2, "SC4522E0-PSG.edf", "SC4522EM-Hypnogram.edf"),
    ("fl-client-1", "train", 64, 1, "SC4641E0-PSG.edf", "SC4641EP-Hypnogram.edf"),
    ("fl-client-1", "train", 70, 1, "SC4701E0-PSG.edf", "SC4701EC-Hypnogram.edf"),
    ("fl-client-1", "val", 25, 1, "SC4251E0-PSG.edf", "SC4251EP-Hypnogram.edf"),
    ("fl-client-2", "test", 23, 1, "SC4231E0-PSG.edf", "SC4231EJ-Hypnogram.edf"),
    ("fl-client-2", "train", 2, 1, "SC4021E0-PSG.edf", "SC4021EH-Hypnogram.edf"),
    ("fl-client-2", "train", 24, 1, "SC4241E0-PSG.edf", "SC4241EC-Hypnogram.edf"),
    ("fl-client-2", "train", 40, 1, "SC4401E0-PSG.edf", "SC4401EC-Hypnogram.edf"),
    ("fl-client-2", "train", 50, 1, "SC4501E0-PSG.edf", "SC4501EW-Hypnogram.edf"),
    ("fl-client-2", "train", 67, 1, "SC4671G0-PSG.edf", "SC4671GJ-Hypnogram.edf"),
    ("fl-client-2", "train", 74, 1, "SC4741E0-PSG.edf", "SC4741EA-Hypnogram.edf"),
    ("fl-client-2", "train", 82, 1, "SC4821G0-PSG.edf", "SC4821GC-Hypnogram.edf"),
    ("fl-client-2", "val", 19, 1, "SC4191E0-PSG.edf", "SC4191EP-Hypnogram.edf"),
    ("fl-client-3", "test", 6, 1, "SC4061E0-PSG.edf", "SC4061EC-Hypnogram.edf"),
    ("fl-client-3", "train", 10, 1, "SC4101E0-PSG.edf", "SC4101EC-Hypnogram.edf"),
    ("fl-client-3", "train", 31, 1, "SC4311E0-PSG.edf", "SC4311EC-Hypnogram.edf"),
    ("fl-client-3", "train", 49, 1, "SC4491G0-PSG.edf", "SC4491GJ-Hypnogram.edf"),
    ("fl-client-3", "train", 56, 1, "SC4561F0-PSG.edf", "SC4561FJ-Hypnogram.edf"),
    ("fl-client-3", "train", 66, 1, "SC4661E0-PSG.edf", "SC4661EJ-Hypnogram.edf"),
    ("fl-client-3", "train", 75, 1, "SC4751E0-PSG.edf", "SC4751EC-Hypnogram.edf"),
    ("fl-client-3", "train", 81, 1, "SC4811G0-PSG.edf", "SC4811GG-Hypnogram.edf"),
    ("fl-client-3", "val", 20, 1, "SC4201E0-PSG.edf", "SC4201EC-Hypnogram.edf"),
    ("fl-client-4", "test", 41, 1, "SC4411E0-PSG.edf", "SC4411EJ-Hypnogram.edf"),
    ("fl-client-4", "train", 13, 1, "SC4131E0-PSG.edf", "SC4131EC-Hypnogram.edf"),
    ("fl-client-4", "train", 17, 1, "SC4171E0-PSG.edf", "SC4171EU-Hypnogram.edf"),
    ("fl-client-4", "train", 26, 1, "SC4261F0-PSG.edf", "SC4261FM-Hypnogram.edf"),
    ("fl-client-4", "train", 47, 1, "SC4471F0-PSG.edf", "SC4471FA-Hypnogram.edf"),
    ("fl-client-4", "train", 61, 1, "SC4611E0-PSG.edf", "SC4611EG-Hypnogram.edf"),
    ("fl-client-4", "train", 72, 1, "SC4721E0-PSG.edf", "SC4721EC-Hypnogram.edf"),
    ("fl-client-4", "train", 80, 1, "SC4801G0-PSG.edf", "SC4801GC-Hypnogram.edf"),
    ("fl-client-4", "val", 59, 1, "SC4591G0-PSG.edf", "SC4591GY-Hypnogram.edf"),
]
SITES = ["fl-client-1", "fl-client-2", "fl-client-3", "fl-client-4"]
SPLITS = ["train", "val", "test"]

# Labelled epochs per site and split after trimming: (n, W, N1, N2, N3, REM).
# A run of this script with the files above must reproduce these numbers.
EXPECTED_COUNTS = {
    ("fl-client-1", "train"): (7718, 2495, 685, 3150, 684, 704),
    ("fl-client-1", "val"): (972, 133, 102, 569, 22, 146),
    ("fl-client-1", "test"): (983, 196, 118, 484, 9, 176),
    ("fl-client-2", "train"): (10970, 4932, 1112, 3362, 324, 1240),
    ("fl-client-2", "val"): (1535, 188, 118, 833, 110, 286),
    ("fl-client-2", "test"): (904, 127, 139, 414, 12, 212),
    ("fl-client-3", "train"): (9859, 3871, 1379, 3234, 344, 1031),
    ("fl-client-3", "val"): (1022, 263, 39, 539, 4, 177),
    ("fl-client-3", "test"): (843, 142, 56, 407, 136, 102),
    ("fl-client-4", "train"): (8738, 2583, 986, 2791, 883, 1495),
    ("fl-client-4", "val"): (1840, 863, 199, 363, 157, 258),
    ("fl-client-4", "test"): (1078, 181, 145, 502, 46, 204),
}

# --- Preprocessing constants -------------------------------------------------------

CHANNELS = ["EEG Fpz-Cz", "EEG Pz-Oz"]
FS = 100  # Hz; checked for every channel
EPOCH_S = 30
EPOCH_SAMPLES = FS * EPOCH_S  # 3000
CLASS_NAMES = ["W", "N1", "N2", "N3", "REM"]
# Rechtschaffen and Kales stages to the five AASM classes (stages 3 and 4 merge
# into N3). "Sleep stage ?", "Movement time" and unannotated epochs are excluded.
STAGE_TO_LABEL = {
    "Sleep stage W": 0,
    "Sleep stage 1": 1,
    "Sleep stage 2": 2,
    "Sleep stage 3": 3,
    "Sleep stage 4": 3,
    "Sleep stage R": 4,
}
UNLABELLED = -1
MARGIN_EPOCHS = (
    60  # 30 min of wake kept before the first and after the last sleep epoch
)
BANDPASS_HZ = (0.3, 35.0)
BANDPASS_SOS = butter(4, BANDPASS_HZ, btype="bandpass", fs=FS, output="sos")
CLIP = 20.0  # robust-scaled values are clipped to [-CLIP, CLIP]

PREPROCESSING = [
    "Read EEG Fpz-Cz and EEG Pz-Oz from the PSG file in microvolts: "
    "(digital - dmin) * (pmax - pmin) / (dmax - dmin) + pmin; both at 100 Hz.",
    "Cut into 30-s epochs by sample index: epoch i is samples 3000*i to 3000*i + 2999; "
    "n_epochs = floor(n_samples / 3000).",
    "Label each epoch from the hypnogram: W=0, stage 1=N1=1, stage 2=N2=2, "
    "stages 3 and 4=N3=3, R=REM=4; 'Sleep stage ?', 'Movement time' and "
    "unannotated epochs are excluded.",
    "Trim to the first N1-REM epoch minus 60 epochs and the last N1-REM epoch plus "
    "60 epochs (30 min of wake on each side), clipped to the recording.",
    "Band-pass filter the kept continuous segment per channel: 4th-order "
    "Butterworth, 0.3-35 Hz, zero phase (scipy.signal.sosfiltfilt).",
    "Scale per recording and channel over the trimmed, filtered segment: "
    "(x - median) / IQR, then clip to [-20, 20]. Nothing is computed across "
    "recordings or sites, but the scored hypnogram chooses the trimmed segment, "
    "so the labels decide which span the median and IQR come from.",
    "Cut into (n, 2, 3000), drop excluded epochs and cast to float16.",
]


# --- EDF and EDF+ reading (numpy only) -------------------------------------------


def read_edf_header(path):
    """Read the fixed and per-signal parts of an EDF/EDF+ header.

    Args:
        path (Path): EDF file.

    Returns:
        dict: Header fields; per-signal fields are lists in signal order.
    """
    with open(path, "rb") as handle:
        fixed = handle.read(256)
        n_signals = int(fixed[252:256].decode("ascii"))
        per_signal = handle.read(256 * n_signals)

    def fields(offset, width):
        return [
            per_signal[offset + i * width : offset + (i + 1) * width]
            .decode("latin-1")
            .strip()
            for i in range(n_signals)
        ]

    # Per-signal header layout: label 16, transducer 80, physical dimension 8,
    # physical min 8, physical max 8, digital min 8, digital max 8,
    # prefiltering 80, samples per data record 8, reserved 32 (bytes each).
    offset = 0
    labels = fields(offset, 16)
    offset += (16 + 80) * n_signals
    units = fields(offset, 8)
    offset += 8 * n_signals
    physical_min = [float(v) for v in fields(offset, 8)]
    offset += 8 * n_signals
    physical_max = [float(v) for v in fields(offset, 8)]
    offset += 8 * n_signals
    digital_min = [int(v) for v in fields(offset, 8)]
    offset += 8 * n_signals
    digital_max = [int(v) for v in fields(offset, 8)]
    offset += (8 + 80) * n_signals
    samples_per_record = [int(v) for v in fields(offset, 8)]

    return {
        "start": (fixed[168:176].decode("ascii"), fixed[176:184].decode("ascii")),
        "header_bytes": int(fixed[184:192].decode("ascii")),
        "record_duration": float(fixed[244:252].decode("ascii")),
        "labels": labels,
        "units": units,
        "physical_min": physical_min,
        "physical_max": physical_max,
        "digital_min": digital_min,
        "digital_max": digital_max,
        "samples_per_record": samples_per_record,
    }


def read_edf_signals(path, header, channel_labels):
    """Read whole channels of an EDF file in physical units.

    Data records are interleaved per signal; the number of records is taken from
    the file size, not from the header.

    Args:
        path (Path): EDF file.
        header (dict): Output of read_edf_header.
        channel_labels (list[str]): Signal labels to return, in this order.

    Returns:
        np.ndarray: float64 array of shape (len(channel_labels), n_samples).
    """
    per_record = header["samples_per_record"]
    record_size = sum(per_record)
    digital = np.fromfile(path, dtype="<i2", offset=header["header_bytes"])
    n_records = digital.size // record_size
    digital = digital[: n_records * record_size].reshape(n_records, record_size)
    signals = []
    for label in channel_labels:
        i = header["labels"].index(label)
        start = sum(per_record[:i])
        values = (
            digital[:, start : start + per_record[i]].reshape(-1).astype(np.float64)
        )
        gain = (header["physical_max"][i] - header["physical_min"][i]) / (
            header["digital_max"][i] - header["digital_min"][i]
        )
        signals.append(
            (values - header["digital_min"][i]) * gain + header["physical_min"][i]
        )
    return np.stack(signals)


def read_edf_annotations(path):
    """Read the annotations of an EDF+ file (time-stamped annotation lists).

    Args:
        path (Path): EDF+ file with an "EDF Annotations" signal.

    Returns:
        tuple[dict, list[tuple[float, float, str]]]: The header and a list of
        (onset seconds, duration seconds, text).
    """
    header = read_edf_header(path)
    i = header["labels"].index("EDF Annotations")
    per_record = header["samples_per_record"]
    record_bytes = 2 * sum(per_record)
    raw = np.fromfile(path, dtype=np.uint8, offset=header["header_bytes"])
    n_records = raw.size // record_bytes
    raw = raw[: n_records * record_bytes].reshape(n_records, record_bytes)
    start = 2 * sum(per_record[:i])
    annotations = []
    for record in raw[:, start : start + 2 * per_record[i]]:
        # TAL: +onset[\x15duration]\x14text\x14[text\x14...]\x00
        for tal in record.tobytes().split(b"\x00"):
            if not tal:
                continue
            parts = tal.split(b"\x14")
            onset_duration = parts[0].split(b"\x15")
            onset = float(onset_duration[0])
            duration = (
                float(onset_duration[1])
                if len(onset_duration) > 1 and onset_duration[1]
                else 0.0
            )
            for text in parts[1:]:
                if text:
                    annotations.append((onset, duration, text.decode("latin-1")))
    return header, annotations


# --- Per-night preprocessing ------------------------------------------------------


def epoch_labels(annotations, n_epochs, source):
    """Return one label per 30-s epoch (UNLABELLED where excluded)."""
    labels = np.full(n_epochs, UNLABELLED, dtype=np.int8)
    for onset, duration, text in annotations:
        if onset % EPOCH_S or duration % EPOCH_S:
            raise ValueError(
                f"{source}: annotation {text!r} at {onset} s with duration "
                f"{duration} s is not aligned to {EPOCH_S}-s epochs"
            )
        first = int(onset // EPOCH_S)
        stop = min(n_epochs, int((onset + duration) // EPOCH_S))  # clip at the PSG end
        if first < n_epochs:
            labels[first:stop] = STAGE_TO_LABEL.get(text, UNLABELLED)
    return labels


def process_night(psg_path, hypnogram_path):
    """Turn one night into labelled, filtered and scaled 30-s epochs.

    Args:
        psg_path (Path): The night's PSG EDF file.
        hypnogram_path (Path): The matching hypnogram EDF+ file.

    Returns:
        dict: X (float16, (n, 2, 3000)), y (int8), epoch (int32 index of the
        epoch in the original recording) and bookkeeping values.
    """
    header = read_edf_header(psg_path)
    hyp_header, annotations = read_edf_annotations(hypnogram_path)
    if header["start"] != hyp_header["start"]:
        raise ValueError(
            f"{hypnogram_path.name} starts at {hyp_header['start']}, "
            f"{psg_path.name} at {header['start']}"
        )
    for label in CHANNELS:
        if label not in header["labels"]:
            raise ValueError(f"{psg_path.name} has no channel {label!r}")
        i = header["labels"].index(label)
        fs = header["samples_per_record"][i] / header["record_duration"]
        if fs != FS or header["units"][i] != "uV":
            raise ValueError(
                f"{psg_path.name} {label}: expected {FS} Hz and uV, found {fs} Hz "
                f"and {header['units'][i]!r}"
            )

    signals = read_edf_signals(psg_path, header, CHANNELS)
    n_epochs = signals.shape[1] // EPOCH_SAMPLES
    labels = epoch_labels(annotations, n_epochs, hypnogram_path.name)

    sleep = np.flatnonzero((labels >= 1) & (labels <= 4))  # N1 to REM
    if sleep.size == 0:
        raise ValueError(f"{hypnogram_path.name}: no sleep epochs")
    first = max(0, int(sleep[0]) - MARGIN_EPOCHS)
    last = min(n_epochs - 1, int(sleep[-1]) + MARGIN_EPOCHS)

    segment = signals[:, first * EPOCH_SAMPLES : (last + 1) * EPOCH_SAMPLES]
    segment = sosfiltfilt(BANDPASS_SOS, segment, axis=1)
    median = np.median(segment, axis=1, keepdims=True)
    iqr = np.subtract(*np.percentile(segment, [75, 25], axis=1)).reshape(-1, 1)
    segment = np.clip((segment - median) / iqr, -CLIP, CLIP)

    epochs = segment.reshape(len(CHANNELS), last - first + 1, EPOCH_SAMPLES)
    epochs = epochs.transpose(1, 0, 2)
    kept_labels = labels[first : last + 1]
    keep = kept_labels != UNLABELLED
    return {
        "X": epochs[keep].astype(np.float16),
        "y": kept_labels[keep],
        "epoch": np.arange(first, last + 1, dtype=np.int32)[keep],
        "n_epochs_recording": int(n_epochs),
        "kept_range": [first, last],
        "dropped_unlabelled": int((~keep).sum()),
    }


# --- Files, checksums, download ---------------------------------------------------


def sha256_file(path):
    """Return the SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_checksums(raw_dir):
    """Map 'sleep-cassette/<file>' to its SHA-256 from SHA256SUMS.txt, if present."""
    path = raw_dir / CHECKSUM_FILE
    if not path.is_file():
        return {}
    checksums = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            checksums[parts[1]] = parts[0]
    return checksums


def locate(raw_dir, name):
    """Find a raw file in RAW/sleep-cassette/ or directly in RAW/."""
    for candidate in (raw_dir / SUBFOLDER / name, raw_dir / name):
        if candidate.is_file():
            return candidate
    return None


def download(url, destination, expected_sha256=None):
    """Download one file to destination, checking its SHA-256 when given."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(
        url, headers={"User-Agent": "swarmmed-prepare-data"}
    )
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        open(partial, "wb") as handle,
    ):
        shutil.copyfileobj(response, handle, length=1 << 20)
    if expected_sha256 and sha256_file(partial) != expected_sha256:
        partial.unlink()
        raise ValueError(f"SHA-256 mismatch for {url}")
    partial.replace(destination)


def fetch_raw_files(raw_dir, names):
    """Download SHA256SUMS.txt and the missing or damaged EDF files from PhysioNet."""
    if not (raw_dir / CHECKSUM_FILE).is_file():
        print(f"Downloading {DOWNLOAD_BASE_URL}{CHECKSUM_FILE}")
        download(DOWNLOAD_BASE_URL + CHECKSUM_FILE, raw_dir / CHECKSUM_FILE)
    checksums = read_checksums(raw_dir)
    todo = []
    for name in names:
        key = f"{SUBFOLDER}/{name}"
        if key not in checksums:
            raise ValueError(f"{key} is not listed in {CHECKSUM_FILE}")
        existing = locate(raw_dir, name)
        if existing is None or sha256_file(existing) != checksums[key]:
            todo.append((name, checksums[key]))
    print(f"Downloading {len(todo)} of {len(names)} EDF files from {DOWNLOAD_BASE_URL}")
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                download,
                f"{DOWNLOAD_BASE_URL}{SUBFOLDER}/{name}",
                raw_dir / SUBFOLDER / name,
                sha256,
            ): name
            for name, sha256 in todo
        }
        for future in concurrent.futures.as_completed(futures):
            future.result()
            print(f"  {futures[future]} OK")
    print(f"Download finished in {time.time() - started:.0f} s")


def verify_raw_files(raw_dir, names):
    """Check every needed EDF file against SHA256SUMS.txt; return the paths."""
    checksums = read_checksums(raw_dir)
    if not checksums:
        print(f"WARNING: {raw_dir / CHECKSUM_FILE} not found; checksums not verified.")
    paths, problems = {}, []
    for name in names:
        path = locate(raw_dir, name)
        if path is None:
            problems.append(f"missing: {name}")
            continue
        expected = checksums.get(f"{SUBFOLDER}/{name}")
        if checksums and expected != sha256_file(path):
            problems.append(f"SHA-256 mismatch or not listed: {name}")
        paths[name] = path
    if problems:
        raise SystemExit(
            "Raw data problems (use --download to fetch the files):\n  "
            + "\n  ".join(problems)
        )
    if checksums:
        print(f"Checked {len(names)} EDF files against {CHECKSUM_FILE}: all match.")
    return paths


def array_sha256(array):
    """SHA-256 of an array's bytes in C order (independent of the npz container)."""
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def class_counts(y):
    """Count labels per class name."""
    counts = np.bincount(y.astype(np.int64), minlength=len(CLASS_NAMES))
    return {name: int(n) for name, n in zip(CLASS_NAMES, counts, strict=True)}


# --- Main -------------------------------------------------------------------------


def parse_args(argv=None):
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--raw-dir",
        required=True,
        type=Path,
        help="Folder with SHA256SUMS.txt and sleep-cassette/*.edf (written with --download)",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output folder")
    parser.add_argument(
        "--layout",
        choices=["local-test", "single"],
        default="local-test",
        help="local-test: OUT/fl-client-1..4/; single: one site's files in OUT/",
    )
    parser.add_argument(
        "--site-index",
        type=int,
        choices=[1, 2, 3, 4],
        help="Site to write with --layout single",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download missing EDF files and SHA256SUMS.txt from PhysioNet",
    )
    parser.add_argument(
        "--no-count-check",
        action="store_true",
        help="Do not stop when epoch counts differ from the expected values",
    )
    args = parser.parse_args(argv)
    if args.layout == "single" and args.site_index is None:
        parser.error("--layout single needs --site-index 1..4")
    if args.layout == "local-test" and args.site_index is not None:
        parser.error("--site-index is used only with --layout single")
    return args


def main(argv=None):
    """Prepare the selected sites and write their files."""
    args = parse_args(argv)
    sites = SITES if args.layout == "local-test" else [SITES[args.site_index - 1]]
    rows = [row for row in SELECTION if row[0] in sites]
    names = [name for row in rows for name in row[4:6]]
    raw_dir = args.raw_dir.resolve()

    if args.download:
        fetch_raw_files(raw_dir, names)
    paths = verify_raw_files(raw_dir, names)

    created = datetime.date.today().isoformat()
    started = time.time()
    summary, mismatches = [], []
    for site in sites:
        site_dir = args.out / site if args.layout == "local-test" else args.out
        site_dir.mkdir(parents=True, exist_ok=True)
        info = {
            **DATASET,
            "site": site,
            "layout": args.layout,
            "task": "5-class sleep staging of 30-s EEG epochs",
            "label_map": {str(i): name for i, name in enumerate(CLASS_NAMES)},
            "channels": CHANNELS,
            "fs": FS,
            "epoch_s": EPOCH_S,
            "n_samples": EPOCH_SAMPLES,
            "arrays": {
                "X": "float16, (N, 2, 3000), channels [Fpz-Cz, Pz-Oz], robust-scaled",
                "y": "int8, (N,), 0-4 = [W, N1, N2, N3, REM]",
                "subject": "int16, (N,), sleep-cassette subject number",
                "night": "int8, (N,), night 1 or 2",
                "epoch": "int32, (N,), index of the 30-s epoch in the recording",
            },
            "subjects": [],
            "counts": {},
            "fingerprints": {},
            "preprocessing": PREPROCESSING,
            "created": created,
            "software": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "scipy": scipy.__version__,
            },
            "arguments": {"layout": args.layout, "site_index": args.site_index},
        }
        for split in SPLITS:
            parts = []
            for row_site, row_split, subject, night, psg, hypnogram in rows:
                if row_site != site or row_split != split:
                    continue
                night_data = process_night(paths[psg], paths[hypnogram])
                n = len(night_data["y"])
                parts.append(
                    (
                        night_data["X"],
                        night_data["y"],
                        np.full(n, subject, dtype=np.int16),
                        np.full(n, night, dtype=np.int8),
                        night_data["epoch"],
                    )
                )
                info["subjects"].append(
                    {
                        "subject": subject,
                        "night": night,
                        "split": split,
                        "psg": psg,
                        "hypnogram": hypnogram,
                        "epochs_in_recording": night_data["n_epochs_recording"],
                        "kept_epoch_range": night_data["kept_range"],
                        "epochs_kept": n,
                        "epochs_dropped_unlabelled": night_data["dropped_unlabelled"],
                        "counts": class_counts(night_data["y"]),
                    }
                )
            arrays = {
                key: np.concatenate([part[k] for part in parts])
                for k, key in enumerate(["X", "y", "subject", "night", "epoch"])
            }
            path = site_dir / f"{split}.npz"
            np.savez_compressed(path, **arrays)

            counts = class_counts(arrays["y"])
            info["counts"][split] = {"n": len(arrays["y"]), **counts}
            info["fingerprints"][split] = {
                "X_sha256": array_sha256(arrays["X"]),
                "y_sha256": array_sha256(arrays["y"]),
            }
            found = (len(arrays["y"]), *counts.values())
            expected = EXPECTED_COUNTS[(site, split)]
            if found != expected:
                mismatches.append(f"{site} {split}: found {found}, expected {expected}")
            summary.append((site, split, len(parts), found, path.stat().st_size))

        with open(site_dir / "site_info.json", "w") as handle:
            json.dump(info, handle, indent=2)
            handle.write("\n")

    print(f"\nProcessed {len(rows)} nights in {time.time() - started:.1f} s")
    header = f"{'site':12s} {'split':5s} {'subj':>4s} {'epochs':>6s} " + " ".join(
        f"{name:>5s}" for name in CLASS_NAMES
    )
    print(header + f" {'MB':>6s}")
    for site, split, n_subjects, found, size in summary:
        counts = " ".join(f"{v:5d}" for v in found[1:])
        print(
            f"{site:12s} {split:5s} {n_subjects:4d} {found[0]:6d} {counts} "
            f"{size / 1e6:6.1f}"
        )
    total = np.sum([row[3] for row in summary], axis=0)
    total_mb = sum(row[4] for row in summary) / 1e6
    print(
        f"{'total':12s} {'':5s} {len(rows):4d} {total[0]:6d} "
        + " ".join(f"{v:5d}" for v in total[1:])
        + f" {total_mb:6.1f}"
    )
    print(f"Written to {args.out.resolve()}")

    if mismatches:
        print("\nEpoch counts differ from the expected values:")
        for line in mismatches:
            print("  " + line)
        if not args.no_count_check:
            return 1
    else:
        print("Epoch counts match the expected values for every site and split.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
