"""Resume checkpoints for SwarmMed swarm jobs.

This module is copied into the ``custom`` folder of every client app that
SwarmMed submits and runs inside the FLARE client job process, not in the Hub.
It may only import the standard library, NumPy, PyTorch and NVIDIA FLARE.

In a swarm job only the site that aggregates a round saves the new global model
(``FL_global_model.pt``), so after a failure the newest model is at one site.
:class:`ResumeCheckpointSaver` writes the model that every training site
receives at the start of each round, so that any site can resume the run.
"""

import json
import os
import time

from nvflare.apis.fl_component import FLComponent
from nvflare.apis.fl_constant import FLContextKey
from nvflare.apis.fl_context import FLContext
from nvflare.app_common.abstract.model import ModelLearnableKey
from nvflare.app_common.app_constant import AppConstants
from nvflare.app_common.app_event_type import AppEventType

# Keep in sync with training.resume, which reads these files on the Hub.
CHECKPOINT_FILE = "swarmmed_resume_checkpoint.pt"
METADATA_FILE = "swarmmed_resume_checkpoint.json"
METADATA_KEY = "swarmmed_resume"


class ResumeCheckpointSaver(FLComponent):
    """Save the global model a site receives at the start of each swarm round.

    The file holds the weights the round starts from, which are the result of
    the previous round, and the number of that round (``next_round``). A run
    resumed from it repeats the interrupted round. The file is overwritten each
    round; a failed write is logged and never stops training.
    """

    def __init__(self, checkpoint_file: str = CHECKPOINT_FILE):
        """Create the saver.

        Args:
            checkpoint_file: File name in the job's app folder.
        """
        super().__init__()
        self.checkpoint_file = checkpoint_file

    def handle_event(self, event_type: str, fl_ctx: FLContext):
        """Save the checkpoint when a round starts."""
        if event_type != AppEventType.ROUND_STARTED:
            return
        try:
            self._save(fl_ctx)
        except Exception as exc:  # a checkpoint must never break training
            self.log_warning(fl_ctx, f"Could not save the resume checkpoint: {exc}")

    def _save(self, fl_ctx: FLContext):
        learnable = fl_ctx.get_prop(AppConstants.GLOBAL_MODEL)
        if not isinstance(learnable, dict):
            return
        weights = learnable.get(ModelLearnableKey.WEIGHTS)
        if not weights:
            # The first round of a new run starts from an empty model.
            return
        current_round = fl_ctx.get_prop(AppConstants.CURRENT_ROUND)
        app_root = fl_ctx.get_prop(FLContextKey.APP_ROOT)
        if current_round is None or not app_root:
            return

        import numpy as np
        import torch

        weights_format = "torch"
        state = {}
        for name, value in weights.items():
            if isinstance(value, torch.Tensor):
                state[name] = value.detach().cpu()
            else:
                if isinstance(value, np.ndarray):
                    weights_format = "numpy"
                state[name] = torch.as_tensor(value)

        metadata = {
            "next_round": int(current_round),
            "job_id": str(fl_ctx.get_job_id() or ""),
            "site": str(fl_ctx.get_identity_name() or ""),
            "weights_format": weights_format,
            "num_tensors": len(state),
            "saved_at": time.time(),
        }

        checkpoint_path = os.path.join(app_root, self.checkpoint_file)
        temporary_path = checkpoint_path + ".tmp"
        torch.save({"model": state, METADATA_KEY: metadata}, temporary_path)
        os.replace(temporary_path, checkpoint_path)

        metadata_path = os.path.join(app_root, METADATA_FILE)
        with open(metadata_path + ".tmp", "w") as handle:
            json.dump(metadata, handle, indent=2)
        os.replace(metadata_path + ".tmp", metadata_path)

        self.log_info(
            fl_ctx,
            f"Saved the resume checkpoint for round {current_round}, "
            f"{len(state)} tensors",
        )
