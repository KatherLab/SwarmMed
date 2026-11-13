#!/usr/bin/env python3
"""Prepare the PhysioNet/CinC Challenge 2016 heart sound recordings for this template.

A site runs this script before it imports data into SwarmMed. It reads the public
dataset (WAV recordings, labels and signal-quality flags), cuts every recording into
3-second log-mel segments and writes a few compact files per site:

    --layout local-test (default)    OUT/fl-client-k/{train,val,test}.npz + site_info.json
                                     for the k-th source in --sources (one folder per
                                     simulated site of a local-test network)
    --layout single --site-index k   OUT/{train,val,test}.npz + site_info.json for the
                                     k-th source only (one real site)
    --holdout c,d (optional)         OUT/holdout-training-c/test.npz + site_info.json with
                                     every recording of a source no client trains on

The six folders training-a ... training-f of the dataset are six independent source
databases (different groups, countries, stethoscopes and populations); each source is
one site. The train/val/test assignment of every recording is read from splits.csv,
shipped next to this script (grouped by subject or raw recording where the dataset
publishes them; see README.md). After writing, the script reloads every file, prints
the counts and compares them with the counts expected for the default options.

Dataset: Classification of Heart Sound Recordings: The PhysioNet/Computing in
Cardiology Challenge 2016, version 1.0.0, https://physionet.org/content/challenge-2016/1.0.0/
Licence: Open Data Commons Attribution License v1.0 (ODC-By 1.0).

Uses only numpy, scipy and the Python standard library.

Example:
    python prepare_data.py --raw-dir /data/physionet2016/raw --out /data/physionet2016/prepared/local-test
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

# The matrix products here are small; extra BLAS threads only add overhead (with the
# default of one thread per core the run took longer and used ~20x the CPU time).
# Set before numpy is imported; an explicit value in the environment wins.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import numpy as np  # noqa: E402
import scipy  # noqa: E402
from scipy.io import wavfile  # noqa: E402
from scipy.signal import butter, resample_poly, sosfiltfilt  # noqa: E402

# ---------------------------------------------------------------------------
# Dataset identity
# ---------------------------------------------------------------------------

DATASET = "PhysioNet/CinC Challenge 2016 heart sound recordings, version 1.0.0"
DATASET_URL = "https://physionet.org/content/challenge-2016/1.0.0/"
FILES_URL = "https://physionet.org/files/challenge-2016/1.0.0/"
LICENCE = "Open Data Commons Attribution License v1.0 (ODC-By 1.0)"

# Files fetched by --download: name -> (size in bytes, SHA-256).
DOWNLOADS = {
    "training.zip": (
        190_112_103,
        "e750fbfd0ca60288f44eb45e4065b356defb821f249baa28b8cb02b576ae6dcb",
    ),
    "annotations.zip": (
        10_542_695,
        "142e265049e33136f9d9da6886a88f0a2401be674988aa6a6cfaef7ff55a1f0b",
    ),
}

ALL_SOURCES = "abcdef"
SOURCE_DATABASE = {
    "a": "MITHSDB",
    "b": "AADHSDB",
    "c": "AUTHHSDB",
    "d": "UHAHSDB",
    "e": "DLUTHSDB",
    "f": "SUAHSDB",
}
SPLIT_UNIT = {
    "a": "recording (no subject IDs published)",
    "b": "subject (Online Appendix 'Subject ID')",
    "c": "recording (one recording per subject)",
    "d": "recording (no subject IDs published)",
    "e": "raw recording (Online Appendix '# Raw record' within class)",
    "f": "recording (no subject IDs published; 112 subjects, 114 recordings)",
}
SPLITS = ("train", "val", "test")

# The split file shipped with this template and its SHA-256. The same file can be
# rebuilt from the dataset's Online Appendix with --resplit (seed 2016).
DEFAULT_SPLITS_FILE = Path(__file__).resolve().parent / "splits.csv"
REFERENCE_SPLITS_SHA256 = (
    "d97b246c3f0bf61c3be81e7a5e6314688812f2d8747aa8b395c925f375b89df9"
)
DEFAULT_SPLIT_SEED = 2016

# Counts expected with the default options (shipped splits.csv, --max-segments 10,
# no --drop-unsure): source -> split -> (recordings, abnormal recordings,
# segments, abnormal segments). Measured with a reference implementation of this
# preprocessing on 11 Nov 2026.
EXPECTED_COUNTS = {
    "a": {
        "train": (247, 176, 2327, 1630),
        "val": (81, 58, 768, 547),
        "test": (81, 58, 781, 552),
    },
    "b": {
        "train": (296, 64, 592, 128),
        "val": (103, 24, 205, 47),
        "test": (91, 16, 182, 32),
    },
    "c": {"train": (19, 14, 183, 136), "val": (6, 5, 34, 29), "test": (6, 5, 57, 47)},
    "d": {"train": (33, 16, 155, 89), "val": (11, 6, 49, 35), "test": (11, 6, 39, 27)},
    "e": {
        "train": (1226, 109, 8126, 655),
        "val": (422, 37, 2777, 209),
        "test": (406, 37, 2681, 214),
    },
    "f": {
        "train": (68, 20, 677, 200),
        "val": (23, 7, 229, 69),
        "test": (23, 7, 229, 70),
    },
}

# ---------------------------------------------------------------------------
# Signal processing constants (all recordings in this dataset are 2000 Hz)
# ---------------------------------------------------------------------------

SR = 2000  # sample rate, Hz
HP_CUTOFF_HZ = 20.0  # 2nd-order Butterworth high-pass, applied forwards and backwards
WIN = 128  # 64 ms periodic Hann window
N_FFT = 256  # zero-padded FFT length -> 129 bins, 7.8125 Hz apart
HOP = 40  # 20 ms hop -> 50 frames per second
N_MELS = 32
FMIN, FMAX = 25.0, 800.0  # Hz, HTK mel scale, triangular filters with peak 1
LOG_EPS = 1e-6
SEG_FRAMES = 150  # 3.0 s segments
SEG_HOP_FRAMES = 150  # no overlap
DEFAULT_MAX_SEGMENTS = 10  # per recording, evenly spaced over the recording


def hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f, np.float64) / 700.0)


def mel_to_hz(m):
    return 700.0 * (10.0 ** (np.asarray(m, np.float64) / 2595.0) - 1.0)


def mel_filterbank() -> np.ndarray:
    """Triangular mel filters, shape (N_MELS, N_FFT // 2 + 1), peak 1, no area normalisation."""
    freqs = np.arange(N_FFT // 2 + 1) * SR / N_FFT
    edges = mel_to_hz(np.linspace(hz_to_mel(FMIN), hz_to_mel(FMAX), N_MELS + 2))
    fb = np.zeros((N_MELS, freqs.size))
    for i in range(N_MELS):
        lo, ce, hi = edges[i], edges[i + 1], edges[i + 2]
        fb[i] = np.maximum(
            0.0, np.minimum((freqs - lo) / (ce - lo), (hi - freqs) / (hi - ce))
        )
    return fb


FB = mel_filterbank()
WINDOW = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(WIN) / WIN)  # periodic Hann
SOS_HP = butter(2, HP_CUTOFF_HZ, btype="highpass", fs=SR, output="sos")


def read_wav(path: Path) -> np.ndarray:
    """Read a WAV file as float64 in [-1, 1) at SR Hz (first channel only)."""
    sr, x = wavfile.read(path)
    if x.ndim > 1:
        x = x[:, 0]
    x = x.astype(np.float64) / 32768.0 if x.dtype == np.int16 else x.astype(np.float64)
    if sr != SR:  # never happens for this dataset; kept for other 16-bit WAV sources
        g = np.gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g)
    return x


