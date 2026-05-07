import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import auc, confusion_matrix, roc_curve
from sklearn.preprocessing import StandardScaler


def unpack_linear_model(payload):
    """Decode packed sklearn linear weights from NVFlare."""
    if payload is None:
        return None

    packed = np.asarray(payload, dtype=np.float32).reshape(-1)
    if packed.size < 3:
        return None

    coef_rows = int(packed[0])
    coef_cols = int(packed[1])
    intercept_size = int(packed[2])
    if coef_rows <= 0 or coef_cols <= 0 or intercept_size <= 0:
        return None

    coef_size = coef_rows * coef_cols
    expected_size = 3 + coef_size + intercept_size
    if packed.size != expected_size:
        return None

    coef_start = 3
    coef_end = coef_start + coef_size
    coef = packed[coef_start:coef_end].reshape(coef_rows, coef_cols)
    intercept = packed[coef_end:].reshape(intercept_size)
    return {"coef": coef, "intercept": intercept}


def main():
    print("--- Starting Scikit-learn Model Performance Visualization ---")

    # --- 1. Load Actual Data ---
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
    y_true = full_df["diagnosis"].values.astype("float32")
    
    X_scaled = StandardScaler().fit_transform(X)
    print(f"Successfully loaded and preprocessed {len(full_df)} rows.")

    # --- 2. Initialize Model ---
    # We must match the training script's configuration
    model = SGDClassifier(loss="log_loss")

    # --- 3. Load Model Weights ---
    print("Loading trained model coefficients...")
    weights = visualization.get_model()
    if isinstance(weights, dict):
        decoded_weights = weights
    else:
        decoded_weights = unpack_linear_model(weights)

    if (
        decoded_weights
        and "coef" in decoded_weights
        and "intercept" in decoded_weights
    ):
        model.coef_ = decoded_weights["coef"]
        model.intercept_ = decoded_weights["intercept"]
        # Required for some sklearn methods after manual attribute setting
        model.classes_ = np.unique(y_true)
        print("Model coefficients loaded successfully.")
    else:
        raise ValueError("Invalid weights format for Scikit-learn.")

    # --- 4. Generate Probabilities ---
    print("Running inference on actual data...")
    y_probs = model.predict_proba(X_scaled)[:, 1]

    # --- 5. Generate Plots ---
    sns.set_theme(style="whitegrid")

    # Confusion Matrix
    plt.figure(figsize=(8, 6))
    cm = confusion_matrix(y_true, (y_probs > 0.5).astype(int))
    sns.heatmap(cm, annot=True, fmt="d", cmap="Greens", cbar=False)
    plt.title("Scikit-learn: Confusion Matrix")
    plt.tight_layout()
    visualization.save_plot("Confusion Matrix")

    # ROC Curve
    plt.figure(figsize=(8, 6))
    fpr, tpr, _ = roc_curve(y_true, y_probs)
    plt.plot(fpr, tpr, color="darkgreen", lw=2, label=f"AUC = {auc(fpr, tpr):.2f}")
    plt.plot([0, 1], [0, 1], color="gray", lw=2, linestyle="--")
    plt.title("Scikit-learn: ROC Curve")
    plt.legend(loc="lower right")
    plt.tight_layout()
    visualization.save_plot("ROC Curve")

    print("--- Visualization Script Completed ---")

if __name__ == "__main__":
    main()
