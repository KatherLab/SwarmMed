"""Grad-CAM and Grad-CAM++ for the MST DINOv2-ViT slice backbone.

Hooks the LayerNorms before the last *N* attention blocks, reshapes patch
tokens back to a ``(C, 16, 16)`` grid, lets pytorch-grad-cam upsample to the
slice resolution, and finally applies percentile clipping + a small Gaussian
blur for visual quality.

Multi-layer averaging (a la pytorch-grad-cam) and post-hoc smoothing follow
the conventions used in /home/jeff/Projects/odelia_breast_mri/scripts: stable
spatial signal, per-case (volume-wide) normalisation, no class collapse from
the very last block.
"""

from __future__ import annotations

import logging
from typing import Sequence

import numpy as np
import torch
from scipy.ndimage import gaussian_filter

from pytorch_grad_cam import GradCAM, GradCAMPlusPlus
from pytorch_grad_cam.utils.model_targets import ClassifierOutputTarget

LOGGER = logging.getLogger("duke_mst_explain.vit_cam")


class _SliceTargetSizeMixin:
    """Override pytorch-grad-cam's target-size inference for our 5D MST input.

    The library returns ``(depth, width, height)`` for 5D inputs which then
    crashes ``cv2.resize`` (expects a 2-tuple). Our CAM is computed per slice
    via the reshape_transform, so the per-slice target size is just
    ``(W, H)`` of the original input regardless of slice count.
    """

    def get_target_width_height(self, input_tensor):  # type: ignore[override]
        return input_tensor.shape[-1], input_tensor.shape[-2]


class _GradCAM5D(_SliceTargetSizeMixin, GradCAM):
    pass


class _GradCAMpp5D(_SliceTargetSizeMixin, GradCAMPlusPlus):
    pass


def _vit_reshape(tensor: torch.Tensor) -> torch.Tensor:
    """Reshape `(B*D, 257, C)` patch tokens (CLS + 16x16) → `(B*D, C, 16, 16)`."""
    if tensor.dim() != 3:
        raise ValueError(f"Unexpected ViT activation shape: {tuple(tensor.shape)}")
    n_tokens = tensor.shape[1]
    grid_tokens = n_tokens - 1
    side = int(round(grid_tokens ** 0.5))
    if side * side != grid_tokens:
        raise ValueError(f"Token count {n_tokens} not 1+square; got side={side}")
    patches = tensor[:, 1:, :]
    return patches.reshape(tensor.shape[0], side, side, -1).permute(0, 3, 1, 2).contiguous()


def _resolve_target_layers(model: torch.nn.Module, last_n: int = 4) -> list[torch.nn.Module]:
    """Return the LayerNorms before attention in the last ``last_n`` ViT blocks.

    Averaging CAMs over the last few blocks markedly cleans up DINOv2 ViT
    attribution, which is otherwise dominated by class-token sharpening at
    the very last block.
    """
    backbone = model.mst.backbone
    blocks = backbone.blocks
    if last_n < 1 or last_n > len(blocks):
        raise ValueError(f"last_n={last_n} out of range for {len(blocks)} blocks")
    targets: list[torch.nn.Module] = []
    for block in blocks[-last_n:]:
        if not hasattr(block, "norm1"):
            raise AttributeError("DINOv2 ViT block missing .norm1 — layout changed?")
        targets.append(block.norm1)
    return targets


def _percentile_clip(arr: np.ndarray, lo: float = 0.0, hi: float = 100.0) -> np.ndarray:
    """Clip to [percentile_lo, percentile_hi] then min-max to [0, 1] per-volume.

    Defaults (0, 100) are equivalent to plain min-max normalization. We avoid
    aggressive clipping because GradCAM/GradCAM++ peaks are typically narrow
    and clipping them flattens the very localisation we want to highlight.
    """
    arr = arr.astype(np.float32)
    if lo <= 0.0 and hi >= 100.0:
        p_lo = float(arr.min())
        p_hi = float(arr.max())
    else:
        p_lo, p_hi = np.percentile(arr, [lo, hi])
    if p_hi - p_lo < 1e-8:
        return np.zeros_like(arr)
    arr = np.clip(arr, p_lo, p_hi)
    return (arr - p_lo) / (p_hi - p_lo)


def _smooth(arr: np.ndarray, sigma: float = 1.5) -> np.ndarray:
    """Light per-slice Gaussian blur (sigma in voxels). Removes upsample blockiness."""
    if sigma <= 0:
        return arr
    return gaussian_filter(arr, sigma=(0, sigma, sigma))


def compute_cam_volume(
    model: torch.nn.Module,
    volume: torch.Tensor,
    *,
    method: str = "gradcam",
    target_class: int = 2,
    last_n_blocks: int = 1,
    percentile_lo: float = 0.0,
    percentile_hi: float = 100.0,
    smooth_sigma: float = 1.0,
) -> np.ndarray:
    """Compute a `(D, H, W)` CAM heatmap for a single MST input volume.

    Args:
        model: loaded MST classifier in eval() mode.
        volume: tensor of shape `(1, 1, D, H, W)` already on the model device.
        method: "gradcam" | "gradcam++".
        target_class: class index for the CAM target (2 = Malignant).
        last_n_blocks: number of trailing ViT blocks to average over.
        percentile_lo/hi: per-volume percentile clip before min-max normalisation.
        smooth_sigma: gaussian blur sigma in voxels (per-slice, H/W).
    """
    if volume.dim() != 5 or volume.shape[0] != 1:
        raise ValueError(f"Expected (1, 1, D, H, W); got {tuple(volume.shape)}")

    target_layers = _resolve_target_layers(model, last_n=last_n_blocks)
    cam_cls = _GradCAM5D if method == "gradcam" else _GradCAMpp5D
    targets = [ClassifierOutputTarget(target_class)]

    with cam_cls(model=model, target_layers=target_layers, reshape_transform=_vit_reshape) as cam:
        cam.batch_size = 1
        grayscale = cam(input_tensor=volume, targets=targets)

    depth = volume.shape[2]
    if grayscale.shape[0] != depth:
        raise RuntimeError(
            f"CAM returned {grayscale.shape[0]} maps but volume has {depth} slices"
        )

    grayscale = _percentile_clip(grayscale, lo=percentile_lo, hi=percentile_hi)
    grayscale = _smooth(grayscale, sigma=smooth_sigma)
    return grayscale  # (D, H, W) in [0, 1]


def available_methods() -> Sequence[str]:
    return ("gradcam", "gradcam++")
