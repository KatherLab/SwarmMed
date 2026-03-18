import os

import matplotlib.pyplot as plt
import pandas as pd
import pytorch_lightning as pl
import seaborn as sns
import torch
import torch.nn as nn
from sklearn.metrics import auc, confusion_matrix, roc_curve
from sklearn.preprocessing import StandardScaler

# --- 1. Define Model Architecture (Must match training.py) ---

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

    def forward(self, x):
        return self.model(x)

def main():
    print("--- Starting PyTorch Lightning Model Performance Visualization ---")

    # --- 2. Load Actual Data ---
    print("Loading data from project filesystem...")
    df_list = []
    for folder in ["biomed_data/", ""]:
        try:
            files = visualization.listdir(folder)
            csv_files = [f for f in files if f.endswith(".csv")]
            for csv_file in csv_files:
                with visualization.open(os.path.join(folder, csv_file), "r") as f:
                    df_list.append(pd.read_csv(f))
            if df_list: break
        except: pass

    if not df_list:
        raise RuntimeError("No actual data found in project filesystem.")

    full_df = pd.concat(df_list, ignore_index=True)
    X = full_df.drop(columns=["patient_id", "diagnosis"], errors="ignore").values.astype("float32")
    y_true = full_df["diagnosis"].values.astype("float32").reshape(-1, 1)
    
    input_dim = X.shape[1]
    X_tensor = torch.tensor(StandardScaler().fit_transform(X))
    print(f"Successfully loaded and preprocessed {len(full_df)} rows.")

    # --- 3. Load Model ---
    model = BioMedLightningModule(input_dim)
    print("Loading trained model weights...")
    # Using helper to handle state_dict conversion
    visualization.load_weights(model)
    model.eval()
    print("Model weights loaded successfully.")

    # --- 4. Generate Probabilities ---
    print("Running inference on actual data...")
    with torch.no_grad():
        logits = model(X_tensor)
        y_probs = torch.sigmoid(logits).numpy()

    # --- 5. Generate Plots ---
    sns.set_theme(style="whitegrid")

    # Confusion Matrix
    plt.figure(figsize=(8, 6))
    cm = confusion_matrix(y_true, (y_probs > 0.5).astype(int))
    sns.heatmap(cm, annot=True, fmt="d", cmap="Purples", cbar=False)
    plt.title("PyTorch Lightning: Confusion Matrix")
    plt.tight_layout()
    visualization.save_plot("Confusion Matrix")

    # ROC Curve
    plt.figure(figsize=(8, 6))
    fpr, tpr, _ = roc_curve(y_true, y_probs)
    plt.plot(fpr, tpr, color="purple", lw=2, label=f"AUC = {auc(fpr, tpr):.2f}")
    plt.plot([0, 1], [0, 1], color="gray", lw=2, linestyle="--")
    plt.title("PyTorch Lightning: ROC Curve")
    plt.legend(loc="lower right")
    plt.tight_layout()
    visualization.save_plot("ROC Curve")

    print("--- Visualization Script Completed ---")

if __name__ == "__main__":
    main()