def log_mel(x: np.ndarray) -> np.ndarray:
    """Waveform -> log-mel spectrogram, shape (frames, N_MELS), float32."""
    x = sosfiltfilt(SOS_HP, x)  # removes DC offset and baseline drift
    x = (x - x.mean()) / (x.std() + 1e-8)  # per-recording amplitude normalisation
    n = 1 + (x.size - WIN) // HOP  # no centre padding
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * WINDOW, n=N_FFT, axis=1)) ** 2
    return np.log(spec @ FB.T + LOG_EPS).astype(np.float32)


def segment_starts(n_frames: int, max_segments: int) -> np.ndarray:
    """Frame indices at which the segments of a recording start."""
    if n_frames < SEG_FRAMES:
        return np.zeros(0, np.int64)
    s = np.arange(0, n_frames - SEG_FRAMES + 1, SEG_HOP_FRAMES, dtype=np.int64)
    if s.size > max_segments:  # keep an evenly spaced subset
        s = s[np.floor(np.linspace(0, s.size - 1, max_segments) + 0.5).astype(np.int64)]
    return s


# ---------------------------------------------------------------------------
# Raw data: download, extraction, labels, quality flags, splits
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, target: Path, size: int, sha256: str) -> None:
    """Download url to target unless a verified copy exists; abort on any mismatch."""
    if (
        target.exists()
        and target.stat().st_size == size
        and sha256_of(target) == sha256
    ):
        print(f"  {target.name}: already present and verified")
        return
    partial = target.with_name(target.name + ".part")
    print(f"  downloading {url} ({size / 1e6:.1f} MB)")
    digest = hashlib.sha256()
    with (
        urllib.request.urlopen(url, timeout=60) as response,
        open(partial, "wb") as out,
    ):  # noqa: S310 (fixed https URL)
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
            out.write(chunk)
    got_size = partial.stat().st_size
    if got_size != size or digest.hexdigest() != sha256:
        partial.unlink()
        sys.exit(
            f"ERROR: {target.name}: got {got_size} bytes, SHA-256 {digest.hexdigest()}; "
            f"expected {size} bytes, SHA-256 {sha256}. Nothing was extracted."
        )
    os.replace(partial, target)
    print(f"  {target.name}: size and SHA-256 verified")


