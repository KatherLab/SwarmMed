#!/usr/bin/env python3
"""Export per-case predictions for DUKE MST FL_global_model.pt checkpoints."""

from __future__ import annotations

import argparse
import csv
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

LOGGER = logging.getLogger("duke_mst_export")


def _add_mediswarm_paths() -> None:
    candidates = [
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path("/MediSwarm/application/jobs/_shared/custom"),
        Path("/MediSwarm/application/jobs/_shared/custom/models"),
        Path("/MediSwarm/application/jobs/ODELIA_ternary_classification/app/custom"),
        Path("/MediSwarm/application/jobs/ODELIA_ternary_classification/app/custom/models"),
        Path("/workspace/app/custom"),
        Path("/workspace/app/custom/models"),
        Path("/app/application/jobs/_shared/custom"),
        Path("/app/application/jobs/_shared/custom/models"),
    ]
    for env_name in ("MEDISWARM_CUSTOM_DIR", "MEDISWARM_PROJECT_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw))
            candidates.append(Path(raw) / "application" / "jobs" / "_shared" / "custom")
            candidates.append(
                Path(raw) / "application" / "jobs" / "_shared" / "custom" / "models"
            )
    for candidate in candidates:
        if str(candidate) and candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()  # nosec B324 - checksum reporting, not security.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_dict_from_checkpoint(path: Path) -> dict[str, Any]:
    payload = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict) or "model" not in payload:
        raise ValueError(f"{path} is not an NVFlare FL_global_model.pt payload")
    state_dict = payload["model"]
    if not isinstance(state_dict, dict):
        raise ValueError(f"{path} has a non-dict model payload")
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
        if target is not None and hasattr(value, "shape") and target.shape != value.shape:
            ignored.append(key)
            continue
        compatible[key] = value
    return compatible, ignored


def _batch_uids(batch: dict[str, Any], offset: int, size: int) -> list[str]:
    for key in ("uid", "UID", "uids", "ids"):
        if key in batch:
            raw = batch[key]
            if isinstance(raw, (list, tuple)):
                return [str(item) for item in raw]
            if hasattr(raw, "tolist"):
                values = raw.tolist()
                if not isinstance(values, list):
                    values = [values]
                return [str(item) for item in values]
    return [f"sample_{offset + i}" for i in range(size)]


def _binary_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    y_true = np.asarray([int(row["y_true"]) for row in rows], dtype=int)
    y_pred = np.asarray([int(row["y_pred"]) for row in rows], dtype=int)
    y_score = np.asarray([float(row["y_score"]) for row in rows], dtype=float)
    tn = int(((y_true == 0) & (y_pred == 0)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    return {
        "num_samples": int(len(y_true)),
        "num_positive": int(y_true.sum()),
        "num_negative": int((y_true == 0).sum()),
        "auroc": float(roc_auc_score(y_true, y_score)),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "sensitivity": float(tp / (tp + fn)) if (tp + fn) else 0.0,
        "specificity": float(tn / (tn + fp)) if (tn + fp) else 0.0,
        "confusion_matrix": [[tn, fp], [fn, tp]],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-family", default="swarm")
    parser.add_argument("--generated-model", action="store_true")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--institution", required=True)
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--config", default="unilateral")
    parser.add_argument("--model-name", default="MST")
    parser.add_argument("--scratch-dir", default="/tmp")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--positive-class", type=int, default=2)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--output-json", required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    _add_mediswarm_paths()
    os.environ["DATA_DIR"] = args.data_root
    os.environ["SCRATCH_DIR"] = args.scratch_dir
    os.environ["SITE_NAME"] = args.institution
    os.environ["INSTITUTION"] = args.institution
    os.environ["CONFIG"] = args.config
    os.environ["MODEL_NAME"] = args.model_name

    from data.datasets import ODELIA_Dataset3D

    try:
        from models_config import create_model
    except ImportError:
        create_model = None
        from models import MST

    checkpoint = Path(args.checkpoint).expanduser().resolve()
    checkpoint_md5 = _file_md5(checkpoint)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type != "cuda":
        raise RuntimeError("CUDA GPU is required for MediSwarm MST export.")

    dataset = ODELIA_Dataset3D(
        path_root=args.data_root,
        institutions=args.institution,
        split=args.split,
        config=args.config,
    )
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True,
        drop_last=False,
    )
    if create_model is not None:
        model = create_model(logger=LOGGER, model_name=args.model_name, num_classes=3)
    elif args.model_name == "MST":
        model = MST(n_input_channels=1, num_classes=3, spatial_dims=3)
    else:
        raise ValueError(f"Unsupported model without models_config: {args.model_name}")
    model = model.to(device)
    state_dict = _state_dict_from_checkpoint(checkpoint)
    compatible, ignored = _compatible_state_dict(model, state_dict)
    incompatible = model.load_state_dict(compatible, strict=False)
    model.eval()

    rows: list[dict[str, Any]] = []
    offset = 0
    with torch.no_grad():
        for batch in dataloader:
            source = batch["source"].to(device)
            target = batch["target"]
            logits = model(source)
            probs = model.logits2probabilities(logits).detach().cpu().numpy()
            preds = model.logits2labels(logits).detach().cpu().numpy()
            targets = np.atleast_1d(target.numpy().squeeze()).flatten()
            preds = np.atleast_1d(preds.squeeze()).flatten()
            uids = _batch_uids(batch, offset, len(targets))
            for idx, uid in enumerate(uids):
                target_class = int(targets[idx])
                pred_class = int(preds[idx])
                rows.append(
                    {
                        "model_id": args.model_id,
                        "model_family": args.model_family,
                        "generated_model": "true" if args.generated_model else "false",
                        "dataset": args.dataset,
                        "uid": uid,
                        "y_true": int(target_class == args.positive_class),
                        "y_score": float(probs[idx, args.positive_class]),
                        "y_pred": int(pred_class == args.positive_class),
                        "checkpoint_md5": checkpoint_md5,
                    }
                )
            offset += len(targets)

    metrics = _binary_metrics(rows)
    output_csv = Path(args.output_csv).expanduser().resolve()
    output_json = Path(args.output_json).expanduser().resolve()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(
            {
                "checkpoint": str(checkpoint),
                "checkpoint_md5": checkpoint_md5,
                "model_id": args.model_id,
                "model_family": args.model_family,
                "generated_model": bool(args.generated_model),
                "dataset": args.dataset,
                "data_root": str(Path(args.data_root).resolve()),
                "institution": args.institution,
                "split": args.split,
                "positive_class": args.positive_class,
                "metrics": metrics,
                "ignored_keys": ignored,
                "missing_keys": list(incompatible.missing_keys),
                "unexpected_keys": list(incompatible.unexpected_keys),
                "prediction_csv": str(output_csv),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"{args.model_id} {args.dataset}: md5={checkpoint_md5} "
        f"AUROC={metrics['auroc']:.4f} accuracy={metrics['accuracy']:.4f} "
        f"f1={metrics['f1']:.4f} sensitivity={metrics['sensitivity']:.4f} "
        f"specificity={metrics['specificity']:.4f}"
    )


if __name__ == "__main__":
    main()
