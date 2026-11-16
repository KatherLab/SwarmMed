"""Explanation bundles: schema version 1, a writer, a validator and a safe copy.

A bundle is one flat directory::

    cases.json            cases, their panels, run-level summaries and run information
    case_01_panel_1.png   panel 1 of the first case (cases 01 to 24, panels 1 to 4)
    summary_1.png         run-level figure 1 (1 to 4)

The sandbox helper writes this format, the CLI exports and imports it, and converters
for other explanation scripts produce it. File names are fixed by position, so no name
in a bundle is chosen by the script that wrote it and no entry can point outside the
directory.

Only the standard library is imported at module level; Pillow is imported when a PNG is
re-encoded. Nothing here imports Django, torch or matplotlib, so the Hub can use this
module to check bundles it receives.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import os
import re
import stat
import struct
import unicodedata
from collections.abc import Mapping
from typing import Any

SCHEMA = "swarmmed.explanations/1"
CASES_FILE = "cases.json"

MAX_CASES = 24
MAX_PANELS_PER_CASE = 4
MAX_SUMMARIES = 4
MAX_FILES = 1 + MAX_CASES * MAX_PANELS_PER_CASE + MAX_SUMMARIES
MAX_CASES_JSON_BYTES = 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_PNG_PIXELS = 40_000_000
MAX_PNG_SIDE = 4096
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_PIXELS = 10 * MAX_PNG_PIXELS

MAX_LABEL_CHARS = 64
MAX_METHOD_CHARS = 64
MAX_TITLE_CHARS = 255
MAX_INFO_TARGET_CHARS = 255
MAX_NOTES_CHARS = 4000
MAX_INFO_METHODS = 16
MAX_DETAILS_KEYS = 16
MAX_DETAILS_TEXT_CHARS = 256
MAX_DETAILS_BYTES = 4096
MAX_METRICS = 32
MAX_ABS_INT = 2**63 - 1

OUTCOMES = ("TP", "FP", "FN", "TN", "correct", "incorrect")
IDENTIFIER_WORDS = (
    "name",
    "patient",
    "birth",
    "dob",
    "mrn",
    "address",
    "phone",
    "email",
    "ssn",
    "insurance",
    "accession",
)

CASE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
KEY_PATTERN = r"[A-Za-z0-9_.-]{1,64}"
PANEL_FILE_PATTERN = r"case_(0[1-9]|1[0-9]|2[0-4])_panel_([1-4])\.png"
SUMMARY_FILE_PATTERN = r"summary_([1-4])\.png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_CASE_ID_RE = re.compile(CASE_ID_PATTERN)
_KEY_RE = re.compile(KEY_PATTERN)
_PANEL_FILE_RE = re.compile(PANEL_FILE_PATTERN)
_SUMMARY_FILE_RE = re.compile(SUMMARY_FILE_PATTERN)

_TOP_KEYS = ("schema", "complete", "info", "cases", "summaries")
_INFO_KEYS = ("methods", "target", "notes", "metrics")
_CASE_KEYS = (
    "case_id",
    "label",
    "prediction",
    "score",
    "target",
    "outcome",
    "details",
    "panels",
)
_PANEL_KEYS = ("method", "title", "file")
_SUMMARY_KEYS = ("title", "file")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class BundleError(ValueError):
    """A bundle, or a value written to one, breaks the schema or a limit."""


# --------------------------------------------------------------------------- names


def panel_file_name(case_position: int, panel_position: int) -> str:
    """Return the fixed file name of a case panel.

    Args:
        case_position: Position of the case in ``cases``, starting at 1.
        panel_position: Position of the panel within the case, starting at 1.

    Returns:
        A name such as ``case_03_panel_2.png``.

    Raises:
        BundleError: If a position is outside the limits.
    """
    if not 1 <= case_position <= MAX_CASES:
        raise BundleError(f"case position {case_position} is outside 1..{MAX_CASES}")
    if not 1 <= panel_position <= MAX_PANELS_PER_CASE:
        raise BundleError(
            f"panel position {panel_position} is outside 1..{MAX_PANELS_PER_CASE}"
        )
    return f"case_{case_position:02d}_panel_{panel_position}.png"


def summary_file_name(position: int) -> str:
    """Return the fixed file name of a run-level summary figure.

    Args:
        position: Position of the summary, starting at 1.

    Returns:
        A name such as ``summary_1.png``.

    Raises:
        BundleError: If the position is outside the limits.
    """
    if not 1 <= position <= MAX_SUMMARIES:
        raise BundleError(f"summary position {position} is outside 1..{MAX_SUMMARIES}")
    return f"summary_{position}.png"


def is_bundle_file_name(name: str) -> bool:
    """Return True for ``cases.json`` and the fixed PNG names a bundle may hold."""
    return (
        name == CASES_FILE
        or _PANEL_FILE_RE.fullmatch(name) is not None
        or _SUMMARY_FILE_RE.fullmatch(name) is not None
    )


def bundle_file_names(data: Mapping[str, Any]) -> list[str]:
    """Return the PNG names a validated ``cases.json`` object refers to, in order."""
    names = [panel["file"] for case in data["cases"] for panel in case["panels"]]
    names.extend(summary["file"] for summary in data["summaries"])
    return names


# --------------------------------------------------------------------------- values


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _plain(value: Any) -> Any:
    """Return numpy and torch scalars as Python scalars; other values as they are."""
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    item = getattr(value, "item", None)
    shape = getattr(value, "shape", None)
    if callable(item) and shape is not None and tuple(shape) == ():
        return _plain(item())
    return value


def _check_text(
    value: Any,
    field: str,
    max_chars: int,
    *,
    required: bool = False,
    multiline: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise BundleError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise BundleError(f"{field} must be a string, not {type(value).__name__}")
    value = str(value)
    if required and not value.strip():
        raise BundleError(f"{field} must not be empty")
    if len(value) > max_chars:
        raise BundleError(
            f"{field} has {len(value)} characters; the limit is {max_chars}"
        )
    for char in value:
        if multiline and char in "\n\t":
            continue
        category = unicodedata.category(char)
        if category in ("Cc", "Cs"):
            raise BundleError(f"{field} contains a control character")
        # Format characters (bidirectional overrides, zero-width characters) can
        # make a label, outcome or title display differently from its value.
        if category == "Cf" and not multiline:
            raise BundleError(
                f"{field} contains an invisible formatting character "
                f"(U+{ord(char):04X})"
            )
        # Line and paragraph separators break a single-line field.
        if category in ("Zl", "Zp") and not multiline:
            raise BundleError(
                f"{field} contains a line or paragraph separator (U+{ord(char):04X})"
            )
    return value


def _check_number(value: Any, field: str) -> int | float | None:
    value = _plain(value)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BundleError(f"{field} must be a number, not {type(value).__name__}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BundleError(f"{field} must be finite, not {value}")
        return value
    if abs(value) > MAX_ABS_INT:
        raise BundleError(f"{field} is outside the 64-bit integer range")
    return value


def _check_key(key: Any, field: str) -> str:
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        raise BundleError(
            f"{field} key {_short(key)} must match {KEY_PATTERN} "
            "(letters, digits, '_', '.', '-', at most 64 characters)"
        )
    return key


def _only_keys(
    mapping: Mapping[str, Any], allowed: tuple[str, ...], where: str
) -> None:
    unknown = [key for key in mapping if key not in allowed]
    if unknown:
        listed = ", ".join(_short(key, 40) for key in unknown[:3])
        raise BundleError(
            f"{where} has unknown keys: {listed}; allowed: {', '.join(allowed)}"
        )


def check_case_id(value: Any) -> str:
    """Check a case ID and return it.

    Args:
        value: The site's pseudonymous case ID.

    Returns:
        The case ID.

    Raises:
        BundleError: If it does not match ``CASE_ID_PATTERN``.
    """
    value = _plain(value)
    if not isinstance(value, str) or _CASE_ID_RE.fullmatch(value) is None:
        raise BundleError(
            f"case_id {_short(value)} must match {CASE_ID_PATTERN}: letters, "
            "digits, '.', '_', ':', '-', at most 128 characters, starting with a "
            "letter or digit"
        )
    return value


def check_outcome(value: Any) -> str | None:
    """Check an outcome value (one of ``OUTCOMES`` or None) and return it.

    Raises:
        BundleError: If the value is not allowed.
    """
    value = _plain(value)
    if value is None:
        return None
    if not isinstance(value, str) or value not in OUTCOMES:
        raise BundleError(
            f"outcome must be one of {', '.join(OUTCOMES)} or None, not {_short(value)}"
        )
    return value


def check_details(value: Any) -> dict[str, Any]:
    """Check a case's ``details`` and return a plain copy.

    ``details`` is a flat dict of at most 16 keys. Keys match ``KEY_PATTERN`` and must
    not contain any word of ``IDENTIFIER_WORDS`` (case-insensitive). Values are strings
    of at most 256 characters, finite numbers, booleans or None. The JSON form is at
    most 4 KiB.

    Args:
        value: The details mapping, or None.

    Returns:
        The details as a new dict with plain Python values.

    Raises:
        BundleError: If a rule is broken.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BundleError(f"details must be a flat dict, not {type(value).__name__}")
    if len(value) > MAX_DETAILS_KEYS:
        raise BundleError(
            f"details has {len(value)} keys; the limit is {MAX_DETAILS_KEYS}"
        )
    result: dict[str, Any] = {}
    for key, raw in value.items():
        _check_key(key, "details")
        lowered = key.lower()
        for word in IDENTIFIER_WORDS:
            if word in lowered:
                raise BundleError(
                    f"details key {key!r} contains {word!r} and looks like a direct "
                    "identifier; details must not hold names, birth dates, record "
                    "numbers or contact data (rename the key if it is not one)"
                )
        item = _plain(raw)
        field = f"details[{key!r}]"
        if item is None or isinstance(item, bool):
            result[key] = item
        elif isinstance(item, int | float):
            result[key] = _check_number(item, field)
        elif isinstance(item, str):
            result[key] = _check_text(item, field, MAX_DETAILS_TEXT_CHARS)
        else:
            raise BundleError(
                f"{field} must be a string, number, boolean or None (details are "
                f"flat), not {type(item).__name__}"
            )
    size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if size > MAX_DETAILS_BYTES:
        raise BundleError(
            f"details take {size} bytes as JSON; the limit is {MAX_DETAILS_BYTES}"
        )
    return result