def safe_extract(zip_path: Path, dest: Path) -> int:
    """Extract a zip into dest, refusing absolute or '..' member paths.

    Skips macOS metadata (__MACOSX/, .DS_Store) and members whose extracted copy
    already has the right size. Returns the number of files written.
    """
    dest_resolved = dest.resolve()
    written = 0
    with zipfile.ZipFile(zip_path) as archive:
        # Check every member name before writing anything.
        members = []
        for member in archive.infolist():
            name = member.filename
            parts = PurePosixPath(name).parts
            unsafe = (
                name.startswith("/") or ".." in parts or ":" in name or "\\" in name
            )
            if unsafe or not dest.joinpath(*parts).resolve().is_relative_to(
                dest_resolved
            ):
                sys.exit(
                    f"ERROR: unsafe member path {name!r} in {zip_path}; nothing extracted."
                )
            if parts and parts[0] != "__MACOSX" and parts[-1] != ".DS_Store":
                members.append((member, dest.joinpath(*parts)))
        for member, target in members:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists() and target.stat().st_size == member.file_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            written += 1
    return written


def download_raw(raw_dir: Path, files_url: str = FILES_URL) -> None:
    """Fetch training.zip and annotations.zip into raw_dir and extract them there."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name, (size, sha256) in DOWNLOADS.items():
        fetch(files_url + name, raw_dir / name, size, sha256)
    for name in DOWNLOADS:
        n = safe_extract(raw_dir / name, raw_dir)
        print(f"  {name}: extracted {n} new files into {raw_dir}")


def find_raw_root(raw_dir: Path, sources: str) -> Path:
    """Return the folder that holds training-x/ and annotations/.

    Accepts RAW itself (layout after --download) or RAW/extracted/.
    """
    for root in (raw_dir, raw_dir / "extracted"):
        if (
            all((root / f"training-{s}").is_dir() for s in sources)
            and (root / "annotations").is_dir()
        ):
            return root
    sys.exit(
        f"ERROR: {raw_dir} does not contain training-{{{','.join(sources)}}}/ and annotations/ "
        "(directly or in extracted/). Run with --download, or extract training.zip and "
        "annotations.zip from " + FILES_URL + " into it."
    )


def read_labels(raw_root: Path, source: str) -> dict[str, int]:
    """REFERENCE.csv (no header; record,label with 1 = abnormal, -1 = normal) -> {record: 0/1}."""
    labels = {}
    with open(raw_root / f"training-{source}" / "REFERENCE.csv", newline="") as handle:
        for row in csv.reader(handle):
            if row:
                labels[row[0].strip()] = 1 if int(row[1]) == 1 else 0
    return labels


def read_sqi(raw_root: Path, source: str, labels: dict[str, int]) -> dict[str, int]:
    """Signal-quality flag per record (1 usable, 0 'unsure') from the updated annotations.

    The file covers the 3,153 records of the Online Appendix; its label column must
    agree with REFERENCE.csv.
    """
    path = (
        raw_root
        / "annotations"
        / "updated"
        / f"training-{source}"
        / "REFERENCE_withSQI.csv"
    )
    sqi = {}
    with open(path, newline="") as handle:
        for row in csv.reader(handle):
            if not row:
                continue
            record, label, quality = row[0].strip(), int(row[1]), int(row[2])
            if record in labels and (1 if label == 1 else 0) != labels[record]:
                sys.exit(
                    f"ERROR: {path}: label of {record} disagrees with REFERENCE.csv"
                )
            sqi[record] = quality
    return sqi


def read_split_file(path: Path) -> dict[str, tuple[str, str]]:
    """splits.csv (record,source,split) -> {record: (source letter, split)}."""
    assignment = {}
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            source = row["source"].strip()
            split = row["split"].strip()
            if not source.startswith("training-") or split not in (*SPLITS, "excluded"):
                sys.exit(f"ERROR: {path}: unexpected row {row}")
            assignment[row["record"].strip()] = (source[-1], split)
    return assignment


def read_appendix(raw_root: Path) -> dict[str, dict[str, str]]:
    """Online Appendix of the training set (latin-1), keyed by challenge record name."""
    folder = raw_root / "annotations"
    for name in (
        "Online Appendix_training set.csv",
        "Online_Appendix_training_set.csv",
    ):
        if (folder / name).exists():
            with open(folder / name, encoding="latin-1", newline="") as handle:
                return {
                    row["Challenge record name"].strip(): row
                    for row in csv.DictReader(handle)
                }
    sys.exit(f"ERROR: no Online Appendix training-set CSV in {folder}")


def resplit(raw_root: Path, sources: str, seed: int) -> dict[str, tuple[str, str]]:
    """Rebuild the grouped, label-stratified 60/20/20 split per source.

    Group unit: the subject in training-b, the raw recording (class + '# Raw record')
    in training-e, the recording elsewhere. Records missing from the Online Appendix
    (87 normal training-e records) are excluded. With numpy 2.4 this reproduces the
    shipped splits.csv exactly; numpy does not promise identical random streams across
    versions, so the shipped file is the reference.
    """
    appendix = read_appendix(raw_root)
    assignment = {}
    for s in sources:
        labels = read_labels(raw_root, s)
        groups: dict[str, list[str]] = {}
        group_label: dict[str, int] = {}
        for record, label in labels.items():
            if record not in appendix:
                assignment[record] = (s, "excluded")
                continue
            if s == "b":
                gid = "b-subj-" + appendix[record]["Subject ID"].strip()
            elif s == "e":
                kind = "abn" if label == 1 else "nor"
                gid = f"e-{kind}-raw-" + appendix[record]["# Raw record"].strip()
            else:
                gid = record
            groups.setdefault(gid, []).append(record)
            if group_label.setdefault(gid, label) != label:
                sys.exit(f"ERROR: group {gid} mixes labels")
        for lab in (0, 1):
            ordered = sorted(
                g for g in groups if group_label[g] == lab
            )  # Python string sort
            perm = np.random.default_rng([seed, ALL_SOURCES.index(s), lab]).permutation(
                len(ordered)
            )
            n_test = n_val = int(np.floor(0.2 * len(ordered) + 0.5))
            for k, j in enumerate(perm):
                split = (
                    "test" if k < n_test else ("val" if k < n_test + n_val else "train")
                )
                for record in groups[ordered[j]]:
                    assignment[record] = (s, split)
    return assignment


# ---------------------------------------------------------------------------
# Feature extraction and output files
# ---------------------------------------------------------------------------


def build_arrays(
    raw_root, source, records, labels, sqi, max_segments
) -> dict[str, np.ndarray]:
    """Segments and per-recording arrays for one split of one source.

    records must be sorted by name; rec indices refer to that order.
    """
    xs, ys, recs, starts, durations = [], [], [], [], []
    for i, record in enumerate(records):
        wav = read_wav(raw_root / f"training-{source}" / f"{record}.wav")
        durations.append(wav.size / SR)
        mel = log_mel(wav)
        seg_starts = segment_starts(mel.shape[0], max_segments)
        if seg_starts.size == 0:
            sys.exit(
                f"ERROR: {record} is shorter than one segment ({wav.size / SR:.2f} s)"
            )
        for a in seg_starts:
            xs.append(mel[a : a + SEG_FRAMES].T)  # (N_MELS, SEG_FRAMES)
            ys.append(labels[record])
            recs.append(i)
            starts.append(a * HOP / SR)
    return {
        "x": np.stack(xs).astype(np.float16),
        "y": np.array(ys, dtype=np.int8),
        "rec": np.array(recs, dtype=np.int32),
        "seg_start_s": np.array(starts, dtype=np.float32),
        "rec_ids": np.array(records),
        "rec_y": np.array([labels[r] for r in records], dtype=np.int8),
        "rec_sqi": np.array([sqi[r] for r in records], dtype=np.int8),
        "rec_dur_s": np.array(durations, dtype=np.float32),
    }


def normalisation(x: np.ndarray) -> tuple[list[float], list[float]]:
    """Per mel band mean and population std over all segments and frames (float64)."""
    x64 = x.astype(np.float64)
    mean, std = x64.mean(axis=(0, 2)), x64.std(axis=(0, 2))
    if not np.all(np.isfinite(mean)) or not np.all(std > 0):
        sys.exit(
            "ERROR: normalisation statistics are not finite or a band has zero variance"
        )
    return [round(float(v), 6) for v in mean], [round(float(v), 6) for v in std]


def write_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
    """Uncompressed .npz, written to a temporary name first and then renamed."""
    partial = path.with_name(path.name + ".part")
    with open(partial, "wb") as handle:
        np.savez(handle, **arrays)
    os.replace(partial, path)


def write_json(path: Path, data: dict) -> None:
    partial = path.with_name(path.name + ".part")
    with open(partial, "w") as handle:
        json.dump(data, handle, indent=1)
        handle.write("\n")
    os.replace(partial, path)


def preprocessing_info(max_segments: int, drop_unsure: bool) -> dict:
    return {
        "sample_rate_hz": SR,
        "highpass": f"Butterworth order 2, {HP_CUTOFF_HZ:g} Hz, sosfiltfilt",
        "amplitude": "per recording: subtract mean, divide by std",
        "stft": {
            "window": "periodic Hann",
            "win_length": WIN,
            "n_fft": N_FFT,
            "hop_length": HOP,
            "center": False,
        },
        "mel": {
            "n_mels": N_MELS,
            "fmin_hz": FMIN,
            "fmax_hz": FMAX,
            "scale": "HTK",
            "norm": None,
        },
        "log": "natural log of (mel power + 1e-6)",
        "segment_frames": SEG_FRAMES,
        "segment_hop_frames": SEG_HOP_FRAMES,
        "max_segments_per_recording": max_segments,
        "drop_unsure": drop_unsure,
        "stored_dtype": "float16",
        "x_shape": "(segments, n_mels, frames)",
        "x_normalised": False,
    }


def write_site(
    folder,
    site,
    layout,
    source,
    split_records,
    raw_root,
    labels,
    sqi,
    args,
    split_source,
) -> dict:
    """Process and write one site's split files and site_info.json; return the info dict."""
    folder.mkdir(parents=True, exist_ok=True)
    arrays = {
        split: build_arrays(raw_root, source, records, labels, sqi, args.max_segments)
        for split, records in split_records.items()
    }
    # Train sites are normalised with their own training split; a held-out source
    # (only test.npz) with its own test split, which uses no labels.
    stats_split = "train" if "train" in arrays else "test"
    mean, std = normalisation(arrays[stats_split]["x"])
    info = {
        "dataset": DATASET,
        "dataset_url": DATASET_URL,
        "licence": LICENCE,
        "site": site,
        "layout": layout,
        "source": f"training-{source}",
        "source_database": SOURCE_DATABASE[source],
        "task": "recording-level binary classification (segments inherit the recording label)",
        "label_map": {
            "0": "normal (REFERENCE.csv value -1)",
            "1": "abnormal (REFERENCE.csv value 1)",
        },
        "split_unit": SPLIT_UNIT[source],
        "split_seed": args.seed,
        "split_source": split_source,
        "preprocessing": preprocessing_info(args.max_segments, args.drop_unsure),
        "software": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "splits": {},
    }
    for split, arr in arrays.items():
        path = folder / f"{split}.npz"
        write_npz(path, arr)
        info["splits"][split] = {
            "recordings": int(arr["rec_ids"].size),
            "recordings_abnormal": int(arr["rec_y"].sum()),
            "segments": int(arr["y"].size),
            "segments_abnormal": int(arr["y"].sum()),
            "bytes": path.stat().st_size,
            "sha256": sha256_of(path),
        }
    info["normalisation"] = {
        "computed_on": f"{stats_split}.npz x, all segments and frames (float64)",
        "mean": mean,
        "std": std,
    }
    write_json(folder / "site_info.json", info)
    return info


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

