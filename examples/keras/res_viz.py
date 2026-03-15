import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import keras
from sklearn.metrics import confusion_matrix, roc_curve, auc, precision_recall_curve, average_precision_score
from sklearn.preprocessing import StandardScaler

# --- 1. Define Model Architecture (Must match training.py) ---

def create_model(input_dim):
    model = keras.Sequential([
        keras.layers.Dense(64, activation="relu", input_shape=(input_dim,)),
        keras.layers.BatchNormalization(),
        keras.layers.Dropout(0.3),
        keras.layers.Dense(32, activation="relu"),
        keras.layers.BatchNormalization(),
        keras.layers.Dropout(0.3),
        keras.layers.Dense(1, activation="sigmoid"),
    ])
    return model

def main():
    print("--- Starting Keras Model Performance Visualization ---")

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
    X_scaled = StandardScaler().fit_transform(X)
    print(f"Successfully loaded and preprocessed {len(full_df)} rows.")

    # --- 3. Load Model ---
    model = create_model(input_dim)
    print("Loading trained model weights...")
    visualization.load_weights(model)
    print("Model weights loaded successfully.")

    # --- 4. Generate Probabilities ---
    print("Running inference on actual data...")
    y_probs = model.predict(X_scaled)

    # --- 5. Generate Plots ---
    sns.set_theme(style="whitegrid")

    # Confusion Matrix
    plt.figure(figsize=(8, 6))
    cm = confusion_matrix(y_true, (y_probs > 0.5).astype(int))
    sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", cbar=False)
    plt.title("Keras: Confusion Matrix")
    plt.tight_layout()
    visualization.save_plot("Confusion Matrix")

    # ROC Curve
    plt.figure(figsize=(8, 6))
    fpr, tpr, _ = roc_curve(y_true, y_probs)
    plt.plot(fpr, tpr, color="darkorange", lw=2, label=f"AUC = {auc(fpr, tpr):.2f}")
    plt.plot([0, 1], [0, 1], color="navy", lw=2, linestyle="--")
    plt.title("Keras: ROC Curve")
    plt.legend(loc="lower right")
    plt.tight_layout()
    visualization.save_plot("ROC Curve")

    print("--- Visualization Script Completed ---")

if __name__ == "__main__":
    main()
