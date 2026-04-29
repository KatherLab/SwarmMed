#!/usr/bin/env python3
"""Evaluate a DUKE MST NVFlare global model checkpoint.

Use this for `FL_global_model.pt` files produced by NVFlare's PyTorch
persistor. It intentionally avoids Lightning `.ckpt` files for the swarm
global evaluation path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score

LOGGER = logging.getLogger("duke_mst_eval")


def _add_mediswarm_paths() -> None:
    candidates = [
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path("/MediSwarm/application/jobs/_shared/custom"),
        Path("/MediSwarm/application/jobs/_shared/custom/models"),
        Path("/workspace/app/custom"),
        Path("/workspace/app/custom/models"),
        Path("/app/application/jobs/_shared/custom"),
        Path("/app/application/jobs/_shared/custom/models"),
    ]
    custom_dir = os.environ.get("MEDISWARM_CUSTOM_DIR", "").strip()
    if custom_dir:
        candidates.append(Path(custom_dir))
    project_root = os.environ.get("MEDISWARM_PROJECT_ROOT", "").strip()
    if project_root:
        candidates.append(
            Path(project_root) / "application" / "jobs" / "_shared" / "custom"
        )
        candidates.append(
            Path(project_root)
            / "application"
            / "jobs"
            / "_shared"
            / "custom"
            / "models"
        )
    for candidate in candidates:
        if str(candidate) and candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate DUKE MST using FL_global_model.pt only."
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        action="append",
        help="Path to an NVFlare FL_global_model.pt file. Repeat to compare sites.",
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get("DATA_DIR", "/mnt/dlhd0/DUKE_iid"),
        help="DUKE_iid dataset root. Defaults to DATA_DIR or /mnt/dlhd0/DUKE_iid.",
    )
    parser.add_argument(
        "--institution",
        default=os.environ.get("INSTITUTION") or os.environ.get("SITE_NAME") or "test",
        help=(
            "Dataset institution folder to evaluate. Defaults to "
            "INSTITUTION/SITE_NAME/test."
        ),
    )
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--config", default=os.environ.get("CONFIG", "unilateral"))
    parser.add_argument("--model-name", default=os.environ.get("MODEL_NAME", "MST"))
    parser.add_argument("--scratch-dir", default=os.environ.get("SCRATCH_DIR", "/tmp"))
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--positive-class", type=int, default=2)
    parser.add_argument("--output-json")
    return parser.parse_args()


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()  # nosec B324 - checksum reporting, not security.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_dict_from_fl_global_model(path: Path) -> dict[str, Any]:
    model_dict = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(model_dict, dict) or "model" not in model_dict:
        raise ValueError(f"{path} is not an NVFlare FL_global_model.pt payload")
    state_dict = model_dict["model"]
    if not isinstance(state_dict, dict):
        raise ValueError(f"{path} has a non-dict 'model' payload")
    return state_dict


def _compatible_state_dict(
    model: torch.nn.Module, state_dict: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    model_state = model.state_dict()
    ignored: list[str] = []
    compatible: dict[str, Any] = {}
    ignore_names = ("_class_weight", "loss.weight")

    for key, value in state_dict.items():
        if any(name in key for name in ignore_names):
            ignored.append(key)
            continue
        target = model_state.get(key)
        if (
            target is not None
            and hasattr(value, "shape")
            and target.shape != value.shape
        ):
            ignored.append(key)
            continue
        compatible[key] = value
    return compatible, ignored


def _build_model(model_name: str, num_classes: int) -> torch.nn.Module:
    from models_config import create_model

    return create_model(logger=LOGGER, model_name=model_name, num_classes=num_classes)


def _build_dataloader(args: argparse.Namespace):
    from data.datasets import ODELIA_Dataset3D

    dataset = ODELIA_Dataset3D(
        path_root=args.data_root,
        institutions=args.institution,
        split=args.split,
        config=args.config,
    )
    return torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=False,
    )


def _predict(
    model: torch.nn.Module, dataloader, device: torch.device
) -> dict[str, Any]:
    probabilities = []
    predictions = []
    targets = []

    with torch.no_grad():
        for batch in dataloader:
            source = batch["source"].to(device)
            target = batch["target"]
            logits = model(source)
            probs = model.logits2probabilities(logits)
            preds = model.logits2labels(logits)
            probabilities.append(probs.detach().cpu().numpy())
            predictions.append(np.atleast_1d(preds.detach().cpu().numpy().squeeze()))
            targets.append(np.atleast_1d(target.numpy().squeeze()))

    return {
        "probabilities": np.concatenate(probabilities, axis=0),
        "predictions": np.concatenate(predictions, axis=0).flatten(),
        "targets": np.concatenate(targets, axis=0).flatten(),
    }


def _binary_metrics(
    *,
    probabilities: np.ndarray,
    predictions: np.ndarray,
    targets: np.ndarray,
    positive_class: int,
) -> dict[str, Any]:
    y_true = (targets == positive_class).astype(int)
    y_pred = (predictions == positive_class).astype(int)
    y_score = probabilities[:, positive_class]

    tn = int(((y_true == 0) & (y_pred == 0)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    tp = int(((y_true == 1) & (y_pred == 1)).sum())

    sensitivity = tp / (tp + fn) if (tp + fn) else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0

    return {
        "num_samples": int(len(y_true)),
        "num_positive": int(y_true.sum()),
        "num_negative": int((y_true == 0).sum()),
        "auroc": float(roc_auc_score(y_true, y_score)),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "sensitivity": float(sensitivity),
        "specificity": float(specificity),
        "confusion_matrix": [[tn, fp], [fn, tp]],
        "positive_class": int(positive_class),
    }


def main() -> None:
    """Run checkpoint loading, inference, and binary DUKE metric reporting."""
    logging.basicConfig(level=logging.INFO)
    _add_mediswarm_paths()
    args = _parse_args()

    os.environ["DATA_DIR"] = args.data_root
    os.environ["SCRATCH_DIR"] = args.scratch_dir
    os.environ["SITE_NAME"] = args.institution
    os.environ["INSTITUTION"] = args.institution
    os.environ["CONFIG"] = args.config
    os.environ["MODEL_NAME"] = args.model_name

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required for MediSwarm MST evaluation.")

    device = torch.device("cuda")
    dataloader = _build_dataloader(args)
    results = []

    for raw_checkpoint in args.checkpoint:
        checkpoint = Path(raw_checkpoint).expanduser().resolve()
        if checkpoint.name != "FL_global_model.pt":
            raise ValueError(f"Use FL_global_model.pt only, got: {checkpoint}")

        model = _build_model(args.model_name, num_classes=3).to(device)
        state_dict = _state_dict_from_fl_global_model(checkpoint)
        compatible_state_dict, ignored_keys = _compatible_state_dict(model, state_dict)
        incompatible = model.load_state_dict(compatible_state_dict, strict=False)
        model.eval()

        prediction = _predict(model, dataloader, device)
        metrics = _binary_metrics(
            probabilities=prediction["probabilities"],
            predictions=prediction["predictions"],
            targets=prediction["targets"],
            positive_class=args.positive_class,
        )
        result = {
            "checkpoint": str(checkpoint),
            "md5": _file_md5(checkpoint),
            "metrics": metrics,
            "ignored_keys": ignored_keys,
            "missing_keys": list(incompatible.missing_keys),
            "unexpected_keys": list(incompatible.unexpected_keys),
        }
        results.append(result)

        print(
            f"{checkpoint}: md5={result['md5']} "
            f"AUROC={metrics['auroc']:.4f} "
            f"accuracy={metrics['accuracy']:.4f} "
            f"f1={metrics['f1']:.4f} "
            f"sensitivity={metrics['sensitivity']:.4f} "
            f"specificity={metrics['specificity']:.4f}"
        )

    payload = {
        "data_root": str(Path(args.data_root).expanduser().resolve()),
        "institution": args.institution,
        "split": args.split,
        "model_name": args.model_name,
        "results": results,
    }
    if args.output_json:
        output_json = Path(args.output_json).expanduser().resolve()
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
