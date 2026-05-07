"""Shared helpers for training result artifact names and S3 keys."""

from __future__ import annotations

import os
from dataclasses import dataclass

GLOBAL_MODEL_FILENAME = "FL_global_model.pt"
GLOBAL_MODEL_FILENAME_CANDIDATES: tuple[str, ...] = (
    GLOBAL_MODEL_FILENAME,
    "best_FL_model.pt",
    "global_model.pt",
    "FL_model.pt",
    "model_weights.npz",
)
MODEL_ARTIFACT_SUFFIXES: tuple[str, ...] = (
    ".pt",
    ".pth",
    ".ckpt",
    ".npz",
    ".npy",
    ".pkl",
    ".joblib",
    ".h5",
    ".keras",
)


@dataclass(frozen=True)
class ResultKeyInfo:
    """Parsed metadata for a key below ``<project>/results/``."""

    flare_job_id: str
    participant: str
    filename: str
    relative_path: str
    is_legacy_layout: bool


def is_model_artifact(filename: str) -> bool:
    """Return true when ``filename`` looks like a model/checkpoint artifact."""
    name = os.path.basename(filename or "").lower()
    return any(name.endswith(suffix) for suffix in MODEL_ARTIFACT_SUFFIXES)


def is_global_model_artifact(filename: str) -> bool:
    """Return true for canonical or legacy global model filenames."""
    name = os.path.basename(filename or "")
    return name in GLOBAL_MODEL_FILENAME_CANDIDATES


def parse_result_key(key: str, project_identifier: str) -> ResultKeyInfo | None:
    """Parse canonical and legacy result object keys.

    Canonical: ``<project>/results/<flare_job_uuid>/<participant>/<artifact>``.
    Legacy: ``<project>/results/<flare_job_uuid>/<artifact>``.
    """
    prefix = f"{project_identifier}/results/"
    if not key.startswith(prefix) or key.endswith("/"):
        return None

    relative_path = key[len(prefix) :]
    parts = [part for part in relative_path.split("/") if part]
    if len(parts) < 2:
        return None

    flare_job_id = parts[0]
    filename = parts[-1]
    if len(parts) == 2:
        participant = "local"
        is_legacy_layout = True
    else:
        participant = parts[1]
        is_legacy_layout = False

    return ResultKeyInfo(
        flare_job_id=flare_job_id,
        participant=participant,
        filename=filename,
        relative_path=relative_path,
        is_legacy_layout=is_legacy_layout,
    )
