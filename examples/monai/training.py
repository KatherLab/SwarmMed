import glob
import os

import flare_adapter
import numpy as np
import torch
from monai.data import partition_dataset
from monai.metrics import DiceMetric
from monai.networks.nets import DenseNet121
from monai.transforms import (
    Compose,
    EnsureChannelFirst,
    RandFlip,
    RandRotate,
    ScaleIntensity,
    ToTensor,
)
from torch.utils.data import DataLoader, Dataset

# Configuration
SWARM_ROUNDS = 10
EPOCHS_PER_ROUND = 1
BATCH_SIZE = 4
LEARNING_RATE = 1e-4

# --- 1. Dataset Class (Enhanced for Data Discovery) ---

class MedicalImageDataset(Dataset):
    def __init__(self, data_dir, image_files=None, transform=None):
        self.data_dir = data_dir
        self.transform = transform
        
        if image_files is not None:
            self.image_files = image_files
        else:
            # Look for NIfTI or image files in the data directory
            self.image_files = glob.glob(os.path.join(data_dir, "**/*.nii.gz"), recursive=True)
            if not self.image_files:
                self.image_files = glob.glob(os.path.join(data_dir, "**/*.png"), recursive=True)
        
        if not self.image_files:
            print("training.py: No image files found in data_dir. Falling back to simulated data.")
            # Simulate 100 images if none found
            self.images = [np.random.rand(64, 64).astype(np.float32) for _ in range(100)]
            self.labels = [np.random.randint(0, 2) for _ in range(100)]
            self.is_simulated = True
        else:
            print(f"training.py: Found {len(self.image_files)} image files.")
            self.is_simulated = False
            # In a real scenario, labels would be loaded from a CSV or filename
            self.labels = [np.random.randint(0, 2) for _ in range(len(self.image_files))]

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        if self.is_simulated:
            img = self.images[idx]
        else:
            # Here you would load the actual image file (e.g. using monai.transforms.LoadImage)
            # For this example, we still return random to stay compatible with placeholder transforms
            img = np.random.rand(64, 64).astype(np.float32)
            
        label = self.labels[idx]
        if self.transform:
            img = self.transform(img)
            
        return img, torch.tensor([label], dtype=torch.float32)

# --- 2. Main Training Function ---

def main(project_id: str):
    flare_adapter.init_flare()

    # MONAI Transforms
    train_transforms = Compose([
        EnsureChannelFirst(channel_dim='no_channel'),
        ScaleIntensity(),
        RandRotate(range_x=15, prob=0.5),
        RandFlip(spatial_axis=0, prob=0.5),
        ToTensor()
    ])
    
    val_transforms = Compose([
        EnsureChannelFirst(channel_dim='no_channel'),
        ScaleIntensity(),
        ToTensor()
    ])

    with flare_adapter.get_data_filesystem(project_id) as fs:
        data_dir = fs.get_data_path()
        
        # Discovery and Split
        full_dataset = MedicalImageDataset(data_dir)
        if len(full_dataset) > 10:
            train_indices, val_indices = partition_dataset(
                list(range(len(full_dataset))), 
                ratios=[0.8, 0.2], 
                shuffle=True
            )
            train_files = [full_dataset.image_files[i] for i in train_indices] if not full_dataset.is_simulated else None
            val_files = [full_dataset.image_files[i] for i in val_indices] if not full_dataset.is_simulated else None
            
            train_ds = MedicalImageDataset(data_dir, image_files=train_files, transform=train_transforms)
            val_ds = MedicalImageDataset(data_dir, image_files=val_files, transform=val_transforms)
        else:
            train_ds = full_dataset
            val_ds = full_dataset # Same for very small datasets

        train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True)
        val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False)

        # Initialize MONAI DenseNet
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = DenseNet121(spatial_dims=2, in_channels=1, out_channels=1).to(device)
        
        optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
        criterion = torch.nn.BCEWithLogitsLoss()
        DiceMetric(include_background=True, reduction="mean")

        # Swarm Loop
        while True:
            input_model = flare_adapter.receive_model()
            if input_model is None:
                break

            current_round = input_model.current_round
            if input_model.params:
                state_dict = flare_adapter.get_pytorch_state_dict(input_model.params)
                model.load_state_dict(state_dict)

            # --- Training Round ---
            model.train()
            total_loss = 0
            steps = 0
            for _epoch in range(EPOCHS_PER_ROUND):
                for batch_data in train_loader:
                    inputs, labels = batch_data[0].to(device), batch_data[1].to(device)
                    optimizer.zero_grad()
                    outputs = model(inputs)
                    loss = criterion(outputs, labels)
                    loss.backward()
                    optimizer.step()
                    total_loss += loss.item()
                    steps += 1
            
            avg_loss = total_loss / steps if steps > 0 else 0

            # --- Validation ---
            model.eval()
            val_correct = 0
            with torch.no_grad():
                for val_data in val_loader:
                    v_inputs, v_labels = val_data[0].to(device), val_data[1].to(device)
                    v_outputs = model(v_inputs)
                    # For classification, accuracy is easier to compute
                    preds = (torch.sigmoid(v_outputs) > 0.5).float()
                    val_correct += (preds == v_labels).sum().item()
            
            val_accuracy = val_correct / len(val_ds) if len(val_ds) > 0 else 0
            
            # Print status to client logs
            print(f"Round {current_round} | Loss: {avg_loss:.4f} | Val Accuracy: {val_accuracy:.4f}")

            # Send back to server
            # Note: Swarm aggregators often look for "accuracy" or "val_dice"
            flare_adapter.send_model(
                params=model.state_dict(),
                metrics={
                    "loss": avg_loss,
                    "accuracy": val_accuracy,
                    "val_accuracy": val_accuracy
                },
                meta={
                    "NUM_STEPS_CURRENT_ROUND": steps
                }
            )

if __name__ == "__main__":
    main(project_id="default_project")
