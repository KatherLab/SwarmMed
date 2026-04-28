"""DUKE MST training entrypoint for the SwarmCloud `swarmed` CLI.

This script is intentionally thin: it uses SwarmCloud's `flare_adapter` for
NVFlare client transport, while reusing the MediSwarm MST/data stack from the
runtime image (`jefftud/odelia:<version>`).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import flare_adapter
import torch  # noqa: F401 - lets SwarmCloud select the PyTorch FLARE persistor.


SWARM_ROUNDS = 20


def _add_mediswarm_paths() -> None:
    """Expose MediSwarm's shared custom modules when using the ODELIA image."""
    candidates = [
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path("/MediSwarm/application/jobs/_shared/custom"),
        Path("/workspace/app/custom"),
        Path("/app/application/jobs/_shared/custom"),
    ]
    for candidate in candidates:
        if candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


def _normalize_env() -> str:
    """Normalize env spellings used by SwarmCloud and older MediSwarm scripts."""
    site_name = os.getenv("SITE_NAME") or os.getenv("INSTITUTION")
    if not site_name:
        raise RuntimeError("SITE_NAME or INSTITUTION must be set for DUKE MST training.")

    data_dir = os.getenv("DATA_DIR") or os.getenv("DATADIR")
    if not data_dir:
        raise RuntimeError("DATA_DIR or DATADIR must point to the local DUKE_iid root.")

    scratch_dir = os.getenv("SCRATCH_DIR") or os.getenv("SCRATCHDIR")
    if not scratch_dir:
        raise RuntimeError("SCRATCH_DIR or SCRATCHDIR must point to writable local storage.")

    os.environ["SITE_NAME"] = site_name
    os.environ.setdefault("INSTITUTION", site_name)
    os.environ["DATA_DIR"] = data_dir
    os.environ.setdefault("DATADIR", data_dir)
    os.environ["SCRATCH_DIR"] = scratch_dir
    os.environ.setdefault("SCRATCHDIR", scratch_dir)
    os.environ.setdefault("MODEL_NAME", "MST")
    os.environ.setdefault("CONFIG", "unilateral")
    os.environ.setdefault("TRAINING_MODE", "swarm")
    return site_name


def _metric_value(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if hasattr(value, "detach"):
        return float(value.detach().cpu().item())
    if hasattr(value, "item"):
        return float(value.item())
    return float(value)


def main(project_id: str = "default_project") -> None:
    """Run one local training epoch per received swarm model."""
    _add_mediswarm_paths()
    site_name = _normalize_env()

    try:
        import threedcnn_ptl
    except ImportError as exc:
        raise RuntimeError(
            "Could not import MediSwarm's threedcnn_ptl module. Run with a "
            "MediSwarm/ODELIA runtime image, for example "
            "SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:<version>."
        ) from exc

    flare_adapter.init_flare()
    logger = threedcnn_ptl.set_up_logging()
    logger.info("Starting DUKE MST CLI training for site=%s", site_name)

    data_module, model, checkpointing, trainer, path_run_dir, env_vars = (
        threedcnn_ptl.prepare_training(
            logger,
            max_epochs=1,
            site_name=site_name,
            log_dataset_details="LOG_DATASET_DETAILS" in os.environ,
            model_variant="MST",
            weighted_epochs=True,
        )
    )

    while True:
        input_model = flare_adapter.receive_model()
        if input_model is None:
            break

        logger.info(
            "Received swarm model for round %s",
            getattr(input_model, "current_round", "?"),
        )
        if input_model.params:
            state_dict = flare_adapter.get_pytorch_state_dict(input_model.params)
            incompatible = model.load_state_dict(state_dict, strict=False)
            if incompatible.missing_keys or incompatible.unexpected_keys:
                logger.info(
                    "Loaded model with missing=%s unexpected=%s",
                    incompatible.missing_keys,
                    incompatible.unexpected_keys,
                )

        threedcnn_ptl.validate_and_train(
            logger, data_module, model, trainer, path_run_dir
        )

        callback_metrics = trainer.callback_metrics
        accuracy = _metric_value(
            callback_metrics.get("val/ACC")
            or callback_metrics.get("val_ACC")
            or callback_metrics.get("accuracy"),
            default=0.0,
        )
        loss = _metric_value(
            callback_metrics.get("train_loss")
            or callback_metrics.get("loss")
            or callback_metrics.get("val/loss"),
            default=0.0,
        )
        aggregation_weight = int(getattr(trainer, "global_step", 0) or 1)

        flare_adapter.send_model(
            params=model.state_dict(),
            metrics={"accuracy": accuracy, "loss": loss},
            meta={"NUM_STEPS_CURRENT_ROUND": aggregation_weight},
        )

    threedcnn_ptl.finalize_training(
        logger, model, checkpointing, trainer, path_run_dir, env_vars
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main(project_id="default_project")
