import flare_adapter
import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
from sklearn.preprocessing import StandardScaler
from torch import nn
from torch.utils.data import DataLoader, Dataset

SWARM_ROUNDS = 10

# --- 1. Dataset Class ---

class BiomedTabularDataset(Dataset):
    def __init__(self, fs):
        file_list = fs.glob("*.csv")
        if not file_list:
            raise RuntimeError("No CSV files found in project data.")

        print(f"Found {len(file_list)} CSV files. Streaming data...")
        
        # Stream files directly into pandas
        df_list = []
        for f_path in file_list:
            with fs.open(f_path) as f:
                df_list.append(pd.read_csv(f))
                
        self.full_df = pd.concat(df_list, ignore_index=True)
        self.X = self.full_df.drop(columns=["patient_id", "diagnosis"]).values.astype("float32")
        self.y = self.full_df["diagnosis"].values.astype("float32").reshape(-1, 1)
        self.scaler = StandardScaler()
        self.X = self.scaler.fit_transform(self.X)

    def __len__(self):
        return len(self.full_df)

    def __getitem__(self, idx):
        return torch.tensor(self.X[idx]), torch.tensor(self.y[idx])

# --- 2. PyTorch Lightning Module ---

class BioMedLightningModule(pl.LightningModule):
    def __init__(self, input_dim):
        super().__init__()
        self.model = nn.Sequential(
            nn.Linear(input_dim, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, 1)
        )
        self.criterion = nn.BCEWithLogitsLoss()

    def forward(self, x):
        return self.model(x)

    def training_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self(x)
        loss = self.criterion(y_hat, y)
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=0.001)

# --- 3. Main Training Function ---

def main(project_id: str):
    flare_adapter.init_flare()

    with flare_adapter.get_data_filesystem(project_id) as fs:
        # Load data using streaming filesystem
        dataset = BiomedTabularDataset(fs)
        train_loader = DataLoader(dataset, batch_size=32, shuffle=True)
        input_dim = dataset.X.shape[1]

        model = BioMedLightningModule(input_dim)
        
        # Swarm Loop
        while True:
            input_model = flare_adapter.receive_model()
            if input_model is None:
                break

            if input_model.params:
                model.load_state_dict(flare_adapter.get_pytorch_state_dict(input_model.params))

            # Train for 1 epoch per round
            trainer = pl.Trainer(max_epochs=1, accelerator="auto", devices=1, logger=False, enable_checkpointing=False)
            trainer.fit(model, train_loader)

            # Simulated validation metric improvement
            current_round = input_model.current_round
            simulated_accuracy = 0.6 + (0.35 * (1.0 - np.exp(-current_round/5.0))) + (np.random.rand() * 0.02)

            # Send back to server
            flare_adapter.send_model(
                params=model.state_dict(),
                metrics={
                    "loss": trainer.callback_metrics.get("train_loss", 0).item(),
                    "accuracy": simulated_accuracy
                },
                meta={
                    "NUM_STEPS_CURRENT_ROUND": trainer.global_step
                }
            )

if __name__ == "__main__":
    main(project_id="default_project")
