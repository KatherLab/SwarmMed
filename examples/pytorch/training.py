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

# --- 1. Dataset Class (Streaming via Adapter) ---


class BiomedTabularDataset(Dataset):
    def __init__(self, fs):
        """
        Initializes the dataset by streaming CSV files directly from the virtual filesystem.
        """
        file_list = fs.glob("*.csv")

        if not file_list:
            raise RuntimeError("No CSV files found in the project data.")

        print(f"Found {len(file_list)} CSV files. Streaming data...")
        
        # Stream files directly from fsspec into pandas
        df_list = []
        for f_path in file_list:
            print(f"BiomedTabularDataset: Loading {f_path}...")
            try:
                with fs.open(f_path) as f:
                    df = pd.read_csv(f)
                    print(f"BiomedTabularDataset: Successfully loaded {f_path} ({len(df)} rows).")
                    df_list.append(df)
            except Exception as e:
                print(f"BiomedTabularDataset: Error loading {f_path}: {e}")
                raise
        
        self.full_df = pd.concat(df_list, ignore_index=True)
        print(f"BiomedTabularDataset: Total rows loaded: {len(self.full_df)}")

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

    # B. Use the adapter to get a virtual streaming filesystem
    with flare_adapter.get_data_filesystem(project_id) as fs:
        batch_size = 32
        lr = 0.001
        epochs_per_round = 5

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # Load Data using the streaming filesystem
        try:
            dataset = BiomedTabularDataset(fs)
            train_loader = DataLoader(
                dataset, batch_size=batch_size, shuffle=True
            )
            input_dim = dataset.X.shape[1]
        except Exception as e:
            import traceback
            print(f"Data loading error: {e}")
            traceback.print_exc()
            return

        # Initialize Model
        model = BioMedNet(input_dim).to(device)
        criterion = nn.BCEWithLogitsLoss()
        optimizer = optim.Adam(model.parameters(), lr=lr)

        # C. NVFlare Loop
        print("Starting NVFlare training loop...")
        while True:
            # 1. Receive the Global Model via Adapter
            input_model = flare_adapter.receive_model()

            if input_model is None:
                print("Training finished or aborted.")
                break

            # Load parameters into the local model
            if input_model.params:
                state_dict = flare_adapter.get_pytorch_state_dict(
                    input_model.params
                )
                model.load_state_dict(state_dict)
                print(f"Round {input_model.current_round}: Global model loaded.")
            else:
                print(f"Round {input_model.current_round}: Starting from scratch.")

            # 2. Local Training Steps
            model.train()
            total_loss = 0.0
            steps = 0
            for epoch in range(epochs_per_round):
                for inputs, targets in train_loader:
                    inputs, targets = inputs.to(device), targets.to(device)
                    optimizer.zero_grad()
                    outputs = model(inputs)
                    loss = criterion(outputs, targets)
                    loss.backward()
                    optimizer.step()
                    total_loss += loss.item()
                    steps += 1
            
            avg_loss = total_loss / steps if steps > 0 else 0
            print(f"Round {input_model.current_round} complete. Avg Loss: {avg_loss:.4f}")

            # 3. Send Results Back to Server via Adapter
            flare_adapter.send_model(
                params=model.state_dict(),
                metrics={"loss": avg_loss},
                meta={"NUM_STEPS_CURRENT_ROUND": steps}
            )


if __name__ == "__main__":
    main(project_id="default_project")
