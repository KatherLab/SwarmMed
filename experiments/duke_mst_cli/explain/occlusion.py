"""Occlusion Confidence Activation (OCA) for the MST volume classifier.

Slides a small occlusion patch through ``(D, H, W)`` and records the drop in
``softmax(logits)[Malignant]`` versus the unperturbed baseline. Differences vs
round 1:

- Smaller default patch (16) and stride (8) → roughly one DINOv2 patch token,
  fine enough to localise a single lesion (was 48/32, too coarse).
- ``fill_mode='mean_img'``: replace each occluded patch with its **own**
  local-window mean (the standard MONAI trick) instead of the per-volume mean.
  This avoids the boundary discontinuity that a flat-fill patch creates.
- 3D sweep with configurable ``slice_stride`` so the heatmap localises lesion
  position along the axial axis instead of treating each slice independently.
- Larger inference batch (16) — memory is dominated by the slice-fold inside
  MST, so batching the perturbations is essentially free on a 24 GB GPU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import torch

LOGGER = logging.getLogger("duke_mst_explain.occlusion")


@dataclass
class OcclusionConfig:
    patch_size: int = 16
    stride: int = 8
    slice_stride: int = 4  # step in the depth dimension for the 3D sweep
    target_class: int = 2
    fill_mode: str = "mean_img"  # "mean_img" | "mean" | "zero"
    batch_size: int = 16
    percentile_lo: float = 1.0
    percentile_hi: float = 99.0


def _baseline_prob(model: torch.nn.Module, volume: torch.Tensor, target_class: int) -> float:
    with torch.no_grad():
        logits = model(volume)
        probs = model.logits2probabilities(logits)
    return float(probs[0, target_class].item())


def _make_axis_positions(extent: int, patch: int, stride: int) -> list[int]:
    if patch >= extent:
        return [0]
    positions = list(range(0, extent - patch + 1, stride))
    if positions[-1] + patch < extent:
        positions.append(extent - patch)
    return positions


def _percentile_normalise(score: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Clip negatives to 0, percentile-clip, then min-max → [0, 1]."""
    score = np.clip(score, a_min=0.0, a_max=None)
    p_lo, p_hi = np.percentile(score, [lo, hi])
    if p_hi - p_lo < 1e-8:
        return np.zeros_like(score)
    score = np.clip(score, p_lo, p_hi)
    return (score - p_lo) / (p_hi - p_lo)


def compute_oca_volume(
    model: torch.nn.Module,
    volume: torch.Tensor,
    config: OcclusionConfig | None = None,
) -> np.ndarray:
    """Return a `(D, H, W)` confidence-drop heatmap normalised to [0, 1]."""
    if volume.dim() != 5 or volume.shape[0] != 1:
        raise ValueError(f"Expected (1, 1, D, H, W); got {tuple(volume.shape)}")
    cfg = config or OcclusionConfig()

    _, _, depth, height, width = volume.shape
    fill_const_volume = float(volume.mean().item()) if cfg.fill_mode == "mean" else 0.0

    baseline = _baseline_prob(model, volume, cfg.target_class)
    LOGGER.info(
        "OCA baseline prob[class=%d]=%.4f, patch=%d, stride=%d, slice_stride=%d, fill=%s",
        cfg.target_class, baseline,
        cfg.patch_size, cfg.stride, cfg.slice_stride, cfg.fill_mode,
    )

    score = np.zeros((depth, height, width), dtype=np.float32)
    counts = np.zeros((depth, height, width), dtype=np.float32)

    ys = _make_axis_positions(height, cfg.patch_size, cfg.stride)
    xs = _make_axis_positions(width, cfg.patch_size, cfg.stride)
    zs = list(range(0, depth, cfg.slice_stride))
    if zs[-1] != depth - 1:
        zs.append(depth - 1)

    positions: list[tuple[int, int, int]] = [(z, y, x) for z in zs for y in ys for x in xs]
    LOGGER.info("OCA total positions: %d (zs=%d ys=%d xs=%d)",
                len(positions), len(zs), len(ys), len(xs))

    for batch_start in range(0, len(positions), cfg.batch_size):
        batch_positions = positions[batch_start : batch_start + cfg.batch_size]
        batch_volumes = volume.repeat(len(batch_positions), 1, 1, 1, 1).clone()

        for i, (z, y, x) in enumerate(batch_positions):
            if cfg.fill_mode == "mean_img":
                local = batch_volumes[i, :, z, y : y + cfg.patch_size, x : x + cfg.patch_size]
                local_mean = local.mean()
                fill_value = local_mean
            else:
                fill_value = fill_const_volume
            batch_volumes[i, :, z, y : y + cfg.patch_size, x : x + cfg.patch_size] = fill_value

        with torch.no_grad():
            logits = model(batch_volumes)
            probs = model.logits2probabilities(logits)
            drops = baseline - probs[:, cfg.target_class].detach().cpu().numpy()

        for (z, y, x), drop in zip(batch_positions, drops):
            score[z, y : y + cfg.patch_size, x : x + cfg.patch_size] += float(drop)
            counts[z, y : y + cfg.patch_size, x : x + cfg.patch_size] += 1.0

    # Spread the per-z scores over neighbouring slices in the slice_stride
    # gap so the depth axis isn't a step function.
    if cfg.slice_stride > 1:
        for sampled_z in zs:
            for offset in range(-cfg.slice_stride // 2, cfg.slice_stride // 2 + 1):
                neighbour = sampled_z + offset
                if 0 <= neighbour < depth and counts[neighbour].sum() == 0:
                    score[neighbour] = score[sampled_z]
                    counts[neighbour] = counts[sampled_z]

    counts = np.maximum(counts, 1e-6)
    score = score / counts
    return _percentile_normalise(score, lo=cfg.percentile_lo, hi=cfg.percentile_hi)
