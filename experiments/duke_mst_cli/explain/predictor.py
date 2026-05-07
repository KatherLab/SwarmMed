"""Per-case prediction loop for the MST FL global model.

Reuses the checkpoint loading helpers from ``evaluate_global_model.py`` and
adds a per-case predictions CSV (uid, target, predicted class, probabilities).
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch

LOGGER = logging.getLogger("duke_mst_explain.predictor")

_THIS_DIR = Path(__file__).resolve().parent
_EVAL_DIR = _THIS_DIR.parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from evaluate_global_model import (  # noqa: E402  (sys.path injected above)
    _add_mediswarm_paths,
    _build_model,
    _compatible_state_dict,
    _file_md5,
    _state_dict_from_fl_global_model,
)


def _ensure_models_subdir_on_path() -> None:
    """The MediSwarm models_config.py lives at ``custom/models/`` — older eval
    scripts only add ``custom/`` itself. Ensure the ``models/`` subdir is on
    sys.path so ``from models_config import create_model`` works regardless of
    which evaluate_global_model.py revision is checked out.
    """
    extra: list[Path] = []
    for path_str in list(sys.path):
        candidate = Path(path_str) / "models"
        if (candidate / "models_config.py").exists():
            extra.append(candidate)
    project_root = os.environ.get("MEDISWARM_PROJECT_ROOT", "").strip()
    if project_root:
        candidate = Path(project_root) / "application" / "jobs" / "_shared" / "custom" / "models"
        if (candidate / "models_config.py").exists():
            extra.append(candidate)
    for path in extra:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _set_required_env(model_name: str) -> None:
    """MediSwarm's `load_environment_variables()` expects several env vars to be
    set even at inference time. Provide safe defaults so a single import works
    without requiring the caller to know the federated training contract.
    """
    os.environ.setdefault("SCRATCH_DIR", "/tmp")
    os.environ.setdefault("DATA_DIR", "/data")
    os.environ.setdefault("SITE_NAME", "test")
    os.environ.setdefault("INSTITUTION", "test")
    os.environ.setdefault("CONFIG", "unilateral")
    os.environ.setdefault("MODEL_NAME", model_name)
    os.environ.setdefault("TRAINING_MODE", "swarm")
    os.environ.setdefault("EPOCHS_PER_ROUND", "1")
    os.environ.setdefault("EPOCHS_MAX_CAP", "1")


def load_model(checkpoint_path: Path, model_name: str = "MST", num_classes: int = 3) -> torch.nn.Module:
    """Load an MST model from an NVFlare ``FL_global_model.pt`` checkpoint."""
    _add_mediswarm_paths()
    _ensure_models_subdir_on_path()
    _set_required_env(model_name)
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for MST inference.")
    # Some driver/cuDNN combos (e.g. CUDA 12.2 driver + torch cu128) hit
    # CUDNN_STATUS_NOT_INITIALIZED in DINOv2's first conv. Falling back to the
    # native CUDA conv kernels is correct and only marginally slower.
    if os.environ.get("MST_DISABLE_CUDNN", "1") == "1":
        torch.backends.cudnn.enabled = False
    device = torch.device("cuda")

    checkpoint_path = Path(checkpoint_path)
    model = _build_model(model_name, num_classes=num_classes).to(device)
    state_dict = _state_dict_from_fl_global_model(checkpoint_path)
    compatible, _ = _compatible_state_dict(model, state_dict)
    model.load_state_dict(compatible, strict=False)
    model.eval()
    LOGGER.info("Loaded checkpoint %s (md5=%s)", checkpoint_path, _file_md5(checkpoint_path))
    return model


def build_dataloader(
    data_root: Path,
    institution: str,
    *,
    split: str = "test",
    config: str = "unilateral",
    num_workers: int = 4,
):
    """Build the ODELIA_Dataset3D + DataLoader."""
    _add_mediswarm_paths()
    _ensure_models_subdir_on_path()
    from data.datasets import ODELIA_Dataset3D  # noqa: WPS433

    os.environ.setdefault("DATA_DIR", str(data_root))
    dataset = ODELIA_Dataset3D(
        path_root=str(data_root),
        institutions=institution,
        split=split,
        config=config,
    )
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=1,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=False,
    )
    return dataset, loader


def predict_per_case(
    model: torch.nn.Module,
    dataloader,
    *,
    positive_class: int = 2,
) -> list[dict[str, Any]]:
    """Run inference and return one record per case with uid + probabilities."""
    device = next(model.parameters()).device
    records: list[dict[str, Any]] = []

    with torch.no_grad():
        for batch in dataloader:
            uid = batch["uid"]
            uid = uid[0] if isinstance(uid, (list, tuple)) else str(uid)
            source = batch["source"].to(device, non_blocking=True)
            target = int(np.atleast_1d(batch["target"].numpy().squeeze())[0])

            logits = model(source)
            probs = model.logits2probabilities(logits).detach().cpu().numpy().squeeze()
            pred = int(np.argmax(probs))

            records.append(
                {
                    "uid": str(uid),
                    "target": target,
                    "pred": pred,
                    "prob_no": float(probs[0]),
                    "prob_benign": float(probs[1]),
                    "prob_malignant": float(probs[2]),
                    "is_tp": bool(target == positive_class and pred == positive_class),
                    "is_fp": bool(target != positive_class and pred == positive_class),
                    "is_fn": bool(target == positive_class and pred != positive_class),
                    "is_tn": bool(target != positive_class and pred != positive_class),
                }
            )
    return records


def write_predictions_csv(records: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    LOGGER.info("Wrote %d per-case predictions to %s", len(records), output_path)


def cohort_auroc(records: list[dict[str, Any]], positive_class: int = 2) -> float:
    from sklearn.metrics import roc_auc_score

    y_true = np.array([1 if r["target"] == positive_class else 0 for r in records])
    y_score = np.array([r["prob_malignant"] for r in records])
    return float(roc_auc_score(y_true, y_score))


def select_topk_per_outcome(
    records: list[dict[str, Any]],
    *,
    k: int = 2,
    positive_class: int = 2,
) -> dict[str, list[dict[str, Any]]]:
    """Pick the top-k most-confident cases for each prediction outcome.

    Outcomes:
      - "TP": target == positive_class & pred == positive_class, ranked by prob_malignant DESC.
      - "FP": target != positive_class & pred == positive_class, ranked by prob_malignant DESC.
      - "FN": target == positive_class & pred != positive_class, ranked by prob_malignant ASC
              (most-confident-wrong → lowest prob among missed positives).
      - "TN": target != positive_class & pred != positive_class, ranked by prob_malignant ASC
              (most-confident-correct rejection).
    """
    tp = [r for r in records if r["target"] == positive_class and r["pred"] == positive_class]
    fp = [r for r in records if r["target"] != positive_class and r["pred"] == positive_class]
    fn = [r for r in records if r["target"] == positive_class and r["pred"] != positive_class]
    tn = [r for r in records if r["target"] != positive_class and r["pred"] != positive_class]

    return {
        "TP": sorted(tp, key=lambda r: r["prob_malignant"], reverse=True)[:k],
        "FP": sorted(fp, key=lambda r: r["prob_malignant"], reverse=True)[:k],
        "FN": sorted(fn, key=lambda r: r["prob_malignant"])[:k],
        "TN": sorted(tn, key=lambda r: r["prob_malignant"])[:k],
    }
