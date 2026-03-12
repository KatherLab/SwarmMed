import glob
import os

import flare_adapter
import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import log_loss
from sklearn.preprocessing import StandardScaler

# Load environment variables from .env file
load_dotenv(find_dotenv())

SWARM_ROUNDS = 10

# --- Import the new adapter ---

# --- 1. Data Loading Function ---


def load_data(data_dir):
    """
    Reads all CSV files from the directory and prepares them for Scikit-learn.
    """
    file_pattern = os.path.join(data_dir, "**", "*.csv")
    file_list = glob.glob(file_pattern, recursive=True)

    if not file_list:
        raise RuntimeError(
            f"No CSV files found in '{data_dir}' or its subdirectories."
        )

    print(f"Found {len(file_list)} CSV files in {data_dir}.")
    df_list = [pd.read_csv(f) for f in file_list]
    full_df = pd.concat(df_list, ignore_index=True)

    X = full_df.drop(columns=["patient_id", "diagnosis"]).values.astype(
        "float32"
    )
    y = full_df["diagnosis"].values.astype("float32")  # 1D for sklearn

    scaler = StandardScaler()
    X = scaler.fit_transform(X)

    return X, y


# --- 2. Main Training Function with Adapter API ---


def main(project_id: str):
    # A. Initialize NVFlare
    flare_adapter.init_flare()
    print("--- NVFlare Client Initialized via Adapter (Scikit-learn) ---")

    # B. Use the adapter to get a local data path
    with flare_adapter.get_data_filesystem(project_id) as fs:
        data_dir = fs.get_data_path()

        # Initialize Model (using SGDClassifier for online/incremental
        # learning)
        model = SGDClassifier(
            loss="log_loss", learning_rate="constant", eta0=0.01
        )

        # Load Data
        try:
            X_train, y_train = load_data(data_dir)
            classes = np.unique(y_train)
        except Exception as e:
            print(f"Data loading error in training script: {e}")
            return

        # C. NVFlare Loop
        while True:
            # 1. Receive the Global Model via Adapter
            input_model = flare_adapter.receive_model()

            if input_model is None:
                print("Training finished or aborted.")
                break

            # Load parameters into the local model
            if input_model.params and "coef" in input_model.params and "intercept" in input_model.params:
                model.coef_ = input_model.params["coef"]
                model.intercept_ = input_model.params["intercept"]
                print(
                    f"Received and loaded global model weights for round: {input_model.current_round}"
                )
            else:
                print(
                    f"Starting training from scratch or with incompatible global model for round: {input_model.current_round}"
                )

            # 2. Local Training Steps (Incremental fit)
            # partial_fit allows training on batches or multiple times on the same data
            # Here we just do one pass over the local data per round
            model.partial_fit(X_train, y_train, classes=classes)

            # Calculate metrics
            y_prob = model.predict_proba(X_train)
            loss = log_loss(y_train, y_prob)
            print(f" Round {input_model.current_round} | Log Loss: {loss:.4f}")

            # 3. Send Results Back to Server via Adapter
            print("Training finished for round. Sending updates to server...")

            # Simulated validation metric improvement
            current_round = input_model.current_round
            simulated_accuracy = 0.6 + (0.35 * (1.0 - np.exp(-current_round/5.0))) + (np.random.rand() * 0.02)

            # Scikit-learn parameters are typically coef_ and intercept_
            params_dict = {"coef": model.coef_, "intercept": model.intercept_}

            flare_adapter.send_model(
                params=params_dict, 
                metrics={
                    "loss": float(loss),
                    "accuracy": simulated_accuracy
                },
                meta={
                    "NUM_STEPS_CURRENT_ROUND": len(X_train) # For sklearn partial_fit, one sample is one step
                }
            )


if __name__ == "__main__":
    main(project_id="default_project")
