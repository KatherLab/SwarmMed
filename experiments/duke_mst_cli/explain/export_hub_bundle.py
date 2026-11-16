#!/usr/bin/env python3
"""Convert run_explainability.py output into a SwarmMedHub explanation bundle.

The bundle (``cases.json`` plus PNG files, format ``swarmmed.explanations/1``)
can be imported into the Hub with::

    swarmed results explain-import --project P --job FLARE_JOB_UUID --dir BUNDLE

Only the selected cases' per-method PNGs, the two composite figures and derived
metadata are copied. The attribution arrays (``attribution_maps/*.npy``), the
per-cohort prediction tables, ``run.log`` and ``report.md`` are never copied.
The input stays untouched.

Needs only the standard library and the ``swarmmed_explain`` package of this
repository (found next to the ``experiments`` folder when it is not installed).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

try:
    import swarmmed_explain  # noqa: F401
except ImportError:  # running from a checkout: the package is at the repository root
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from swarmmed_explain.bundle import (  # noqa: E402
    MAX_CASES,
    MAX_PNG_BYTES,
    PNG_SIGNATURE,
    BundleError,
    BundleWriter,
    check_case_id,
    describe,
)

OUTCOME_ORDER = ("TP", "FP", "FN", "TN")
DEFAULT_CLASS_NAMES = ("no lesion", "benign", "malignant")
REQUIRED_COLUMNS = ("cohort", "outcome", "uid", "target", "pred", "prob_malignant")
# (bundle method name, file suffix in figures/, column of selected_cases.csv, title)
PANELS = (
    ("raw", "raw", "raw_png", "Raw slices"),
    ("gradcam", "gradcam", "gradcam_png", "Grad-CAM"),
    ("gradcam++", "gradcampp", "gradcampp_png", "Grad-CAM++"),
    ("oca", "oca", "oca_png", "Occlusion (OCA)"),
)
SUMMARIES = (
    ("figures/figure_main.png", "True and false positives"),
    ("figures/figure_extended.png", "True and false positives and negatives"),
)
NOTES = (
    "Grad-CAM, Grad-CAM++ and occlusion explain the malignant class (class 2) for "
    "every case, whatever the prediction. Each map is scaled to [0, 1] per case, so "
    "brightness cannot be compared between cases. The composite figures may have "
    "been redrawn from recomputed arrays after the run (render_from_npy.py) while "
    "the per-method panels were not, so the two can disagree."
)


class ConversionError(ValueError):
    """The input folder cannot be converted."""


def _read_png(input_dir: Path, relative: str) -> bytes | None:
    """Read a PNG below ``input_dir``; None if it does not exist.

    The path must be relative, inside the folder and free of symbolic links,
    and the file must be a regular file with a PNG signature.
    """
    if not relative:
        return None
    rel = PurePosixPath(relative.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ConversionError(f"{relative!r} is not a path inside the input folder")
    current = input_dir
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ConversionError(f"{relative!r} is a symbolic link")
    if not current.exists():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(current, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConversionError(f"{relative!r} is not a regular file")
        if info.st_size > MAX_PNG_BYTES:
            raise ConversionError(f"{relative!r} is larger than {MAX_PNG_BYTES} bytes")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(MAX_PNG_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if not data.startswith(PNG_SIGNATURE):
        raise ConversionError(f"{relative!r} is not a PNG file")
    return data


def _read_rows(input_dir: Path) -> list[dict[str, str]]:
    path = input_dir / "selected_cases.csv"
    if path.is_symlink() or not path.is_file():
        raise ConversionError(
            f"{path} is missing (is --input a run_explainability output?)"
        )
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [
            name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or [])
        ]
        if missing:
            raise ConversionError(
                f"selected_cases.csv lacks the columns {', '.join(missing)}"
            )
        rows = list(reader)
    for number, row in enumerate(rows, start=2):
        if row["outcome"] not in OUTCOME_ORDER:
            raise ConversionError(
                f"selected_cases.csv line {number}: unknown outcome {row['outcome']!r}"
            )
        try:
            float(row["prob_malignant"])
            int(row["target"])
            int(row["pred"])
        except ValueError as exc:
            raise ConversionError(f"selected_cases.csv line {number}: {exc}") from None
    return rows


def order_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Order rows as strips_to_pdf.py does.

    Outcome TP, FP, FN, TN; then cohort; then the probability of malignancy,
    descending for TP and FP and ascending for FN and TN.
    """

    def key(row):
        prob = float(row["prob_malignant"])
        return (
            OUTCOME_ORDER.index(row["outcome"]),
            row["cohort"],
            -prob if row["outcome"] in ("TP", "FP") else prob,
        )

    return sorted(rows, key=key)