NPZ_KEYS = {
    "x": np.float16,
    "y": np.int8,
    "rec": np.int32,
    "seg_start_s": np.float32,
    "rec_ids": np.str_,
    "rec_y": np.int8,
    "rec_sqi": np.int8,
    "rec_dur_s": np.float32,
}


def check_written(folder: Path, info: dict) -> list[str]:
    """Reload a site's files and check keys, dtypes, shapes and counts against site_info."""
    problems = []
    for split, counts in info["splits"].items():
        with np.load(folder / f"{split}.npz", allow_pickle=False) as z:
            arrays = {key: z[key] for key in z.files}
        if set(arrays) != set(NPZ_KEYS):
            problems.append(f"{folder.name}/{split}.npz: keys {sorted(arrays)}")
            continue
        for key, dtype in NPZ_KEYS.items():
            if not np.issubdtype(arrays[key].dtype, dtype):
                problems.append(
                    f"{folder.name}/{split}.npz: {key} has dtype {arrays[key].dtype}"
                )
        x, y, rec, rec_y = arrays["x"], arrays["y"], arrays["rec"], arrays["rec_y"]
        n_rec = arrays["rec_ids"].size
        if x.shape[1:] != (N_MELS, SEG_FRAMES) or not np.all(np.isfinite(x)):
            problems.append(
                f"{folder.name}/{split}.npz: x has shape {x.shape} or non-finite values"
            )
        if not (y.size == x.shape[0] == rec.size == arrays["seg_start_s"].size):
            problems.append(
                f"{folder.name}/{split}.npz: segment arrays differ in length"
            )
        if rec.min() < 0 or rec.max() >= n_rec or np.unique(rec).size != n_rec:
            problems.append(
                f"{folder.name}/{split}.npz: rec does not index every recording"
            )
        elif not np.array_equal(y, rec_y[rec]):
            problems.append(
                f"{folder.name}/{split}.npz: segment labels differ from recording labels"
            )
        got = (n_rec, int(rec_y.sum()), int(y.size), int(y.sum()))
        want = (
            counts["recordings"],
            counts["recordings_abnormal"],
            counts["segments"],
            counts["segments_abnormal"],
        )
        if got != want:
            problems.append(
                f"{folder.name}/{split}.npz: counts {got} differ from site_info.json {want}"
            )
    return problems


