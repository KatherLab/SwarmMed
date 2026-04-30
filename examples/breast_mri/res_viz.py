import io
import gzip
import logging
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as data
import pytorch_lightning as pl
import monai.networks.nets as nets
import nibabel as nib
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import confusion_matrix, roc_curve, auc, classification_report

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =================================================================================
# 1. Model Definition (3D ResNet-101) - Must match training.py
# =================================================================================

class _ResNet(nn.Module):
    def __init__(self, n_input_channels: int, num_classes: int, spatial_dims: int):
        super().__init__()
        self.model = nets.resnet101(
            n_input_channels=n_input_channels,
            spatial_dims=spatial_dims,
            num_classes=num_classes,
            feed_forward=False,
            shortcut_type='B',
            bias_downsample=False,
            pretrained=True,
        )
        self.model.fc = nn.Linear(2048, num_classes)
        nn.init.xavier_normal_(self.model.fc.weight, gain=0.01)
        nn.init.zeros_(self.model.fc.bias)

    def forward(self, x):
        return self.model(x)

class BreastResNet101(pl.LightningModule):
    def __init__(self, num_classes: int = 3):
        super().__init__()
        self.model = _ResNet(
            n_input_channels=1,
            num_classes=num_classes,
            spatial_dims=3,
        )

    def forward(self, x):
        return self.model(x)

# =================================================================================
# 2. Data Loading (Adapted for Visualization Context)
# =================================================================================

