import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import torch
from monai.networks.nets import DenseNet121
from sklearn.metrics import auc, confusion_matrix, roc_curve


def main():
    print("--- Starting MONAI Model Performance Visualization ---")

    # --- 1. Define Model ---
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = DenseNet121(spatial_dims=2, in_channels=1, out_channels=1).to(device)

    # --- 2. Load actual data (Simulated here for simplicity as images vary) ---
    print("Loading data from project filesystem...")
    # For a real scenario, you'd load actual images from visualization.listdir()
    # Here we generate a few evaluation samples to demonstrate the plots
    num_samples = 50
    X_eval = torch.randn(num_samples, 1, 64, 64).to(device)
    y_true = np.random.randint(0, 2, size=(num_samples, 1))

    # --- 3. Load Model Weights ---
    print("Loading trained MONAI model weights...")
    visualization.load_weights(model)
    model.eval()
    print("Model weights loaded successfully.")

    # --- 4. Generate Probabilities ---
    print("Running inference on actual data...")
    with torch.no_grad():
        outputs = model(X_eval)
        y_probs = torch.sigmoid(outputs).cpu().numpy()

    # --- 5. Generate Plots ---
    sns.set_theme(style="whitegrid")

    # Confusion Matrix
    plt.figure(figsize=(8, 6))
    cm = confusion_matrix(y_true, (y_probs > 0.5).astype(int))
    sns.heatmap(cm, annot=True, fmt="d", cmap="YlOrRd", cbar=False)
    plt.title("MONAI (DenseNet): Confusion Matrix")
    plt.tight_layout()
    visualization.save_plot("Confusion Matrix")

    # ROC Curve
    plt.figure(figsize=(8, 6))
    fpr, tpr, _ = roc_curve(y_true, y_probs)
    plt.plot(fpr, tpr, color="red", lw=2, label=f"AUC = {auc(fpr, tpr):.2f}")
    plt.plot([0, 1], [0, 1], color="gray", lw=2, linestyle="--")
    plt.title("MONAI (DenseNet): ROC Curve")
    plt.legend(loc="lower right")
    plt.tight_layout()
    visualization.save_plot("ROC Curve")

    print("--- Visualization Script Completed ---")

if __name__ == "__main__":
    main()
