"""DUKE MST training entrypoint for the SwarmCloud `swarmed` CLI.

This script is intentionally thin: it uses NVFlare's PyTorch Lightning client
API while reusing the MediSwarm MST/data stack from the runtime image
(`jefftud/odelia:<version>`).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import nvflare.client as flare_util
import nvflare.client.lightning as flare
import torch  # lets SwarmCloud select the PyTorch FLARE persistor.

SWARM_ROUNDS = 20


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        raise RuntimeError(
            "SITE_NAME or INSTITUTION must be set for DUKE MST training."
        )

    data_dir = os.getenv("DATA_DIR") or os.getenv("DATADIR")
    if not data_dir:
        raise RuntimeError(
            "DATA_DIR or DATADIR must point to the local DUKE_iid root."
        )

    scratch_dir = os.getenv("SCRATCH_DIR") or os.getenv("SCRATCHDIR")
    if not scratch_dir:
        raise RuntimeError(
            "SCRATCH_DIR or SCRATCHDIR must point to writable local storage."
        )

    os.environ["SITE_NAME"] = site_name
    os.environ.setdefault("INSTITUTION", site_name)
    os.environ["DATA_DIR"] = data_dir
    os.environ.setdefault("DATADIR", data_dir)
    os.environ["SCRATCH_DIR"] = scratch_dir
    os.environ.setdefault("SCRATCHDIR", scratch_dir)
    os.environ.setdefault("MODEL_NAME", "MST")
    os.environ.setdefault("CONFIG", "unilateral")
    os.environ.setdefault("TRAINING_MODE", "swarm")
    os.environ.setdefault("EPOCHS_PER_ROUND", "5")
    os.environ.setdefault("EPOCHS_MAX_CAP", "10")
    os.environ.setdefault("SWARMMEDHUB_MST_EXPORT_PREDICTIONS", "false")
    return site_name


def main(project_id: str = "default_project") -> None:
    """Run the DUKE MST swarm training loop through NVFlare Lightning."""
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

    flare_util.init(rank="0")
    flare_site_name = flare.get_site_name() or site_name

    logger = threedcnn_ptl.set_up_logging()
    logger.info(
        "Starting DUKE MST CLI training for site=%s flare_site=%s",
        site_name,
        flare_site_name,
    )

    data_module, model, checkpointing, trainer, path_run_dir, env_vars = (
        threedcnn_ptl.prepare_training(
            logger,
            max_epochs=1,
            site_name=site_name,
            log_dataset_details="LOG_DATASET_DETAILS" in os.environ,
            model_variant=os.environ.get("MODEL_NAME", "MST"),
            weighted_epochs=True,
        )
    )

    flare.patch(trainer, load_state_dict_strict=False)
    torch.autograd.set_detect_anomaly(True)
    export_predictions = _env_flag("SWARMMEDHUB_MST_EXPORT_PREDICTIONS")
    logger.info("Per-round prediction CSV export enabled: %s", export_predictions)

    while flare.is_running():
        input_model = flare.receive()
        logger.info(
            "Received swarm model for round %s",
            getattr(input_model, "current_round", "?"),
        )
        threedcnn_ptl.validate_and_train(
            logger,
            data_module,
            model,
            trainer,
            path_run_dir,
            output_GT_and_classprob=export_predictions,
        )

    threedcnn_ptl.finalize_training(
        logger, model, checkpointing, trainer, path_run_dir, env_vars
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main(project_id="default_project")