class VizDataset(data.Dataset):
    def __init__(self, viz, metadata_dir, data_dir, split='val'):
        self.viz = viz
        self.data_dir = data_dir

        logger.info(f"Loading metadata from {metadata_dir}...")
        with viz.open(f"{metadata_dir}/annotation.csv", "r") as f:
            df_annot = pd.read_csv(f, dtype={'UID': str})

        with viz.open(f"{metadata_dir}/split.csv", "r") as f:
            df_split = pd.read_csv(f, dtype={'UID': str})

        df_split = df_split[df_split['Split'] == split]
        self.df = df_split.merge(df_annot, on='UID', how='inner')

        logger.info(f"Dataset initialized for split '{split}' with {len(self.df)} samples.")

    def __len__(self):
        return len(self.df)

    def _load_nifti(self, path):
        # Read from visualization context
        with self.viz.open(path, "rb") as f:
            content = f.read()

        if path.endswith('.gz') and content.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as gf:
                content = gf.read()

        fh = nib.FileHolder(fileobj=io.BytesIO(content))
        img = nib.Nifti1Image.from_file_map({'header': fh, 'image': fh})
        data = img.get_fdata()

        data = torch.from_numpy(data).float().unsqueeze(0)
        data = torch.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        data = data.flip(dims=[1, 2])
        data = self._resize_volume(data, (224, 224, 32))

        mask = (data > data.min()) & (data < data.max())
        if mask.any():
            masked_data = data[mask]
            low = torch.quantile(masked_data, 0.005)
            high = torch.quantile(masked_data, 0.995)
            data = torch.clamp(data, low, high)
            mean = data[mask].mean()
            std = data[mask].std()
            if std > 0:
                data = (data - mean) / std

        return data.permute(0, 3, 1, 2)

    def _resize_volume(self, tensor, target_shape):
        c, h, w, d = tensor.shape
        th, tw, td = target_shape
        res = torch.zeros((c, th, tw, td))
        sh, sw, sd = max(0, (h - th) // 2), max(0, (w - tw) // 2), max(0, (d - td) // 2)
        eh, ew, ed = min(h, sh + th), min(w, sw + tw), min(d, sd + td)
        tsh, tsw, tsd = max(0, (th - h) // 2), max(0, (tw - w) // 2), max(0, (td - d) // 2)
        source_slice = tensor[:, sh:eh, sw:ew, sd:ed]
        res[:, tsh:tsh+source_slice.shape[1], tsw:tsw+source_slice.shape[2], tsd:tsd+source_slice.shape[3]] = source_slice
        return res

    def __getitem__(self, index):
        item = self.df.iloc[index]
        uid = item['UID']
        img_path = f"{self.data_dir}/{uid}/Sub_1.nii.gz"
        try:
            img_tensor = self._load_nifti(img_path)
            target = item['Lesion']
            return {'uid': uid, 'source': img_tensor, 'target': target}
        except Exception as e:
            logger.error(f"Error loading sample {uid}: {e}")
            return self.__getitem__((index + 1) % len(self))

def discover_folders(viz):
    all_files = list(viz.filesystem.manifest.keys())
    metadata_folder, data_folder = None, None
    for file_path in all_files:
        parts = file_path.split('/')
        for i, part in enumerate(parts):
            if part == "data_unilateral":
                data_folder = "/".join(parts[:i+1])
            elif part == "metadata_unilateral":
                metadata_folder = "/".join(parts[:i+1])
    if data_folder and not metadata_folder:
        metadata_folder = data_folder
    return metadata_folder, data_folder

def main():
    print("--- Starting Breast MRI Result Visualization ---")
    sns.set_theme(style="whitegrid", context="talk")

    # 1. Discover data
    meta_dir, data_dir = discover_folders(visualization)
    if not meta_dir or not data_dir:
        raise RuntimeError("Could not locate metadata_unilateral or data_unilateral folders.")

    # 2. Load Model
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = BreastResNet101(num_classes=3).to(device)
    print("Loading trained model weights...")
    visualization.load_weights(model)
    model.eval()

    # 3. Load Validation Data
    ds_val = VizDataset(visualization, meta_dir, data_dir, split='val')
    val_loader = data.DataLoader(ds_val, batch_size=1, shuffle=False)

    all_preds = []
    all_targets = []
    all_probs = []

    print(f"Running inference on {len(ds_val)} validation samples...")
    with torch.no_grad():
        for batch in val_loader:
            source = batch['source'].to(device)
            target = batch['target'].to(device)
            
            outputs = model(source)
            probs = F.softmax(outputs, dim=1)
            preds = torch.argmax(probs, dim=1)
            
            all_preds.extend(preds.cpu().numpy())
            all_targets.extend(target.cpu().numpy())
            all_probs.extend(probs.cpu().numpy())

    all_preds = np.array(all_preds)
    all_targets = np.array(all_targets)
    all_probs = np.array(all_probs)

    # --- Plot 1: Confusion Matrix ---
    plt.figure(figsize=(10, 8))
    cm = confusion_matrix(all_targets, all_preds)
    labels = ["No Lesion", "Benign", "Malignant"]
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', xticklabels=labels, yticklabels=labels)
    plt.title("Confusion Matrix: Breast MRI Classification")
    plt.ylabel("Actual")
    plt.xlabel("Predicted")
    visualization.save_plot("Confusion Matrix")

    # --- Plot 2: ROC Curves (One-vs-Rest) ---
    plt.figure(figsize=(10, 8))
    for i in range(3):
        fpr, tpr, _ = roc_curve((all_targets == i).astype(int), all_probs[:, i])
        roc_auc = auc(fpr, tpr)
        plt.plot(fpr, tpr, lw=2, label=f'{labels[i]} (AUC = {roc_auc:.2f})')

    plt.plot([0, 1], [0, 1], color='gray', lw=2, linestyle='--')
    plt.xlim([0.0, 1.0])
    plt.ylim([0.0, 1.05])
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
    plt.title('Receiver Operating Characteristic (ROC) Curves')
    plt.legend(loc="lower right")
    visualization.save_plot("ROC Curves")

    # --- Plot 3: Precision-Recall (Summary) ---
    report = classification_report(all_targets, all_preds, target_names=labels, output_dict=True)
    df_report = pd.DataFrame(report).transpose().iloc[:3, :3] # Classes only, metrics only
    
    plt.figure(figsize=(10, 6))
    sns.heatmap(df_report, annot=True, cmap='YlGnBu')
    plt.title("Classification Metrics Summary")
    visualization.save_plot("Classification Metrics")

    # --- Plot 4: Example Prediction ---
    # Pick a random sample from the last batch processed
    plt.figure(figsize=(12, 6))
    img_slice = source[0, 0, :, :, 16].cpu().numpy() # Middle slice
    plt.imshow(img_slice, cmap='gray')
    actual_label = labels[all_targets[-1]]
    pred_label = labels[all_preds[-1]]
    prob_val = all_probs[-1, all_preds[-1]]
    
    plt.title(f"Sample MRI (Slice 16)\nActual: {actual_label} | Predicted: {pred_label} ({prob_val:.2f})")
    plt.axis('off')
    visualization.save_plot("Example Prediction")

    print("--- Breast MRI Result Visualization Completed ---")

if __name__ == "__main__":
    main()