def _metrics(input_dir: Path) -> dict[str, float]:
    path = input_dir / "metrics_check.json"
    if path.is_symlink() or not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    metrics: dict[str, float] = {}
    for alias, values in sorted(data.items()):
        if not isinstance(values, dict):
            continue
        measured = values.get("measured")
        if isinstance(measured, (int, float)):
            metrics[f"auroc_{alias}"] = float(measured)
        published = values.get("published")
        if isinstance(published, (int, float)):
            metrics[f"auroc_{alias}_published"] = float(published)
    return metrics


def _class_name(names: tuple[str, ...], index: str) -> str:
    position = int(index)
    if 0 <= position < len(names):
        return names[position]
    return str(position)


def convert(
    input_dir: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    max_cases: int = MAX_CASES,
    class_names: tuple[str, ...] = DEFAULT_CLASS_NAMES,
    positive_class: int = 2,
    summaries: bool = True,
) -> tuple[dict, list[str]]:
    """Write the bundle and return its description and any warnings.

    Raises:
        ConversionError: If the input folder cannot be converted.
        BundleError: If a value breaks the bundle rules (for example a case ID).
    """
    source = Path(input_dir).resolve()
    if not source.is_dir():
        raise ConversionError(f"{input_dir} is not a folder")
    if not 1 <= max_cases <= MAX_CASES:
        raise ConversionError(f"--max-cases must be between 1 and {MAX_CASES}")
    warnings: list[str] = []
    rows = order_rows(_read_rows(source))
    if len(rows) > max_cases:
        warnings.append(
            f"kept the first {max_cases} of {len(rows)} cases "
            "(order: outcome, cohort, probability)"
        )
        rows = rows[:max_cases]

    positive = _class_name(class_names, str(positive_class))
    writer = BundleWriter(out_dir)
    methods_used: list[str] = []
    for row in rows:
        case_id = check_case_id(row["uid"])
        writer.add_case(
            case_id,
            label=_class_name(class_names, row["target"]),
            prediction=_class_name(class_names, row["pred"]),
            score=float(row["prob_malignant"]),
            target=positive,
            outcome=row["outcome"],
            details={"cohort": row["cohort"]},
        )
        safe_uid = row["uid"].replace("/", "_")
        for method, suffix, column, title in PANELS:
            png = _read_png(source, f"figures/{row['cohort']}_{safe_uid}_{suffix}.png")
            if png is None:
                png = _read_png(source, row.get(column) or "")
            if png is None:
                if row.get(column):
                    warnings.append(
                        f"case {len(writer.case_ids)}: no {method} PNG found"
                    )
                continue
            writer.add_panel_png(case_id, method, png, title=title)
            if method not in methods_used:
                methods_used.append(method)

    if summaries:
        for relative, title in SUMMARIES:
            png = _read_png(source, relative)
            if png is None:
                warnings.append(f"{relative} not found")
                continue
            writer.add_summary_png(title, png)

    stamp = datetime.fromtimestamp(
        (source / "selected_cases.csv").stat().st_mtime, tz=UTC
    )
    writer.set_info(
        methods=methods_used,
        target=f"{positive} (class {positive_class} of {', '.join(class_names)})",
        notes=(
            f"{NOTES} Converted from run_explainability.py output written "
            f"{stamp:%Y-%m-%d %H:%M} UTC."
        ),
        metrics=_metrics(source),
    )
    return writer.finish(), warnings


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--input", required=True, help="run_explainability.py --output-dir"
    )
    parser.add_argument("--out", required=True, help="new or empty bundle folder")
    parser.add_argument("--max-cases", type=int, default=MAX_CASES)
    parser.add_argument(
        "--class-names",
        default=",".join(DEFAULT_CLASS_NAMES),
        help="comma-separated class names by index",
    )
    parser.add_argument("--positive-class", type=int, default=2)
    parser.add_argument(
        "--no-summaries", action="store_true", help="leave out the composite figures"
    )
    args = parser.parse_args(argv)
    names = tuple(name.strip() for name in args.class_names.split(",") if name.strip())
    try:
        data, warnings = convert(
            args.input,
            args.out,
            max_cases=args.max_cases,
            class_names=names,
            positive_class=args.positive_class,
            summaries=not args.no_summaries,
        )
    except (ConversionError, BundleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Wrote {args.out}: {describe(data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