def check_metrics(value: Any) -> dict[str, Any]:
    """Check run-level metrics (a flat dict of finite numbers or None); return a copy.

    Raises:
        BundleError: If a rule is broken.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BundleError(f"metrics must be a flat dict, not {type(value).__name__}")
    if len(value) > MAX_METRICS:
        raise BundleError(f"metrics has {len(value)} keys; the limit is {MAX_METRICS}")
    result: dict[str, Any] = {}
    for key, raw in value.items():
        _check_key(key, "metrics")
        result[key] = _check_number(raw, f"metrics[{key!r}]")
    return result


def coerce_label(value: Any, field: str) -> str | None:
    """Turn a label, prediction or target into the stored string form.

    Numbers and booleans (also numpy and torch scalars) become ``str(value)``.

    Args:
        value: A string, number, boolean or None.
        field: The field name used in error messages.

    Returns:
        The string, at most 64 characters, or None.

    Raises:
        BundleError: For other types, non-finite numbers or strings that are too long.
    """
    item = _plain(value)
    if item is None:
        return None
    if isinstance(item, float) and not math.isfinite(item):
        raise BundleError(f"{field} must be finite, not {item}")
    if isinstance(item, bool | int | float):
        item = str(item)
    if not isinstance(item, str):
        raise BundleError(
            f"{field} must be a string, number or boolean, not {type(item).__name__}"
        )
    return _check_text(item, field, MAX_LABEL_CHARS)


# --------------------------------------------------------------------------- schema


def _validate_info(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise BundleError(f"info must be an object, not {type(raw).__name__}")
    _only_keys(raw, _INFO_KEYS, "info")
    methods_raw = raw.get("methods")
    if methods_raw is None:
        methods_raw = []
    if not isinstance(methods_raw, list | tuple):
        raise BundleError("info methods must be a list of strings")
    if len(methods_raw) > MAX_INFO_METHODS:
        raise BundleError(
            f"info methods has {len(methods_raw)} entries; the limit is "
            f"{MAX_INFO_METHODS}"
        )
    methods = [
        _check_text(item, f"info methods[{index}]", MAX_METHOD_CHARS, required=True)
        for index, item in enumerate(methods_raw)
    ]
    return {
        "methods": methods,
        "target": _check_text(raw.get("target"), "info target", MAX_INFO_TARGET_CHARS),
        "notes": _check_text(
            raw.get("notes"), "info notes", MAX_NOTES_CHARS, multiline=True
        ),
        "metrics": check_metrics(raw.get("metrics")),
    }


def _validate_case(raw: Any, position: int) -> dict[str, Any]:
    where = f"case {position}"
    if not isinstance(raw, Mapping):
        raise BundleError(f"{where} must be an object, not {type(raw).__name__}")
    _only_keys(raw, _CASE_KEYS, where)
    case = {
        "case_id": check_case_id(raw.get("case_id")),
        "label": _check_text(raw.get("label"), f"{where} label", MAX_LABEL_CHARS),
        "prediction": _check_text(
            raw.get("prediction"), f"{where} prediction", MAX_LABEL_CHARS
        ),
        "score": _check_number(raw.get("score"), f"{where} score"),
        "target": _check_text(raw.get("target"), f"{where} target", MAX_LABEL_CHARS),
        "outcome": check_outcome(raw.get("outcome")),
        "details": check_details(raw.get("details")),
    }
    panels_raw = raw.get("panels", [])
    if not isinstance(panels_raw, list):
        raise BundleError(f"{where} panels must be a list")
    if len(panels_raw) > MAX_PANELS_PER_CASE:
        raise BundleError(
            f"{where} has {len(panels_raw)} panels; the limit is {MAX_PANELS_PER_CASE}"
        )
    panels = []
    for index, panel in enumerate(panels_raw, start=1):
        place = f"{where} panel {index}"
        if not isinstance(panel, Mapping):
            raise BundleError(f"{place} must be an object")
        _only_keys(panel, _PANEL_KEYS, place)
        expected = panel_file_name(position, index)
        if panel.get("file") != expected:
            raise BundleError(
                f"{place}: file must be {expected!r}, not {_short(panel.get('file'))}"
            )
        panels.append(
            {
                "method": _check_text(
                    panel.get("method"),
                    f"{place} method",
                    MAX_METHOD_CHARS,
                    required=True,
                ),
                "title": _check_text(
                    panel.get("title"), f"{place} title", MAX_TITLE_CHARS
                ),
                "file": expected,
            }
        )
    case["panels"] = panels
    return case


def _validate_summary(raw: Any, position: int) -> dict[str, Any]:
    where = f"summary {position}"
    if not isinstance(raw, Mapping):
        raise BundleError(f"{where} must be an object")
    _only_keys(raw, _SUMMARY_KEYS, where)
    expected = summary_file_name(position)
    if raw.get("file") != expected:
        raise BundleError(
            f"{where}: file must be {expected!r}, not {_short(raw.get('file'))}"
        )
    return {
        "title": _check_text(
            raw.get("title"), f"{where} title", MAX_TITLE_CHARS, required=True
        ),
        "file": expected,
    }


def validate_cases(data: Any) -> dict[str, Any]:
    """Validate a parsed ``cases.json`` object and return a normalised copy.

    The copy has every key: missing optional values become None, missing ``details`` and
    ``metrics`` become ``{}``, missing ``panels``, ``methods`` and ``summaries`` become
    ``[]``. Unknown keys are rejected at every level.

    Args:
        data: The object read from ``cases.json``.

    Returns:
        The normalised bundle description.

    Raises:
        BundleError: On the first broken rule, with a specific message.
    """
    if not isinstance(data, Mapping):
        raise BundleError("cases.json must hold a JSON object")
    _only_keys(data, _TOP_KEYS, "cases.json")
    if data.get("schema") != SCHEMA:
        raise BundleError(
            f"cases.json schema is {_short(data.get('schema'))}; expected {SCHEMA!r}"
        )
    complete = data.get("complete")
    if not isinstance(complete, bool):
        raise BundleError("cases.json 'complete' must be true or false")
    info = _validate_info(data.get("info"))
    cases_raw = data.get("cases")
    if not isinstance(cases_raw, list):
        raise BundleError("cases.json 'cases' must be a list")
    if len(cases_raw) > MAX_CASES:
        raise BundleError(
            f"cases.json has {len(cases_raw)} cases; the limit is {MAX_CASES}"
        )
    cases = []
    seen: set[str] = set()
    for position, raw in enumerate(cases_raw, start=1):
        case = _validate_case(raw, position)
        if case["case_id"] in seen:
            raise BundleError(
                f"case {position}: case_id {case['case_id']!r} is used twice"
            )
        seen.add(case["case_id"])
        cases.append(case)
    summaries_raw = data.get("summaries", [])
    if not isinstance(summaries_raw, list):
        raise BundleError("cases.json 'summaries' must be a list")
    if len(summaries_raw) > MAX_SUMMARIES:
        raise BundleError(
            f"cases.json has {len(summaries_raw)} summaries; the limit is "
            f"{MAX_SUMMARIES}"
        )
    summaries = [
        _validate_summary(raw, position)
        for position, raw in enumerate(summaries_raw, start=1)
    ]
    return {
        "schema": SCHEMA,
        "complete": complete,
        "info": info,
        "cases": cases,
        "summaries": summaries,
    }


def _reject_constant(name: str) -> Any:
    raise BundleError(f"cases.json contains {name}, which is not valid JSON")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"cases.json repeats the key {_short(key)}")
        result[key] = value
    return result


def loads_cases(raw: bytes | str) -> dict[str, Any]:
    """Parse ``cases.json`` strictly and validate it.

    NaN and Infinity, duplicate keys, invalid UTF-8 and files over 1 MiB are rejected.

    Args:
        raw: The file content.

    Returns:
        The normalised bundle description (see :func:`validate_cases`).

    Raises:
        BundleError: If the content is not valid.
    """
    if isinstance(raw, bytes | bytearray):
        if len(raw) > MAX_CASES_JSON_BYTES:
            raise BundleError(
                f"cases.json is {len(raw)} bytes; the limit is {MAX_CASES_JSON_BYTES}"
            )
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise BundleError("cases.json is not valid UTF-8") from None
    elif isinstance(raw, str):
        text = raw
    else:
        raise BundleError("cases.json content must be bytes or str")
    try:
        data = json.loads(
            text, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs
        )
    except BundleError:
        raise
    except (ValueError, RecursionError) as exc:
        raise BundleError(f"cases.json is not valid JSON: {exc}") from None
    return validate_cases(data)


def dumps_cases(data: Mapping[str, Any]) -> bytes:
    """Validate a bundle description and return the canonical ``cases.json`` bytes.

    Raises:
        BundleError: If the description is not valid or the file would exceed 1 MiB.
    """
    normalised = validate_cases(data)
    raw = (
        json.dumps(normalised, ensure_ascii=False, allow_nan=False, indent=1) + "\n"
    ).encode("utf-8")
    if len(raw) > MAX_CASES_JSON_BYTES:
        raise BundleError(
            f"cases.json would be {len(raw)} bytes; the limit is {MAX_CASES_JSON_BYTES}"
        )
    return raw


# --------------------------------------------------------------------------- PNG


def png_size(data: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` from a PNG header without decoding the image.

    Raises:
        BundleError: If the data does not start with a PNG signature and IHDR chunk.
    """
    if bytes(data[:8]) != PNG_SIGNATURE:
        raise BundleError("not a PNG file (wrong signature)")
    if len(data) < 33 or bytes(data[12:16]) != b"IHDR":
        raise BundleError("PNG file has no IHDR header")
    width, height = struct.unpack(">II", bytes(data[16:24]))
    return width, height