def compare_with_expected(source: str, info: dict, check_segments: bool) -> list[str]:
    """Compare a site's counts with EXPECTED_COUNTS (default options only)."""
    problems = []
    for split, counts in info["splits"].items():
        got = (
            counts["recordings"],
            counts["recordings_abnormal"],
            counts["segments"],
            counts["segments_abnormal"],
        )
        if split == "test" and info["layout"] == "holdout":
            want = tuple(
                sum(EXPECTED_COUNTS[source][sp][i] for sp in SPLITS) for i in range(4)
            )
        else:
            want = EXPECTED_COUNTS[source][split]
        n = 4 if check_segments else 2
        if got[:n] != want[:n]:
            problems.append(
                f"training-{source} {split}: got {got[:n]}, expected {want[:n]}"
            )
    return problems


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def parse_sources(text: str, option: str) -> str:
    letters = "".join(
        part.strip().removeprefix("training-")
        for part in text.split(",")
        if part.strip()
    )
    if (
        not letters
        or any(c not in ALL_SOURCES for c in letters)
        or len(set(letters)) != len(letters)
    ):
        raise argparse.ArgumentTypeError(
            f"{option}: give distinct letters from a-f, e.g. a,b,e,f"
        )
    return letters


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert the PhysioNet/CinC 2016 heart sound recordings into this template's data layout."
    )
    parser.add_argument(
        "--raw-dir",
        required=True,
        type=Path,
        help="folder with training-a ... training-f/ and annotations/ (or extracted/ holding them); "
        "with --download the zips are fetched and extracted here",
    )
    parser.add_argument(
        "--out",
        "--out-dir",
        dest="out",
        required=True,
        type=Path,
        help="output folder (outside any repository)",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="fetch training.zip and annotations.zip from PhysioNet, verify size and SHA-256, extract",
    )
    parser.add_argument(
        "--layout",
        choices=("local-test", "single"),
        default="local-test",
        help="local-test: OUT/fl-client-k/ for the k-th source; single: one site's files at OUT/",
    )
    parser.add_argument(
        "--sources",
        default="a,b,e,f",
        type=lambda t: parse_sources(t, "--sources"),
        help="source databases in site order (default a,b,e,f)",
    )
    parser.add_argument(
        "--site-index",
        type=int,
        help="with --layout single: 1-based index into --sources",
    )
    parser.add_argument(
        "--holdout",
        default="",
        type=lambda t: parse_sources(t, "--holdout") if t else "",
        help="sources written as OUT/holdout-training-x/test.npz (all recordings), e.g. c,d",
    )
    parser.add_argument(
        "--splits",
        type=Path,
        default=DEFAULT_SPLITS_FILE,
        help="split file (default: splits.csv next to this script)",
    )
    parser.add_argument(
        "--resplit",
        action="store_true",
        help="rebuild the splits from the Online Appendix instead of reading --splits",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SPLIT_SEED,
        help="seed for --resplit only (default 2016)",
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=DEFAULT_MAX_SEGMENTS,
        help="maximum 3-s segments per recording (default 10)",
    )
    parser.add_argument(
        "--drop-unsure",
        action="store_true",
        help="leave out recordings flagged as too noisy (sqi = 0)",
    )
    args = parser.parse_args(argv)
    if args.layout == "single" and not (
        args.site_index and 1 <= args.site_index <= len(args.sources)
    ):
        parser.error(
            f"--layout single needs --site-index between 1 and {len(args.sources)}"
        )
    if args.layout == "local-test" and args.site_index is not None:
        parser.error("--site-index is only used with --layout single")
    if set(args.holdout) & set(args.sources):
        parser.error("--holdout sources must not also be in --sources")
    if args.max_segments < 1:
        parser.error("--max-segments must be at least 1")
    if not args.resplit and args.seed != DEFAULT_SPLIT_SEED:
        parser.error("--seed only applies with --resplit")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.download:
        print(f"Downloading into {args.raw_dir}")
        download_raw(args.raw_dir)
    site_sources = (
        args.sources
        if args.layout == "local-test"
        else args.sources[args.site_index - 1]
    )
    needed = site_sources + args.holdout
    raw_root = find_raw_root(args.raw_dir, needed)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    # Split assignment: the shipped file (checked against its fingerprint) or a rebuild.
    reference_split = False
    if args.resplit:
        assignment = resplit(raw_root, needed, args.seed)
        split_source = f"rebuilt from the Online Appendix (--resplit, seed {args.seed})"
        if args.splits.exists():
            shipped = read_split_file(args.splits)
            diff = sorted(r for r, v in assignment.items() if shipped.get(r) != v)
            print(
                f"Resplit: {len(diff)} of {len(assignment)} records differ from {args.splits}"
            )
            reference_split = (
                not diff
                and args.seed == DEFAULT_SPLIT_SEED
                and sha256_of(args.splits) == REFERENCE_SPLITS_SHA256
            )
    else:
        digest = sha256_of(args.splits)
        reference_split = digest == REFERENCE_SPLITS_SHA256
        if not reference_split:
            print(
                f"Note: {args.splits} is not the shipped split file (SHA-256 {digest}); counts are not compared."
            )
        assignment = read_split_file(args.splits)
        split_source = f"{args.splits.name} (SHA-256 {digest})"

    jobs = []  # (folder, site name, layout, source, splits to write)
    if args.layout == "local-test":
        for k, s in enumerate(args.sources, start=1):
            jobs.append(
                (out / f"fl-client-{k}", f"fl-client-{k}", "local-test", s, SPLITS)
            )
    else:
        jobs.append((out, None, "single", site_sources, SPLITS))
    for s in args.holdout:
        jobs.append(
            (
                out / f"holdout-training-{s}",
                f"holdout-training-{s}",
                "holdout",
                s,
                ("test",),
            )
        )

    print(f"Raw data: {raw_root}")
    print(f"Splits:   {split_source}")
    print(f"Output:   {out} (layout {args.layout})")
    header = f"{'folder':<22} {'source':<11} {'split':<5} {'recordings (abn)':>17} {'segments (abn)':>15} {'MB':>7}"
    print(header)
    print("-" * len(header))

    problems, total_bytes, infos = [], 0, []
    for folder, site, layout, s, splits in jobs:
        labels = read_labels(raw_root, s)
        sqi = read_sqi(raw_root, s, labels)
        rows = {r: v for r, v in assignment.items() if v[0] == s}
        missing = sorted(set(labels) - set(rows))
        unknown = sorted(set(rows) - set(labels))
        if missing or unknown:
            sys.exit(
                f"ERROR: training-{s}: {len(missing)} records missing from the split file, "
                f"{len(unknown)} split rows not in REFERENCE.csv (e.g. {(missing + unknown)[:3]})"
            )
        split_records = {}
        for split in splits:
            wanted = SPLITS if layout == "holdout" else (split,)
            records = sorted(r for r, (_, sp) in rows.items() if sp in wanted)
            no_sqi = [r for r in records if r not in sqi]
            if no_sqi:
                sys.exit(f"ERROR: training-{s}: no quality flag for {no_sqi[:3]}")
            if args.drop_unsure:
                records = [r for r in records if sqi[r] == 1]
            if not records:
                sys.exit(f"ERROR: training-{s} {split}: no recordings left")
            split_records[split] = records
        info = write_site(
            folder,
            site,
            layout,
            s,
            split_records,
            raw_root,
            labels,
            sqi,
            args,
            split_source,
        )
        infos.append((folder, s, info))
        problems += check_written(folder, info)
        for split, c in info["splits"].items():
            total_bytes += c["bytes"]
            label = folder.name if layout != "single" else "."
            print(
                f"{label:<22} training-{s} {split:<5} {c['recordings']:>10} ({c['recordings_abnormal']:>4}) "
                f"{c['segments']:>8} ({c['segments_abnormal']:>4}) {c['bytes'] / 1e6:>7.1f}"
            )
    print(
        f"Total: {total_bytes / 1e6:.1f} MB in {sum(len(i['splits']) for _, _, i in infos)} .npz files"
    )

    # Compare with the expected counts when the options allow it.
    if reference_split and not args.drop_unsure:
        check_segments = args.max_segments == DEFAULT_MAX_SEGMENTS
        for _, s, info in infos:
            problems += compare_with_expected(s, info, check_segments)
        what = "recording and segment counts" if check_segments else "recording counts"
        if not problems:
            print(
                f"Verified: {what} match the expected counts for the default options."
            )
    else:
        print(
            "Counts not compared: expected counts exist only for the shipped splits without --drop-unsure."
        )

    if args.layout == "local-test":
        written = {folder.name for folder, *_ in jobs}
        stale = sorted(
            p.name
            for p in out.iterdir()
            if p.is_dir()
            and p.name.startswith(("fl-client-", "holdout-"))
            and p.name not in written
        )
        if stale:
            print(
                f"Warning: {out} also holds folders this run did not write: {', '.join(stale)}. "
                "Remove them before importing the data."
            )

    if problems:
        print("ERROR: verification failed:")
        for p in problems:
            print("  " + p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
