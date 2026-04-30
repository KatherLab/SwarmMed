#!/usr/bin/env python3
"""
training.py — Platform-adapted MST (Multi-Slice Transformer) breast MRI classifier.

Strictly follows the ODELIA codebase logic including seeded sampling, 
validation during training, and early stopping.

Platform adaptations (# PLATFORM:):
  - Data streamed via flare_adapter from MinIO.
  - Federated learning loop via flare.lightning.patch.
  - bf16-mixed precision for stability.
"""

import os
import gc
import io
import gzip
import logging
import json
import math
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as data
import pytorch_lightning as pl
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from torchmetrics import AUROC, Accuracy
import torchio as tio
from torchio import Subject, Image
from torchio.types import TypeRangeFloat
from torchio.transforms.transform import TypeMaskingMethod
import nibabel as nib
import flare_adapter as flare
from typing import Union, Optional, Sequence
from pathlib import Path
from torch.utils.data.sampler import RandomSampler
from einops import rearrange
from transformers import Dinov2WithRegistersModel, DINOv3ViTModel
from x_transformers import Encoder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SWARM_ROUNDS = 20

# =============================================================================
# 1. Augmentations — Exact copy from ODELIA
# =============================================================================

class ImageOrSubjectToTensor(object):
    def __call__(self, input: Union[Image, Subject]):
        if isinstance(input, Subject):
            return {key: val.data.swapaxes(1,-1) if isinstance(val, Image) else val for key,val in input.items()}
        else:
            return input.data.swapaxes(1,-1)

def parse_per_channel(per_channel, channels):
    if isinstance(per_channel, bool):
        return [(ch,) for ch in range(channels)] if per_channel else [tuple(range(channels))]
    return per_channel 

class ZNormalization(tio.ZNormalization):
    def __init__(self, percentiles: TypeRangeFloat = (0, 100), per_channel=True, per_slice=False, masking_method=None, **kwargs):
        super().__init__(masking_method=masking_method, **kwargs)
        self.percentiles = percentiles; self.per_channel = per_channel; self.per_slice = per_slice

    def apply_normalization(self, subject: Subject, image_name: str, mask: torch.Tensor) -> None:
        image = subject[image_name]
        per_channel = parse_per_channel(self.per_channel, image.shape[0])
        per_slice = parse_per_channel(self.per_slice, image.shape[-1])
        image.set_data(torch.cat([torch.cat([self._znorm(image.data[chs,][:,:,:, sl,], mask[chs,][:,:,:, sl,], image_name, image.path) for sl in per_slice], dim=-1) for chs in per_channel ]))

    def _znorm(self, image_data, mask, image_name, image_path):
        cutoff = torch.quantile(image_data.masked_select(mask).float(), torch.tensor(self.percentiles)/100.0)
        torch.clamp(image_data, *cutoff.to(image_data.dtype).tolist(), out=image_data)
        standardized = self.znorm(image_data, mask)
        if standardized is None: raise RuntimeError(f'Std 0 in "{image_name}"')
        return standardized

class CropOrPad(tio.CropOrPad):
    def __init__(self, target_shape=None, padding_mode=0, mask_name=None, labels=None, random_center=False, **kwargs):
        super().__init__(target_shape=target_shape, padding_mode=padding_mode, mask_name=mask_name, labels=labels, **kwargs)
        self.random_center = random_center

    def _get_six_bounds_parameters(self, parameters: np.ndarray):
        result = []
        for number in parameters:
            ini = np.random.randint(low=0, high=number+1) if self.random_center else int(np.ceil(number/2))
            result.extend([ini, number-ini])
        return tuple(result)
    
    def apply_transform(self, subject: tio.Subject) -> tio.Subject:
        subject.check_consistent_space()
        padding_params, cropping_params = self.compute_crop_or_pad(subject)
        if padding_params is not None:
            if self.random_center:
                random_padding_params = []
                for i in range(0, len(padding_params), 2):
                    s = padding_params[i] + padding_params[i + 1]; r = np.random.randint(0, s+1)
                    random_padding_params.extend([r, s - r])
                padding_params = random_padding_params
            subject = tio.Pad(padding_params, padding_mode=self.padding_mode)(subject)
        if cropping_params is not None: subject = tio.Crop(cropping_params)(subject)
        return subject

# =============================================================================
# 2. Models — Exact copy from ODELIA
# =============================================================================