def check_png(data: bytes, name: str = "image") -> tuple[int, int]:
    """Check size, signature and pixel count of PNG data without decoding it.

    Args:
        data: The PNG bytes.
        name: The name used in error messages.

    Returns:
        ``(width, height)``.

    Raises:
        BundleError: If a check fails.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise BundleError(f"{name} must be bytes, not {type(data).__name__}")
    if len(data) > MAX_PNG_BYTES:
        raise BundleError(f"{name} is {len(data)} bytes; the limit is {MAX_PNG_BYTES}")
    try:
        width, height = png_size(data)
    except BundleError as exc:
        raise BundleError(f"{name}: {exc}") from None
    if width == 0 or height == 0:
        raise BundleError(f"{name} has no pixels")
    if width * height > MAX_PNG_PIXELS:
        raise BundleError(
            f"{name} is {width} x {height} pixels; the limit is {MAX_PNG_PIXELS} pixels"
        )
    return width, height


def sanitize_png(
    data: bytes, name: str = "image", *, max_side: int = MAX_PNG_SIDE
) -> tuple[bytes, int, int]:
    """Decode a PNG and encode its pixels again as a new PNG.

    The new file holds only the first frame's pixels as RGB or RGBA: text chunks, EXIF,
    ICC profiles and any other metadata are dropped. Images whose longer side exceeds
    ``max_side`` are downscaled.

    Args:
        data: Untrusted PNG bytes.
        name: The name used in error messages.
        max_side: The largest allowed width or height after re-encoding.

    Returns:
        ``(png_bytes, width, height)`` of the new image.

    Raises:
        BundleError: If the data is not a decodable PNG within the limits.
    """
    from PIL import Image

    width, height = check_png(data, name)
    try:
        with Image.open(io.BytesIO(bytes(data)), formats=["PNG"]) as image:
            if image.size != (width, height):
                raise BundleError(f"{name}: header and decoder disagree on the size")
            image.verify()
        with Image.open(io.BytesIO(bytes(data)), formats=["PNG"]) as image:
            image.load()
            has_alpha = image.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (
                "transparency" in image.info
            )
            clean = image.convert("RGBA" if has_alpha else "RGB")
    except BundleError:
        raise
    except Exception as exc:  # Pillow raises many types for broken files.
        raise BundleError(f"{name} is not a valid PNG image: {exc}") from None
    # convert() copies the source's metadata (text, ICC profile, EXIF, ...); the PNG
    # encoder reads some of it from ``info``, so start from an empty one.
    clean.info = {}
    if max(clean.size) > max_side:
        clean.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    clean.save(buffer, format="PNG")
    return buffer.getvalue(), clean.width, clean.height


# --------------------------------------------------------------------------- files


def _open_dir(path: str | os.PathLike[str]) -> int:
    try:
        return os.open(path, _DIR_FLAGS)
    except FileNotFoundError:
        raise BundleError(
            f"bundle directory {os.fspath(path)} does not exist"
        ) from None
    except OSError as exc:
        raise BundleError(
            f"bundle directory {os.fspath(path)} cannot be opened safely (it is a "
            f"symbolic link or not a directory): {exc.strerror}"
        ) from None


def _scan(dir_fd: int) -> dict[str, int]:
    """Return ``{name: size}`` of the directory after checking names, types, sizes."""
    sizes: dict[str, int] = {}
    with os.scandir(dir_fd) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > MAX_FILES:
                raise BundleError(
                    f"the bundle directory holds more than {MAX_FILES} entries"
                )
            name = entry.name
            if not is_bundle_file_name(name):
                raise BundleError(
                    f"unexpected file {_short(name)} in the bundle directory; allowed "
                    "names are cases.json, case_NN_panel_K.png and summary_K.png"
                )
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise BundleError(f"{name} is a symbolic link")
            if not stat.S_ISREG(info.st_mode):
                raise BundleError(f"{name} is not a regular file")
            limit = MAX_CASES_JSON_BYTES if name == CASES_FILE else MAX_PNG_BYTES
            if info.st_size > limit:
                raise BundleError(
                    f"{name} is {info.st_size} bytes; the limit is {limit}"
                )
            sizes[name] = info.st_size
    if CASES_FILE not in sizes:
        raise BundleError("the bundle has no cases.json")
    total = sum(sizes.values())
    if total > MAX_BUNDLE_BYTES:
        raise BundleError(
            f"the bundle holds {total} bytes; the limit is {MAX_BUNDLE_BYTES}"
        )
    return sizes


def _read_regular(dir_fd: int, name: str, max_bytes: int) -> bytes:
    """Read a regular file below ``dir_fd`` without following links or blocking."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise BundleError(
            f"{name} cannot be opened safely (a symbolic link?): {exc.strerror}"
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BundleError(f"{name} is not a regular file")
        if info.st_nlink != 1:
            raise BundleError(f"{name} has {info.st_nlink} hard links")
        if info.st_size > max_bytes:
            raise BundleError(
                f"{name} is {info.st_size} bytes; the limit is {max_bytes}"
            )
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise BundleError(f"{name} grew beyond {max_bytes} bytes while being read")
        return data
    finally:
        os.close(fd)


