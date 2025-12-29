import torch
import torch.nn as nn
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import confusion_matrix, roc_curve, auc, precision_recall_curve, average_precision_score
import seaborn as sns
import os
import glob

# --- 1. Define Model Architecture (Must match training.py) ---
class BioMedNet(nn.Module):
    def __init__(self, input_dim):
        super(BioMedNet, self).__init__()
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
    print("Starting Model Performance Visualization...")

    # --- 2. Load Data ---
    # We use the visualization context to get the path to the data
    try:
        data_dir = visualization.get_data_path("biomed_data")
        file_pattern = os.path.join(data_dir, "*.csv")
        file_list = glob.glob(file_pattern)
        
        if not file_list:
             # Fallback if specific folder structure differs
            data_dir = visualization.get_data_path("")
            file_pattern = os.path.join(data_dir, "**", "*.csv")
            file_list = glob.glob(file_pattern, recursive=True)

        if not file_list:
            print("No CSV data files found.")
            return

        print(f"Loading data from {len(file_list)} files...")
        df_list = [pd.read_csv(f) for f in file_list]
        full_df = pd.concat(df_list, ignore_index=True)
        
        # Preprocessing (Must match training.py)
        # Note: In a real scenario, we should load the saved scaler from training.
        # Here we approximate by fitting on the full dataset available.
        X = full_df.drop(columns=['patient_id', 'diagnosis']).values.astype('float32')
        y = full_df['diagnosis'].values.astype('float32').reshape(-1, 1)
        
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        
        X_tensor = torch.tensor(X_scaled)
        y_tensor = torch.tensor(y)
        
        input_dim = X.shape[1]
        
    except Exception as e:
        print(f"Error loading data: {e}")
        return

    # --- 3. Load Model ---
    try:
        # visualization.get_model() loads 'model.pt' from 'fl-client-1' by default
        # Ideally, we should check which client's model we want or if there's a global model.
        # For this example, we'll try the default.
        loaded_model_dict = visualization.get_model()
        
        model = BioMedNet(input_dim)
        
        # Handle different saving formats (state_dict vs full model)
        if isinstance(loaded_model_dict, dict):
             model.load_state_dict(loaded_model_dict)
        elif isinstance(loaded_model_dict, nn.Module):
             model = loaded_model_dict
        else:
             # Sometimes it might be the 'model' key inside a dict
             # Adjust based on how NVFlare/Adapter saves it.
             # Assuming standard state_dict for now based on training.py
             model.load_state_dict(loaded_model_dict)

        model.eval()
        print("Model loaded successfully.")
        
    except Exception as e:
        print(f"Error loading model: {e}")
        # Create a dummy model for demonstration if loading fails (so plots can still be generated for testing UI)
        # print("Creating dummy model for demonstration...")
        # model = BioMedNet(input_dim)
        return

    # --- 4. Inference ---
    with torch.no_grad():
        y_logits = model(X_tensor)
        y_probs = torch.sigmoid(y_logits).numpy()
        y_true = y_tensor.numpy()

    # --- 5. Generate Plots ---

    # Plot 1: Confusion Matrix
    try:
        y_pred = (y_probs > 0.5).astype(int)
        cm = confusion_matrix(y_true, y_pred)
        
        plt.figure(figsize=(8, 6))
        sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', cbar=False)
        plt.title('Confusion Matrix')
        plt.ylabel('True Label')
        plt.xlabel('Predicted Label')
        plt.tight_layout()
        
        visualization.save_plot("Confusion Matrix")
    except Exception as e:
        print(f"Error plotting Confusion Matrix: {e}")

    # Plot 2: ROC Curve
    try:
        fpr, tpr, _ = roc_curve(y_true, y_probs)
        roc_auc = auc(fpr, tpr)
        
        plt.figure(figsize=(8, 6))
        plt.plot(fpr, tpr, color='darkorange', lw=2, label=f'ROC curve (area = {roc_auc:.2f})')
        plt.plot([0, 1], [0, 1], color='navy', lw=2, linestyle='--')
        plt.xlim([0.0, 1.0])
        plt.ylim([0.0, 1.05])
        plt.xlabel('False Positive Rate')
        plt.ylabel('True Positive Rate')
        plt.title('Receiver Operating Characteristic (ROC)')
        plt.legend(loc="lower right")
        plt.tight_layout()
        
        visualization.save_plot("ROC Curve")
    except Exception as e:
        print(f"Error plotting ROC Curve: {e}")

    # Plot 3: Precision-Recall Curve
    try:
        precision, recall, _ = precision_recall_curve(y_true, y_probs)
        avg_precision = average_precision_score(y_true, y_probs)
        
        plt.figure(figsize=(8, 6))
        plt.plot(recall, precision, color='teal', lw=2, label=f'AP = {avg_precision:.2f}')
        plt.xlabel('Recall')
        plt.ylabel('Precision')
        plt.title('Precision-Recall Curve')
        plt.legend(loc="upper right")
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        
        visualization.save_plot("Precision-Recall Curve")
    except Exception as e:
        print(f"Error plotting Precision-Recall Curve: {e}")

    # Plot 4: Prediction Probability Histogram
    try:
        plt.figure(figsize=(8, 6))
        plt.hist(y_probs[y_true==0], bins=20, alpha=0.5, label='Healthy (0)', color='blue', edgecolor='black')
        plt.hist(y_probs[y_true==1], bins=20, alpha=0.5, label='Diagnosed (1)', color='red', edgecolor='black')
        plt.xlabel('Predicted Probability of Diagnosis')
        plt.ylabel('Count')
        plt.title('Prediction Probability Distribution')
        plt.legend(loc='upper center')
        plt.tight_layout()
        
        visualization.save_plot("Prediction Probabilities")
    except Exception as e:
        print(f"Error plotting Probability Histogram: {e}")

if __name__ == "__main__":
    main()