class VeryBasicModel(pl.LightningModule):
    def __init__(self, save_hyperparameters=True):
        super().__init__()
        if save_hyperparameters: self.save_hyperparameters()
        self._step_train = -1; self._step_val = -1; self._step_test = -1

    def training_step(self, batch, batch_idx): self._step_train += 1; return self._step(batch, batch_idx, "train", self._step_train)
    def validation_step(self, batch, batch_idx): self._step_val += 1; return self._step(batch, batch_idx, "val", self._step_val)
    def on_train_epoch_end(self): self._epoch_end("train")
    def on_validation_epoch_end(self): self._epoch_end("val")

class BasicClassifier(VeryBasicModel):
    def __init__(self, in_ch, out_ch, spatial_dims, task="binary", optimizer_kwargs={'lr':1e-4}, **kwargs):
        super().__init__()
        self.task = task; self.out_ch = out_ch; self.optimizer_kwargs = optimizer_kwargs
        self.loss = nn.BCEWithLogitsLoss()
        self.auc_roc = nn.ModuleDict({state:AUROC(task="binary") for state in ["train_", "val_", "test_"]})
        self.acc = nn.ModuleDict({state:Accuracy(task="binary") for state in ["train_", "val_", "test_"]})

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), **self.optimizer_kwargs)

    def _step(self, batch, batch_idx, state, step):
        source = batch['source']; target = batch['target']; batch_size = source.shape[0]
        pred = self(source); loss = self.loss(pred, target.float())
        with torch.no_grad():
            self.acc[state+"_"].update(pred, target); self.auc_roc[state+"_"].update(pred, target)
            self.log(f"{state}/loss", loss, batch_size=batch_size, on_step=True, on_epoch=True, prog_bar=True)
        return loss 

    def _epoch_end(self, state):
        metrics = {}
        for name, value in [("ACC", self.acc[state+"_"]), ("AUC_ROC", self.auc_roc[state+"_"])]:
            v = value.compute()
            self.log(f"{state}/{name}", v, on_epoch=True, sync_dist=True, prog_bar=True)
            metrics[name] = v.item()
            value.reset()
        print(f"  [{state}] Epoch {self.current_epoch}: " + ", ".join(f"{k}={v:.4f}" for k, v in metrics.items()))

class _MST(nn.Module):
    def __init__(self, out_ch=1, backbone_type="dinov2", model_size="s"):
        super().__init__()
        m_size = {'s':'small', 'b':'base', 'l':'large'}.get(model_size, 'small')
        self.backbone = Dinov2WithRegistersModel.from_pretrained(f"facebook/dinov2-with-registers-{m_size}")
        emb_ch = self.backbone.config.hidden_size
        self.slice_fusion = Encoder(dim=emb_ch, heads=12, depth=1, rotary_pos_emb=True, attn_flash=True, ff_mult=1, ff_no_bias=True)
        self.cls_token = nn.Parameter(torch.randn(1, 1, emb_ch)); self.linear = nn.Linear(emb_ch, out_ch)

    def forward(self, x):
        B, C, D, H, W = x.shape
        x_pad = torch.isclose(x.mean(dim=(-1,-2)), x[:, :, :, 0, 0]); x_pad = rearrange(x_pad, 'b c d -> b (c d)')
        x = rearrange(x, 'b c d h w -> (b c d) h w')[:, None].repeat(1, 3, 1, 1)
        x = self.backbone(x).pooler_output; x = rearrange(x, '(b d) e -> b d e', b=B)
        cls_pad = torch.zeros(B, 1, dtype=torch.bool, device=x.device); pad = torch.concat([x_pad, cls_pad], dim=1)
        x = torch.concat([x, self.cls_token.repeat(B, 1, 1)], dim=1)
        x = self.slice_fusion(x, mask=~pad)
        return self.linear(x[:, -1])

class MST(BasicClassifier):
    def __init__(self, backbone_type="dinov2", **kwargs):
        super().__init__(in_ch=1, out_ch=1, spatial_dims=3, **kwargs)
        self.mst = _MST(backbone_type=backbone_type)
    def forward(self, x): return self.mst(x)

# =============================================================================
# 3. DataModule — Adapted from ODELIA
# =============================================================================

