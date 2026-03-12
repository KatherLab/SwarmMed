import glob
import os

import numpy as np
import flare_adapter
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from dotenv import find_dotenv, load_dotenv
from sklearn.preprocessing import StandardScaler
from torch.utils.data import DataLoader, Dataset

# Load environment variables from .env file
load_dotenv(find_dotenv())

SWARM_ROUNDS = 5

# --- Import the new adapter ---

# --- 1. Dataset Class (reads from a local path) ---


class BiomedTabularDataset(Dataset):
    def __init__(self, data_dir):
        """
        Initializes the dataset from a local directory of CSV files.
        """
        file_pattern = os.path.join(data_dir, "**", "*.csv")
        file_list = glob.glob(file_pattern, recursive=True)

        if not file_list:
            raise RuntimeError(
                f"No CSV files found in '{data_dir}' or its subdirectories."
            )

        print(f"Found {len(file_list)} CSV files in {data_dir}.")
        df_list = [pd.read_csv(f) for f in file_list]
        self.full_df = pd.concat(df_list, ignore_index=True)

        self.X = self.full_df.drop(
            columns=["patient_id", "diagnosis"]
        ).values.astype("float32")
        self.y = (
            self.full_df["diagnosis"].values.astype("float32").reshape(-1, 1)
        )

        self.scaler = StandardScaler()
        self.X = self.scaler.fit_transform(self.X)

    def __len__(self):
        return len(self.full_df)

    def __getitem__(self, idx):
        return torch.tensor(self.X[idx]), torch.tensor(self.y[idx])


# --- 2. Model Definition (Standard PyTorch) ---


class BioMedNet(nn.Module):
    def __init__(self, input_dim):
        super().__init__()
        self.layer_1 = nn.Linear(input_dim, 64)
        self.batch_norm1 = nn.BatchNorm1d(64)
        self.layer_2 = nn.Linear(64, 32)
        self.batch_norm2 = nn.BatchNorm1d(32)
        self.layer_out = nn.Linear(32, 1)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p=0.3)

    def forward(self, x):
        x = self.layer_1(x)
        x = self.batch_norm1(x)
        x = self.relu(x)
        x = self.dropout(x)
        x = self.layer_2(x)
        x = self.batch_norm2(x)
        x = self.relu(x)
        x = self.dropout(x)
        x = self.layer_out(x)
        return x


# --- 3. Main Training Function with Adapter API ---


def main(project_id: str):
    # A. Initialize NVFlare
    flare_adapter.init_flare()
    print("--- NVFlare Client Initialized via Adapter ---")

    # B. Use the adapter to get a local data path
    with flare_adapter.get_data_filesystem(project_id) as fs:
        data_dir = (
            fs.get_data_path()
        )  # This downloads the data and returns a local path

        batch_size = 32
        lr = 0.001
        epochs_per_round = 5

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Load Data from the local path provided by the adapter
        try:
            dataset = BiomedTabularDataset(data_dir)
            train_loader = DataLoader(
                dataset, batch_size=batch_size, shuffle=True
            )
            input_dim = dataset.X.shape[1]
        except Exception as e:
            print(f"Data loading error in training script: {e}")
            return

        # Initialize Model
        model = BioMedNet(input_dim).to(device)
        criterion = nn.BCEWithLogitsLoss()
        optimizer = optim.Adam(model.parameters(), lr=lr)

        # C. NVFlare Loop
        while True:
            # 1. Receive the Global Model via Adapter
            input_model = flare_adapter.receive_model()

            if input_model is None:
                print("Training finished or aborted.")
                break

            # Manually load parameters into the local model
            if input_model.params:
                # Use helper to convert dict back to Tensors (handles received
                # NumPy arrays)
                state_dict = flare_adapter.get_pytorch_state_dict(
                    input_model.params
                )
                model.load_state_dict(state_dict)
                print(
                    f"Received and loaded global model for round: {input_model.current_round}"
                )
            else:
                print(
                    f"Starting training from scratch for round: {input_model.current_round}"
                )

            # 2. Local Training Steps
            model.train()
            total_loss = 0.0
            steps = 0

            for epoch in range(epochs_per_round):
                epoch_loss = 0
                for X_batch, y_batch in train_loader:
                    X_batch, y_batch = X_batch.to(device), y_batch.to(device)

                    optimizer.zero_grad()
                    output = model(X_batch)
                    loss = criterion(output, y_batch)
                    loss.backward()
                    optimizer.step()

                    epoch_loss += loss.item()
                    steps += 1

                avg_epoch_loss = epoch_loss / len(train_loader)
                print(
                    f" Round {input_model.current_round} | Epoch {epoch + 1} | Loss: {avg_epoch_loss:.4f}"
                )
                total_loss += avg_epoch_loss

            # 3. Send Results Back to Server via Adapter
            print("Training finished for round. Sending updates to server...")

            # Simulated validation metric improvement
            current_round = input_model.current_round
            simulated_accuracy = 0.6 + (0.35 * (1.0 - np.exp(-current_round/5.0))) + (np.random.rand() * 0.02)

            # Manually extract parameters from the model as a dictionary
            flare_adapter.send_model(
                params=model.cpu().state_dict(),
                metrics={
                    "loss": total_loss / (epochs_per_round * len(train_loader)),
                    "accuracy": simulated_accuracy,
                },
                meta={
                    "NUM_STEPS_CURRENT_ROUND": steps
                }
            )

            model.to(device)


if __name__ == "__main__":
    # This script expects a project_id to be passed to main().
    # The view injects this, e.g., main(project_id="...")
    # Providing a default for local testing if needed.
    main(project_id="default_project")
