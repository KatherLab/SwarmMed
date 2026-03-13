import os

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
import torch
import torch.nn as nn
from sklearn.metrics import (
    auc,
    average_precision_score,
    confusion_matrix,
    precision_recall_curve,
    roc_curve,
)
from sklearn.preprocessing import StandardScaler


# --- 1. Define Model Architecture (Must match training.py) ---


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


def main():
    print("--- Starting Model Performance Visualization ---")

    input_dim = 10  # Default for Biomed dataset
    X_tensor = None
    y_true = None
    y_probs = None

    # --- 2. Load Actual Data ---
    print("Loading data from project filesystem...")
    df_list = []
    for folder in ["biomed_data/", ""]:
        try:
            files = visualization.listdir(folder)
            csv_files = [f for f in files if f.endswith(".csv")]
            for csv_file in csv_files:
                with visualization.open(
                    os.path.join(folder, csv_file), "r"
                ) as f:
                    df_list.append(pd.read_csv(f))
            if df_list:
                break
        except BaseException:
            pass

    if not df_list:
        raise RuntimeError(
            "No actual data found in project filesystem. Cannot proceed with visualization."
        )

    full_df = pd.concat(df_list, ignore_index=True)
    if "diagnosis" not in full_df.columns:
        raise KeyError("Target column 'diagnosis' not found in loaded data.")

    X = full_df.drop(
        columns=["patient_id", "diagnosis"], errors="ignore"
    ).values.astype("float32")
    y_true = full_df["diagnosis"].values.astype("float32").reshape(-1, 1)
    input_dim = X.shape[1]
    X_tensor = torch.tensor(StandardScaler().fit_transform(X))
    print(f"Successfully loaded and preprocessed {len(full_df)} rows.")

    # --- 3. Load Model ---
    model = BioMedNet(input_dim)
    print("Loading trained model weights...")
    # This will now raise an exception if loading fails
    visualization.load_weights(model)
    model.eval()
    print("Model weights loaded successfully.")

    # --- 4. Generate Probabilities ---
    print("Running inference on actual data...")
    with torch.no_grad():
        y_probs = torch.sigmoid(model(X_tensor)).numpy()

    # --- 5. Generate Plots ---
    sns.set_theme(style="whitegrid")

    # Confusion Matrix
    plt.figure(figsize=(8, 6))
    cm = confusion_matrix(y_true, (y_probs > 0.5).astype(int))
    sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", cbar=False)
    plt.title("Confusion Matrix")
    plt.tight_layout()
    visualization.save_plot("Confusion Matrix")

    # ROC Curve
    plt.figure(figsize=(8, 6))
    fpr, tpr, _ = roc_curve(y_true, y_probs)
    plt.plot(
        fpr, tpr, color="darkorange", lw=2, label=f"AUC = {auc(fpr, tpr):.2f}"
    )
    plt.plot([0, 1], [0, 1], color="navy", lw=2, linestyle="--")
    plt.title("ROC Curve")
    plt.legend(loc="lower right")
    plt.tight_layout()
    visualization.save_plot("ROC Curve")

    # Precision-Recall
    plt.figure(figsize=(8, 6))
    precision, recall, _ = precision_recall_curve(y_true, y_probs)
    plt.plot(
        recall,
        precision,
        color="teal",
        lw=2,
        label=f"AP = {average_precision_score(y_true, y_probs):.2f}",
    )
    plt.title("Precision-Recall Curve")
    plt.legend(loc="upper right")
    plt.tight_layout()
    visualization.save_plot("Precision-Recall")

    # Confidence Scores
    plt.figure(figsize=(8, 6))
    plt.hist(
        y_probs[y_true == 0], bins=15, alpha=0.5, label="Healthy", color="blue"
    )
    plt.hist(
        y_probs[y_true == 1],
        bins=15,
        alpha=0.5,
        label="Diagnosed",
        color="red",
    )
    plt.title("Confidence Distribution")
    plt.legend()
    plt.tight_layout()
    visualization.save_plot("Confidence Scores")

    print("--- Visualization Script Completed ---")


if __name__ == "__main__":
    main()
