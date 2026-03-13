import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import confusion_matrix, roc_curve, auc
from sklearn.preprocessing import StandardScaler

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
    if weights and "coef" in weights and "intercept" in weights:
        model.coef_ = weights["coef"]
        model.intercept_ = weights["intercept"]
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
