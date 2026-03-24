#!/usr/bin/env python3
import os
import io
import gzip
import logging
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.utils.data as data
import pytorch_lightning as pl
import monai.networks.nets as nets
import nibabel as nib
import flare_adapter as flare

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =================================================================================
# 1. Model Definition (3D ResNet-101)
# =================================================================================

class BreastResNet101(pl.LightningModule):
    """3D ResNet-101 Classifier using MONAI backbone, aligned with training/models/resnet.py."""
    
    def __init__(self, num_classes: int = 3, lr: float = 1e-4):
        super().__init__()
        self.save_hyperparameters()
        
        # Define the 3D ResNet-101 model matching the exact configuration in _ResNet
        self.model = nets.resnet101(
            n_input_channels=1,
            spatial_dims=3,
            num_classes=num_classes,
            feed_forward=False,
            shortcut_type='B',
            bias_downsample=False,
            pretrained=True
        )
        # Custom FC layer for the specific task
        self.model.fc = nn.Linear(2048, num_classes)
        self.loss_fn = nn.CrossEntropyLoss()

    def forward(self, x):
        return self.model(x)

    def training_step(self, batch, batch_idx):
        x, y = batch['source'], batch['target'].long().view(-1)
        logits = self(x)
        loss = self.loss_fn(logits, y)
        acc = (logits.argmax(dim=1) == y).float().mean()
        self.log("train_loss", loss, prog_bar=True)
        self.log("train/ACC", acc, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch['source'], batch['target'].long().view(-1)
        logits = self(x)
        loss = self.loss_fn(logits, y)
        acc = (logits.argmax(dim=1) == y).float().mean()
        self.log("val_loss", loss, prog_bar=True)
        self.log("val/ACC", acc, prog_bar=True)
        return loss

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.hparams.lr)

    def logits2probabilities(self, logits):
        return torch.softmax(logits, dim=1)

# =================================================================================
# 2. Data Loading (Streaming & Discovery)
# =================================================================================

