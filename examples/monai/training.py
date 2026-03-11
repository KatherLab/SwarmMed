import os
import glob
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
import numpy as np
from monai.networks.nets import DenseNet121
from monai.transforms import Compose, AddChannel, ScaleIntensity, ToTensor, RandRotate, RandFlip
import flare_adapter

# --- 1. Dataset Class (Simulated Medical Imaging) ---

class MedicalImageDataset(Dataset):
    def __init__(self, data_dir, transform=None):
        # In a real scenario, we'd look for NIfTI or DICOM files
        # For this example, we'll simulate finding data
        self.data_dir = data_dir
        self.transform = transform
        
        # Simulate 100 images
        self.images = [np.random.rand(64, 64).astype(np.float32) for _ in range(100)]
        self.labels = [np.random.randint(0, 2) for _ in range(100)]

    def __len__(self):
        return len(self.images)

    def __getitem__(self, idx):
        img = self.images[idx]
        label = self.labels[idx]
        
        if self.transform:
            img = self.transform(img)
            
        return img, torch.tensor([label], dtype=torch.float32)

# --- 2. Main Training Function ---

def main(project_id: str):
    flare_adapter.init_flare()

    # MONAI Transforms
    train_transforms = Compose([
        AddChannel(),
        ScaleIntensity(),
        RandRotate(range_x=15, prob=0.5),
        RandFlip(spatial_axis=0, prob=0.5),
        ToTensor()
    ])

    with flare_adapter.get_data_filesystem(project_id) as fs:
        data_dir = fs.get_data_path()
        dataset = MedicalImageDataset(data_dir, transform=train_transforms)
        train_loader = DataLoader(dataset, batch_size=4, shuffle=True)

        # Initialize MONAI DenseNet
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = DenseNet121(spatial_dims=2, in_channels=1, out_channels=1).to(device)
        
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-4)
        criterion = torch.nn.BCEWithLogitsLoss()

        # Swarm Loop
        while True:
            input_model = flare_adapter.receive_model()
            if input_model is None:
                break

            if input_model.params:
                state_dict = flare_adapter.get_pytorch_state_dict(input_model.params)
                model.load_state_dict(state_dict)

            model.train()
            epoch_loss = 0
            for batch_data in train_loader:
                inputs, labels = batch_data[0].to(device), batch_data[1].to(device)
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, labels)
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item()

            # Send back to server
            flare_adapter.send_model(
                params=model.state_dict(),
                metrics={"loss": epoch_loss / len(train_loader)}
            )

if __name__ == "__main__":
    main(project_id="default_project")
