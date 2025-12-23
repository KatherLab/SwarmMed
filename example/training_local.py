import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, random_split
import pandas as pd
import glob
import os
from sklearn.preprocessing import StandardScaler

# --- 1. Custom Dataset Class ---
class BiomedTabularDataset(Dataset):
    def __init__(self, data_dir):
        """
        Reads all CSV files from the directory and concatenates them.
        """
        file_list = glob.glob(os.path.join(data_dir, "*.csv"))
        
        if not file_list:
            raise RuntimeError(f"No CSV files found in {data_dir}")
            
        # Load and combine all files
        df_list = [pd.read_csv(f) for f in file_list]
        self.full_df = pd.concat(df_list, ignore_index=True)
        
        # Separate features and target
        # Dropping patient_id (not predictive) and diagnosis (target)
        self.X = self.full_df.drop(columns=['patient_id', 'diagnosis']).values.astype('float32')
        self.y = self.full_df['diagnosis'].values.astype('float32').reshape(-1, 1)
        
        # Standardize features (crucial for Neural Networks)
        self.scaler = StandardScaler()
        self.X = self.scaler.fit_transform(self.X)
        
    def __len__(self):
        return len(self.full_df)
    
    def __getitem__(self, idx):
        return torch.tensor(self.X[idx]), torch.tensor(self.y[idx])

# --- 2. Model Definition ---
class BioMedNet(nn.Module):
    def __init__(self, input_dim):
        super(BioMedNet, self).__init__()
        
        self.layer_1 = nn.Linear(input_dim, 64)
        self.batch_norm1 = nn.BatchNorm1d(64)
        self.layer_2 = nn.Linear(64, 32)
        self.batch_norm2 = nn.BatchNorm1d(32)
        self.layer_out = nn.Linear(32, 1)
        
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p=0.3) # Regularization
        
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

# --- 3. Training Setup ---
def train_model():
    # Parameters
    BATCH_SIZE = 32
    LEARNING_RATE = 0.001
    EPOCHS = 10
    DATA_DIR = 'biomed_data'
    
    # Check for GPU
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # Prepare Data
    dataset = BiomedTabularDataset(DATA_DIR)
    
    # 80/20 Train/Val split
    train_size = int(0.8 * len(dataset))
    val_size = len(dataset) - train_size
    train_dataset, val_dataset = random_split(dataset, [train_size, val_size])
    
    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False)
    
    # Initialize Model
    input_dim = dataset.X.shape[1] # Number of features
    model = BioMedNet(input_dim).to(device)
    
    # Loss and Optimizer
    criterion = nn.BCEWithLogitsLoss() # More stable than BCELoss + Sigmoid
    optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE)
    
    # --- 4. Training Loop ---
    for epoch in range(EPOCHS):
        model.train()
        train_loss = 0
        
        for X_batch, y_batch in train_loader:
            X_batch, y_batch = X_batch.to(device), y_batch.to(device)
            
            optimizer.zero_grad()
            y_pred = model(X_batch)
            loss = criterion(y_pred, y_batch)
            loss.backward()
            optimizer.step()
            
            train_loss += loss.item()
            
        # Validation
        model.eval()
        val_loss = 0
        correct = 0
        total = 0
        
        with torch.no_grad():
            for X_batch, y_batch in val_loader:
                X_batch, y_batch = X_batch.to(device), y_batch.to(device)
                y_pred_logits = model(X_batch)
                val_loss += criterion(y_pred_logits, y_batch).item()
                
                # Calculate accuracy
                probs = torch.sigmoid(y_pred_logits)
                predicted = (probs > 0.5).float()
                total += y_batch.size(0)
                correct += (predicted == y_batch).sum().item()
        
        print(f"Epoch {epoch+1}/{EPOCHS} | "
              f"Train Loss: {train_loss/len(train_loader):.4f} | "
              f"Val Loss: {val_loss/len(val_loader):.4f} | "
              f"Val Acc: {100 * correct / total:.2f}%")

if __name__ == "__main__":
    train_model()