class FlareStreamingDataset(data.Dataset):
    """Dataset that loads breast MRI data directly from FlareDataFileSystem, aligned with ODELIA_Dataset3D."""
    
    def __init__(self, fs, metadata_dir, data_dir, split='train'):
        self.fs = fs
        self.data_dir = data_dir
        
        # Load metadata
        logger.info(f"Loading metadata from {metadata_dir}...")
        with fs.open(f"{metadata_dir}/annotation.csv", "r") as f:
            df_annot = pd.read_csv(f, dtype={'UID': str})
        
        with fs.open(f"{metadata_dir}/split.csv", "r") as f:
            df_split = pd.read_csv(f, dtype={'UID': str})
            
        # Filter for the requested split
        df_split = df_split[df_split['Split'] == split]
        self.df = df_split.merge(df_annot, on='UID', how='inner')
        
        logger.info(f"Dataset initialized for split '{split}' with {len(self.df)} samples.")

    def __len__(self):
        return len(self.df)

    def _load_nifti(self, path):
        """Reads NIfTI and applies preprocessing matching ODELIA_Dataset3D and ImageOrSubjectToTensor."""
        content = self.fs.read_bytes(path)
        # Handle .nii.gz
        if path.endswith('.gz'):
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as gf:
                content = gf.read()
        
        # Load with nibabel from memory
        fh = nib.FileHolder(fileobj=io.BytesIO(content))
        img = nib.Nifti1Image.from_file_map({'header': fh, 'image': fh})
        data = img.get_fdata()
        
        # Basic preprocessing: Add channel dim and convert to tensor
        data = torch.from_numpy(data).float().unsqueeze(0)
        
        # 1. Z-Normalization with percentile clipping (0.5, 99.5) and masking
        mask = (data > data.min()) & (data < data.max())
        if mask.any():
            masked_data = data[mask]
            # Clipping
            low = torch.quantile(masked_data, 0.005)
            high = torch.quantile(masked_data, 0.995)
            data = torch.clamp(data, low, high)
            
            # Standardization
            mean = data[mask].mean()
            std = data[mask].std()
            if std > 0:
                data = (data - mean) / std
        
        # 2. Standardize volume size to (1, 224, 224, 32)
        data = self._resize_volume(data, (224, 224, 32))
        
        # 3. Swap axes matching ImageOrSubjectToTensor: (C, H, W, D) -> (C, D, H, W)
        # Assuming NIfTI load gave (H, W, D) and unsqueeze(0) gave (C, H, W, D)
        return data.permute(0, 3, 1, 2)

    def _resize_volume(self, tensor, target_shape):
        """Simple center crop or pad."""
        c, h, w, d = tensor.shape
        th, tw, td = target_shape
        
        # Create empty target
        res = torch.zeros((c, th, tw, td))
        
        # Calculate indices
        sh = max(0, (h - th) // 2)
        sw = max(0, (w - tw) // 2)
        sd = max(0, (d - td) // 2)
        
        eh = min(h, sh + th)
        ew = min(w, sw + tw)
        ed = min(d, sd + td)
        
        # Calculate target indices if input is smaller than target
        tsh = max(0, (th - h) // 2)
        tsw = max(0, (tw - w) // 2)
        tsd = max(0, (td - d) // 2)
        
        source_slice = tensor[:, sh:eh, sw:ew, sd:ed]
        res[:, tsh:tsh+source_slice.shape[1], tsw:tsw+source_slice.shape[2], tsd:tsd+source_slice.shape[3]] = source_slice
        return res

    def __getitem__(self, index):
        item = self.df.iloc[index]
        uid = item['UID']
        
        # Path to unilateral subtraction image (Sub_1.nii.gz)
        img_path = f"{self.data_dir}/{uid}/Sub_1.nii.gz"
        
        try:
            img_tensor = self._load_nifti(img_path)
            # Use multi-class target [0, 1, 2] to match ODELIA_Dataset3D
            target = torch.tensor([item['Lesion']], dtype=torch.long)
            
            return {'uid': uid, 'source': img_tensor, 'target': target}
        except Exception as e:
            logger.error(f"Error loading sample {uid}: {e}")
            # Return a dummy sample to avoid crashing the whole batch
            return self.__getitem__((index + 1) % len(self))

def discover_folders(fs):
    """Uses logic from val.py to find the unilateral folders."""
    all_files = list(fs.manifest.keys())
    metadata_folder = None
    data_folder = None
    
    for file_path in all_files:
        if "metadata_unilateral" in file_path:
            metadata_folder = file_path.split("metadata_unilateral")[0] + "metadata_unilateral"
        if "data_unilateral" in file_path:
            data_folder = file_path.split("data_unilateral")[0] + "data_unilateral"
            
    return metadata_folder, data_folder

# =================================================================================
# 3. Main Training Execution
# =================================================================================

def main():
    # Initialize Flare Adapter
    flare.init()
    
    # Get the project filesystem
    project_id = os.environ.get("MEDSWARMHUB_PROJECT_ID", "default")
    fs = flare.get_data_filesystem(project_id)
    
    # Discover folders
    meta_dir, data_dir = discover_folders(fs)
    if not meta_dir or not data_dir:
        raise RuntimeError("Could not locate metadata_unilateral or data_unilateral folders in S3.")
    
    # Prepare Data
    ds_train = FlareStreamingDataset(fs, meta_dir, data_dir, split='train')
    ds_val = FlareStreamingDataset(fs, meta_dir, data_dir, split='val')
    
    train_loader = data.DataLoader(ds_train, batch_size=1, shuffle=True, num_workers=0)
    val_loader = data.DataLoader(ds_val, batch_size=1, shuffle=False, num_workers=0)
    
    # Prepare Model (3 classes: No, Benign, Malignant)
    model = BreastResNet101(num_classes=3)
    
    # Prepare Lightning Trainer
    trainer = pl.Trainer(
        max_epochs=1,
        accelerator="auto",
        precision="16-mixed" if torch.cuda.is_available() else 32,
        enable_checkpointing=False,
        logger=False
    )
    
    # CRITICAL: Patch the trainer for platform compatibility
    flare.lightning.patch(trainer)
    
    logger.info("Starting Federated Training Loop...")
    
    # Federated Learning Loop
    while flare.is_running():
        # 1. Receive the global model
        input_model = flare.receive()
        logger.info(f"Received global model for Round {input_model.current_round}")
        
        # 2. Update local model with global parameters
        if input_model.params:
            model.load_state_dict(input_model.params)
            
        # 3. Run local training (1 epoch per round)
        trainer.fit(model, train_loader, val_loader)
        
        # 4. Send updates back
        # The trainer.fit() already has our updated weights
        output_model = flare.FLModel(
            params=model.state_dict(),
            metrics={"val_loss": trainer.callback_metrics.get("val_loss", 0.0)}
        )
        flare.send(output_model)
        
    logger.info("Training complete.")

if __name__ == "__main__":
    main()
