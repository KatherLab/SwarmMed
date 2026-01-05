import flare_adapter
import keras
import pandas as pd
import glob
import os
import numpy as np
from sklearn.preprocessing import StandardScaler
from dotenv import load_dotenv, find_dotenv

# Load environment variables from .env file
load_dotenv(find_dotenv())

# --- Import the new adapter ---

# --- 1. Data Loading Function ---


def load_data(data_dir):
    """
    Reads all CSV files from the directory and prepares them for Keras.
    """
    file_pattern = os.path.join(data_dir, "**", "*.csv")
    file_list = glob.glob(file_pattern, recursive=True)

    if not file_list:
        raise RuntimeError(f"No CSV files found in '{data_dir}' or its subdirectories.")

    print(f"Found {len(file_list)} CSV files in {data_dir}.")
    df_list = [pd.read_csv(f) for f in file_list]
    full_df = pd.concat(df_list, ignore_index=True)

    X = full_df.drop(columns=["patient_id", "diagnosis"]).values.astype("float32")
    y = full_df["diagnosis"].values.astype("float32").reshape(-1, 1)

    scaler = StandardScaler()
    X = scaler.fit_transform(X)

    return X, y


# --- 2. Model Definition ---


def create_model(input_dim):
    model = keras.Sequential(
        [
            keras.layers.Dense(64, activation="relu", input_shape=(input_dim,)),
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

            # 3. Send Results Back to Server via Adapter
            print("Training finished for round. Sending updates to server...")

            # Convert Keras weights to a dictionary for the adapter
            params_dict = {str(i): w for i, w in enumerate(model.get_weights())}

            flare_adapter.send_model(
                params=params_dict, metrics={"loss": float(avg_loss)}
            )


if __name__ == "__main__":
    main(project_id="default_project")