class FlareStreamingDataset(data.Dataset):
    def __init__(self, fs, metadata_dir, data_dir, split='train', fold=0, random_flip=False, random_rotate=False, noise=False):
        self.fs, self.data_dir = fs, data_dir
        with fs.open(f"{metadata_dir}/annotation.csv", "r") as f: df_anno = pd.read_csv(f, dtype={'UID':str})
        with fs.open(f"{metadata_dir}/split.csv", "r") as f: df_split = pd.read_csv(f, dtype={'UID':str})
        df_split = df_split[(df_split['Fold'] == fold) & (df_split['Split'] == split)]
        self.df = df_split.merge(df_anno, on='UID', how='inner')
        self.transform = tio.Compose([
            tio.Lambda(lambda x: x), tio.Lambda(lambda x: x), tio.Flip((1,0)),
            CropOrPad((224, 224, 32), random_center=random_rotate),
            ZNormalization(per_channel=True, per_slice=False, masking_method=lambda x:(x>x.min()) & (x<x.max())),
            tio.OneOf([
                tio.RandomAffine(scales=0, degrees=(0, 0, 0, 0, 0,90), translation=0, isotropic=True, default_pad_value='minimum') if random_rotate else tio.Lambda(lambda x: x),
                tio.RandomFlip((0,1,2)) if random_flip else tio.Lambda(lambda x: x),
            ]),
            tio.RandomNoise(std=(0.0, 0.25)) if noise else tio.Lambda(lambda x: x),
            ImageOrSubjectToTensor()
        ])

    def __len__(self): return len(self.df)

    def __getitem__(self, index):
        item = self.df.iloc[index]; uid = item['UID']
        content = self.fs.read_bytes(f"{self.data_dir}/{uid}/Sub_1.nii.gz")
        if content.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as gf: content = gf.read()
        fh = nib.FileHolder(fileobj=io.BytesIO(content)); nii = nib.Nifti1Image.from_file_map({'header': fh, 'image': fh})
        img = self.transform(tio.ScalarImage(tensor=torch.from_numpy(nii.get_fdata()).float().unsqueeze(0), affine=nii.affine))
        return {'uid':uid, 'source': img, 'target':torch.tensor([int(item['Lesion'] == 2)])}

class DataModule(pl.LightningDataModule):
    def __init__(self, ds_train, ds_val, batch_size=2, seed=0):
        super().__init__()
        self.ds_train, self.ds_val, self.batch_size, self.seed = ds_train, ds_val, batch_size, seed

    def train_dataloader(self):
        gen = torch.Generator(); gen.manual_seed(self.seed)
        return data.DataLoader(self.ds_train, batch_size=self.batch_size, sampler=RandomSampler(self.ds_train, generator=gen), num_workers=0, pin_memory=True, drop_last=True)

    def val_dataloader(self):
        return data.DataLoader(self.ds_val, batch_size=self.batch_size, shuffle=False, num_workers=0, pin_memory=True)

# =============================================================================
# 4. Main
# =============================================================================

def main():
    torch.set_float32_matmul_precision('high'); flare.init()
    fs = flare.get_data_filesystem(os.environ.get("MEDSWARMHUB_PROJECT_ID", "default"))
    # Platform Discovery
    meta_dir = data_dir = None
    for p in fs.manifest.keys():
        parts = p.split("/")
        if "metadata_unilateral" in parts:
            idx = parts.index("metadata_unilateral")
            meta_dir = "/".join(parts[:idx+1])
        if "data_unilateral" in parts:
            idx = parts.index("data_unilateral")
            data_dir = "/".join(parts[:idx+1])
        if meta_dir and data_dir:
            break

    dm = DataModule(
        ds_train=FlareStreamingDataset(fs, meta_dir, data_dir, split='train', random_flip=True, random_rotate=True, noise=True),
        ds_val=FlareStreamingDataset(fs, meta_dir, data_dir, split='val')
    )

    model = MST(backbone_type="dinov2", optimizer_kwargs={'lr':1e-5})
    
    # Matching ODELIA Trainer Settings
    trainer = Trainer(
        accelerator='gpu', precision='bf16-mixed', max_epochs=1000, 
        enable_checkpointing=False, logger=False, check_val_every_n_epoch=1,
        accumulate_grad_batches=4,
        callbacks=[EarlyStopping(monitor="val/AUC_ROC", patience=25, mode="max")]
    )
    
    # FL Loop (Patched)
    flare.lightning.patch(trainer)
    while flare.is_running():
        input_model = flare.receive() # Required to maintain round state
        if input_model is None: break
        gc.collect(); torch.cuda.empty_cache()

        # Reset state for fresh training round
        trainer.should_stop = False
        trainer.fit_loop.epoch_progress.current.completed = 0
        trainer.fit_loop.epoch_progress.current.started = 0
        
        # Reset EarlyStopping (monitoring val/AUC_ROC)
        for cb in trainer.callbacks:
            if isinstance(cb, EarlyStopping):
                cb.wait_count = 0; cb.stopped_epoch = 0
                cb.best_score = torch.tensor(float('-inf'))
                cb._reduce_on_train_epoch_end = False # ensure it checks on val
        
        # Reset custom step counters
        model._step_train = -1; model._step_val = -1

        trainer.fit(model, datamodule=dm)

if __name__ == "__main__": main()
