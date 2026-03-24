#!/usr/bin/env python3

import os
import torch

import flare_adapter as flare

import threedcnn_ptl

LOG_DATASET_DETAILS = 'LOG_DATASET_DETAILS' in os.environ

NUM_EPOCHS = 2
SWARM_ROUNDS = 5

def main():
    """
    Main function for training and evaluating the model using NVFlare and PyTorch Lightning.
    """
    logger = threedcnn_ptl.set_up_logging()
    
    flare.init()

    try:
        data_module, model, checkpointing, trainer, path_run_dir, env_vars = threedcnn_ptl.prepare_training(
            logger, NUM_EPOCHS, LOG_DATASET_DETAILS)

        flare.lightning.patch(trainer)  # Patch trainer to enable swarm learning
        torch.autograd.set_detect_anomaly(True)

        logger.info(f"Start Training")

        while flare.is_running():
            input_model = flare.receive()
            logger.info(f"Current round: {input_model.current_round}")
            threedcnn_ptl.validate_and_train(logger, data_module, model, trainer, path_run_dir)

        threedcnn_ptl.finalize_training(logger, model, checkpointing, trainer, path_run_dir, env_vars)

    except Exception as e:
        logger.error(f"Error in main function: {e}")
        raise

if __name__ == "__main__":
    main()