def _check_file_set(names: list[str], sizes: Mapping[str, int]) -> None:
    present = set(sizes) - {CASES_FILE}
    for name in names:
        if name not in present:
            raise BundleError(f"cases.json refers to {name}, which is missing")
    extra = sorted(present - set(names))
    if extra:
        raise BundleError(f"{extra[0]} is not referenced by cases.json")


def _write_new(dir_fd: int, name: str, data: bytes) -> None:
    """Create ``name`` below ``dir_fd`` and write ``data``; remove it on failure."""
    fd = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)
        raise


def _remove_written(dir_fd: int, names: list[str]) -> None:
    for name in names:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)


def _missing_parent(path: str) -> str | None:
    """Return the parent folder of ``path`` if it does not exist, else None."""
    parent = os.path.dirname(os.path.abspath(path))
    return None if os.path.isdir(parent) else parent


def check_destination(path: str | os.PathLike[str]) -> None:
    """Check that a bundle can be written to ``path``, without creating anything.

    ``path`` must be an empty directory, or not exist yet inside an existing one.
    Symbolic links are not followed; resolve the path first to allow them.

    Raises:
        BundleError: If the destination cannot take a new bundle.
    """
    path = os.fspath(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        parent = _missing_parent(path)
        if parent is not None:
            raise BundleError(
                f"cannot create destination {path}: its parent folder {parent} "
                "does not exist"
            ) from None
        return
    except OSError as exc:
        raise BundleError(
            f"destination {path} cannot be checked: {exc.strerror}"
        ) from None
    if not stat.S_ISDIR(info.st_mode):
        raise BundleError(f"destination {path} is not a plain directory")
    try:
        entries = os.listdir(path)
    except OSError as exc:
        raise BundleError(
            f"destination {path} cannot be read: {exc.strerror}"
        ) from None
    if entries:
        raise BundleError(f"destination {path} is not empty")


def _prepare_destination(path: str | os.PathLike[str]) -> int:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        parent = _missing_parent(os.fspath(path))
        reason = (
            f"its parent folder {parent} does not exist" if parent else exc.strerror
        )
        raise BundleError(
            f"cannot create destination {os.fspath(path)}: {reason}"
        ) from None
    try:
        fd = os.open(path, _DIR_FLAGS)
    except OSError as exc:
        raise BundleError(
            f"destination {os.fspath(path)} is not a plain directory: {exc.strerror}"
        ) from None
    if os.listdir(fd):
        os.close(fd)
        raise BundleError(f"destination {os.fspath(path)} is not empty")
    return fd


def _load_bundle(
    bundle_dir: str | os.PathLike[str], *, require_complete: bool, keep_images: bool
) -> tuple[dict[str, Any], dict[str, bytes]]:
    dir_fd = _open_dir(bundle_dir)
    try:
        sizes = _scan(dir_fd)
        data = loads_cases(_read_regular(dir_fd, CASES_FILE, MAX_CASES_JSON_BYTES))
        if require_complete and not data["complete"]:
            raise BundleError(
                "the bundle is incomplete (cases.json has complete: false); the "
                "explanation script did not finish"
            )
        names = bundle_file_names(data)
        _check_file_set(names, sizes)
        images: dict[str, bytes] = {}
        for name in names:
            raw = _read_regular(dir_fd, name, MAX_PNG_BYTES)
            check_png(raw, name)
            if keep_images:
                images[name] = raw
        return data, images
    finally:
        os.close(dir_fd)


def validate_bundle(
    bundle_dir: str | os.PathLike[str], *, require_complete: bool = True
) -> dict[str, Any]:
    """Check a bundle directory and return its normalised description.

    Checks: the directory and its files are not symbolic links; only regular files with
    the fixed names; size caps; strict ``cases.json``; every referenced PNG present with
    a PNG signature and at most 40 megapixels; no unreferenced files. Images are not
    decoded; use :func:`safe_copy_bundle` for directories written by untrusted code.

    Args:
        bundle_dir: The bundle directory.
        require_complete: Reject bundles whose ``complete`` flag is false.

    Returns:
        The normalised bundle description.

    Raises:
        BundleError: On the first broken rule.
    """
    data, _ = _load_bundle(
        bundle_dir, require_complete=require_complete, keep_images=False
    )
    return data


def read_bundle(
    bundle_dir: str | os.PathLike[str], *, require_complete: bool = True
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Check a bundle directory like :func:`validate_bundle` and return its images too.

    Returns:
        ``(description, {file name: PNG bytes})``. The PNG bytes are not re-encoded.

    Raises:
        BundleError: On the first broken rule.
    """
    return _load_bundle(bundle_dir, require_complete=require_complete, keep_images=True)


def write_bundle(
    out_dir: str | os.PathLike[str],
    data: Mapping[str, Any],
    images: Mapping[str, bytes],
) -> dict[str, Any]:
    """Write a complete bundle in one step.

    Args:
        out_dir: A new or empty directory.
        data: The bundle description (as in ``cases.json``; file names by position).
        images: PNG bytes for every file name the description refers to.

    Returns:
        The normalised bundle description that was written.

    Raises:
        BundleError: If the description or an image is not valid, an image is missing or
            not referenced, or the directory is not empty.
    """
    normalised = validate_cases(data)
    names = bundle_file_names(normalised)
    for name in names:
        if name not in images:
            raise BundleError(f"no image given for {name}")
    for name in images:
        if name not in names:
            raise BundleError(f"image {_short(name)} is not referenced by the cases")
    for name in names:
        check_png(images[name], name)
    raw = dumps_cases(normalised)
    dir_fd = _prepare_destination(out_dir)
    written: list[str] = []
    try:
        for name in names:
            _write_new(dir_fd, name, bytes(images[name]))
            written.append(name)
        _write_new(dir_fd, CASES_FILE, raw)
    except BaseException:
        _remove_written(dir_fd, written)
        raise
    finally:
        os.close(dir_fd)
    return normalised


def safe_copy_bundle(
    src_dir: str | os.PathLike[str],
    dst_dir: str | os.PathLike[str],
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Copy a bundle written by untrusted code into a private directory.

    The source directory and every file are opened with ``O_NOFOLLOW`` (and files with
    ``O_NONBLOCK``, so a FIFO cannot block the caller) relative to one directory handle.
    Only regular files with the fixed names and within the size caps are read, each
    once. ``cases.json`` is parsed strictly, validated and written again in canonical
    form; every PNG is decoded and re-encoded with Pillow, which drops metadata, and
    written at once, so only one decoded image is held in memory. The images may hold
    at most ``MAX_BUNDLE_PIXELS`` pixels together (checked from the headers before
    decoding); each re-encoded PNG is at most ``MAX_PNG_BYTES`` and all of them
    together at most ``MAX_BUNDLE_BYTES``. ``cases.json`` is written last; on any
    error the files already written are removed, so a failed copy leaves no files.

    Args:
        src_dir: The untrusted bundle directory.
        dst_dir: A new or empty directory that the untrusted code cannot reach.
        require_complete: Reject bundles whose ``complete`` flag is false.

    Returns:
        ``{"data": description, "files": {name: {"width", "height", "bytes"}}}`` for the
        copied bundle.

    Raises:
        BundleError: If the source breaks a rule or the destination is not empty.
    """
    dst_fd = _prepare_destination(dst_dir)
    written: list[str] = []
    try:
        src_fd = _open_dir(src_dir)
        try:
            sizes = _scan(src_fd)
            data = loads_cases(_read_regular(src_fd, CASES_FILE, MAX_CASES_JSON_BYTES))
            if require_complete and not data["complete"]:
                raise BundleError(
                    "the bundle is incomplete (cases.json has complete: false); the "
                    "explanation script did not finish"
                )
            names = bundle_file_names(data)
            _check_file_set(names, sizes)
            files: dict[str, dict[str, int]] = {}
            read_total = written_total = pixels = 0
            for name in names:
                raw = _read_regular(src_fd, name, MAX_PNG_BYTES)
                read_total += len(raw)
                if read_total > MAX_BUNDLE_BYTES:
                    raise BundleError(
                        f"the bundle holds more than {MAX_BUNDLE_BYTES} bytes"
                    )
                width, height = check_png(raw, name)
                pixels += width * height
                if pixels > MAX_BUNDLE_PIXELS:
                    raise BundleError(
                        f"the bundle's images hold more than {MAX_BUNDLE_PIXELS} "
                        "pixels together"
                    )
                png, width, height = sanitize_png(raw, name)
                del raw
                if len(png) > MAX_PNG_BYTES:
                    raise BundleError(
                        f"{name} is {len(png)} bytes after re-encoding; the limit "
                        f"is {MAX_PNG_BYTES}"
                    )
                written_total += len(png)
                if written_total > MAX_BUNDLE_BYTES:
                    raise BundleError(
                        f"the re-encoded images hold more than {MAX_BUNDLE_BYTES} bytes"
                    )
                _write_new(dst_fd, name, png)
                written.append(name)
                files[name] = {"width": width, "height": height, "bytes": len(png)}
        finally:
            os.close(src_fd)
        _write_new(dst_fd, CASES_FILE, dumps_cases(data))
        written.append(CASES_FILE)
    except BaseException:
        _remove_written(dst_fd, written)
        raise
    finally:
        os.close(dst_fd)
    return {"data": data, "files": files}


def describe(data: Mapping[str, Any]) -> str:
    """Return a one-line count of the cases, panels and summaries in a description."""
    n_cases = len(data["cases"])
    n_panels = sum(len(case["panels"]) for case in data["cases"])
    n_summaries = len(data["summaries"])
    return (
        f"{n_cases} case{'s' if n_cases != 1 else ''}, "
        f"{n_panels} panel{'s' if n_panels != 1 else ''}, "
        f"{n_summaries} summar{'ies' if n_summaries != 1 else 'y'}"
    )


# --------------------------------------------------------------------------- writer


class BundleWriter:
    """Build a bundle one case, panel and summary at a time.

    ``cases.json`` is rewritten after every change, with ``"complete": false`` until
    :meth:`finish` is called. Every value is checked when it is added, with the same
    rules the validator applies, so a script fails at the call that breaks a rule.
    """

    def __init__(self, out_dir: str | os.PathLike[str]) -> None:
        """Create the writer and an incomplete ``cases.json`` in ``out_dir``.

        Args:
            out_dir: A new or empty directory.

        Raises:
            BundleError: If the directory exists and is not empty.
        """
        self.out_dir = os.path.abspath(os.fspath(out_dir))
        os.makedirs(self.out_dir, exist_ok=True)
        if os.listdir(self.out_dir):
            raise BundleError(f"output directory {self.out_dir} is not empty")
        self._cases: list[dict[str, Any]] = []
        self._positions: dict[str, int] = {}
        self._summaries: list[dict[str, Any]] = []
        self._info: dict[str, Any] = {
            "methods": [],
            "target": None,
            "notes": None,
            "metrics": {},
        }
        self._complete = False
        self._finished: dict[str, Any] | None = None
        self.write()

    @property
    def data(self) -> dict[str, Any]:
        """A copy of the bundle description as it stands."""
        return {
            "schema": SCHEMA,
            "complete": self._complete,
            "info": copy.deepcopy(self._info),
            "cases": copy.deepcopy(self._cases),
            "summaries": copy.deepcopy(self._summaries),
        }

    @property
    def case_ids(self) -> list[str]:
        """The case IDs added so far, in order."""
        return [case["case_id"] for case in self._cases]

    def _check_open(self) -> None:
        if self._finished is not None:
            raise RuntimeError("the bundle is finished; nothing can be added")

    def add_case(
        self,
        case_id: Any,
        *,
        label: Any = None,
        prediction: Any = None,
        score: Any = None,
        target: Any = None,
        outcome: Any = None,
        details: Any = None,
    ) -> int:
        """Add a case and return its position (starting at 1).

        Args:
            case_id: The site's pseudonymous case ID (``CASE_ID_PATTERN``), unique.
            label: The reference label; numbers are stored as strings (max 64 chars).
            prediction: The model's prediction, stored like ``label``.
            score: The model's score for the explained target, a finite number.
            target: What the explanation explains, e.g. the class name.
            outcome: One of ``OUTCOMES`` or None.
            details: A small flat dict; see :func:`check_details`.

        Returns:
            The case position.

        Raises:
            BundleError: If a value breaks a rule or 24 cases exist already.
        """
        self._check_open()
        case_id = check_case_id(case_id)
        if case_id in self._positions:
            raise BundleError(f"case_id {case_id!r} was added already")
        if len(self._cases) >= MAX_CASES:
            raise BundleError(f"a bundle holds at most {MAX_CASES} cases")
        case = {
            "case_id": case_id,
            "label": coerce_label(label, "label"),
            "prediction": coerce_label(prediction, "prediction"),
            "score": _check_number(score, "score"),
            "target": coerce_label(target, "target"),
            "outcome": check_outcome(outcome),
            "details": check_details(details),
            "panels": [],
        }
        if case["score"] is not None:
            case["score"] = float(case["score"])
        self._cases.append(case)
        self._positions[case_id] = len(self._cases)
        self.write()
        return len(self._cases)

    def check_panel(self, case_id: Any, method: Any, title: Any = None) -> str:
        """Check that a panel can be added and return the file name it will get.

        Raises:
            BundleError: If the case is unknown, has 4 panels, or a text is not valid.
        """
        self._check_open()
        case_id = _plain(case_id)
        position = self._positions.get(case_id) if isinstance(case_id, str) else None
        if position is None:
            raise BundleError(f"unknown case_id {_short(case_id)}; call add_case first")
        _check_text(_plain(method), "method", MAX_METHOD_CHARS, required=True)
        _check_text(_plain(title), "title", MAX_TITLE_CHARS)
        panels = self._cases[position - 1]["panels"]
        if len(panels) >= MAX_PANELS_PER_CASE:
            raise BundleError(
                f"case {case_id!r} has {MAX_PANELS_PER_CASE} panels already"
            )
        return panel_file_name(position, len(panels) + 1)

    def add_panel_png(
        self, case_id: Any, method: Any, png: bytes, *, title: Any = None
    ) -> str:
        """Add a PNG panel to a case and return its file name.

        Args:
            case_id: A case added with :meth:`add_case`.
            method: The explanation method, e.g. ``"integrated_gradients"`` (max 64).
            png: The PNG bytes (at most 8 MiB and 40 megapixels).
            title: An optional caption (max 255 characters).

        Returns:
            The panel's file name.

        Raises:
            BundleError: If a rule is broken.
        """
        name = self.check_panel(case_id, method, title)
        check_png(png, name)
        position = self._positions[_plain(case_id)]
        with open(os.path.join(self.out_dir, name), "xb") as handle:
            handle.write(bytes(png))
        self._cases[position - 1]["panels"].append(
            {"method": _plain(method), "title": _plain(title), "file": name}
        )
        self.write()
        return name

    def check_summary(self, title: Any) -> str:
        """Check that a summary can be added and return the file name it will get.

        Raises:
            BundleError: If 4 summaries exist already or the title is not valid.
        """
        self._check_open()
        _check_text(_plain(title), "title", MAX_TITLE_CHARS, required=True)
        if len(self._summaries) >= MAX_SUMMARIES:
            raise BundleError(f"a bundle holds at most {MAX_SUMMARIES} summary figures")
        return summary_file_name(len(self._summaries) + 1)

    def add_summary_png(self, title: Any, png: bytes) -> str:
        """Add a run-level PNG figure and return its file name.

        Args:
            title: The figure's title (required, max 255 characters).
            png: The PNG bytes.

        Returns:
            The figure's file name.

        Raises:
            BundleError: If a rule is broken.
        """
        name = self.check_summary(title)
        check_png(png, name)
        with open(os.path.join(self.out_dir, name), "xb") as handle:
            handle.write(bytes(png))
        self._summaries.append({"title": _plain(title), "file": name})
        self.write()
        return name

    def set_info(
        self,
        *,
        methods: Any = None,
        target: Any = None,
        notes: Any = None,
        metrics: Any = None,
    ) -> None:
        """Set run-level information; arguments left as None keep their earlier value.

        Args:
            methods: Names of the methods used (at most 16, each at most 64 characters).
            target: What is explained, e.g. ``"abnormal ECG (class 1)"`` (max 255).
            notes: Free text for the reviewer (max 4,000 characters).
            metrics: A flat dict of finite numbers, e.g. ``{"auroc_test": 0.87}``.

        Raises:
            BundleError: If a value breaks a rule.
        """
        self._check_open()
        if isinstance(methods, str):
            raise BundleError("methods must be a list of strings, not one string")
        target = _plain(target)
        if isinstance(target, bool | int | float):
            target = str(target)
        update = {
            key: value
            for key, value in {
                "methods": None if methods is None else list(methods),
                "target": target,
                "notes": _plain(notes),
                "metrics": metrics,
            }.items()
            if value is not None
        }
        merged = {**self._info, **update}
        self._info = _validate_info(merged)
        self.write()

    def write(self) -> None:
        """Write ``cases.json`` as it stands (atomically)."""
        raw = dumps_cases(self.data)
        path = os.path.join(self.out_dir, CASES_FILE)
        temporary = os.path.join(self.out_dir, f".{CASES_FILE}.tmp")
        with open(temporary, "wb") as handle:
            handle.write(raw)
        os.replace(temporary, path)

    def finish(self) -> dict[str, Any]:
        """Mark the bundle complete, write it and validate the directory.

        Calling it again returns the same description.

        Returns:
            The normalised bundle description.

        Raises:
            BundleError: If the directory does not validate (for example because
                something else wrote files into it).
        """
        if self._finished is not None:
            return copy.deepcopy(self._finished)
        self._complete = True
        self.write()
        self._finished = validate_bundle(self.out_dir)
        return copy.deepcopy(self._finished)
