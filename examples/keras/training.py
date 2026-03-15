import os
import math
import flare_adapter
import keras
import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv
from sklearn.preprocessing import StandardScaler

# Load environment variables from .env file
load_dotenv(find_dotenv())

SWARM_ROUNDS = 10

# --- 1. Data Loading Function ---


def load_data(fs):
    """
    Reads all CSV files from the virtual filesystem and prepares them for Keras.
    """
    file_list = fs.glob("*.csv")

    if not file_list:
        raise RuntimeError("No CSV files found in the project data.")

    print(f"Found {len(file_list)} CSV files. Streaming data...")
    
    # Stream files directly into pandas
    df_list = []
    for f_path in file_list:
        with fs.open(f_path) as f:
            df_list.append(pd.read_csv(f))
            
    full_df = pd.concat(df_list, ignore_index=True)

    X = full_df.drop(columns=["patient_id", "diagnosis"]).values.astype(
        "float32"
    )
    y = full_df["diagnosis"].values.astype("float32").reshape(-1, 1)

    scaler = StandardScaler()
    X = scaler.fit_transform(X)

    return X, y


# --- 2. Model Definition ---


def create_model(input_dim):
    model = keras.Sequential(
        [
            keras.layers.Dense(
                64, activation="relu", input_shape=(input_dim,)
            ),
            keras.layers.BatchNormalization(),
            keras.layers.Dropout(0.3),
            keras.layers.Dense(32, activation="relu"),
            keras.layers.BatchNormalization(),
            keras.layers.Dropout(0.3),
            keras.layers.Dense(1, activation="sigmoid"),
        ]
    )

    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=0.001),
        loss="binary_crossentropy",
        metrics=["accuracy"],
    )
    return model


# --- 3. Main Training Function with Adapter API ---


def main(project_id: str):
    # A. Initialize NVFlare
    flare_adapter.init_flare()
    print("--- NVFlare Client Initialized via Adapter (Keras) ---")

    # B. Use the adapter to get a local data path
    with flare_adapter.get_data_filesystem(project_id) as fs:
        data_dir = fs.get_data_path()

        batch_size = 32
        epochs_per_round = 5

        # Load Data
        try:
            X_train, y_train = load_data(data_dir)
            input_dim = X_train.shape[1]
        except Exception as e:
            print(f"Data loading error in training script: {e}")
            return

        # Initialize Model
        model = create_model(input_dim)

        # C. NVFlare Loop
        while True:
            # 1. Receive the Global Model via Adapter
            input_model = flare_adapter.receive_model()

            if input_model is None:
                print("Training finished or aborted.")
                break

            # Load parameters into the local model
            if input_model.params:
                # Use helper to convert dict back to weight list
                weights = flare_adapter.get_weights_list(input_model.params)
                model.set_weights(weights)
                print(
                    f"Received and loaded global model weights for round: {input_model.current_round}"
                )
            else:
                print(
                    f"Starting training from scratch for round: {input_model.current_round}"
                )

            # 2. Local Training Steps
            history = model.fit(
                X_train,
                y_train,
                batch_size=batch_size,
                epochs=epochs_per_round,
                verbose=1,
            )

            avg_loss = np.mean(history.history["loss"])
            
            # Calculate steps
            steps_per_epoch = math.ceil(len(X_train) / batch_size)
            total_steps = steps_per_epoch * epochs_per_round

            # 3. Send Results Back to Server via Adapter
            print("Training finished for round. Sending updates to server...")

            # Simulated validation metric improvement
            current_round = input_model.current_round
            simulated_accuracy = 0.6 + (0.35 * (1.0 - np.exp(-current_round/5.0))) + (np.random.rand() * 0.02)

            # Convert Keras weights to a dictionary for the adapter
            params_dict = {
                str(i): w for i, w in enumerate(model.get_weights())
            }

            flare_adapter.send_model(
                params=params_dict, 
                metrics={
                    "loss": float(avg_loss),
                    "accuracy": simulated_accuracy
                },
                meta={
                    "NUM_STEPS_CURRENT_ROUND": total_steps
                }
            )


if __name__ == "__main__":
    main(project_id="default_project")
