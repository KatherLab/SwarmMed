"""Rendering helpers for the MST explainability report.

- ``render_grid``: 4x4 sampled-slice grid for one (D, H, W) volume + optional heatmap overlay.
- ``render_composite``: composite figure mirroring the user's reference layout —
  rows for True/False positive cases, two cohorts each, with three columns
  (Raw / GradCAM++ / OCA) and a single shared 0–1 jet colorbar at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import gridspec


GRID_ROWS = 4
GRID_COLS = 4
NUM_TILES = GRID_ROWS * GRID_COLS

# Gamma applied to all heatmap panels at display time. >1 darkens mid-tones
# (more blue) while preserving peak intensities (red) — keeps the lesion
# localisation crisp.
HEATMAP_GAMMA = 3.0


@dataclass
class CasePanel:
    cohort: str
    case_label: str
    uid: str
    volume: np.ndarray  # (D, H, W) raw intensity, will be windowed for display
    heatmaps: dict[str, np.ndarray]  # method -> (D, H, W) in [0, 1]


def _window(volume: np.ndarray, low_pct: float = 0.5, high_pct: float = 99.5) -> np.ndarray:
    """Per-volume percentile-clipped grayscale window for the underlying anatomy.

    Tighter percentiles (0.5 / 99.5) than the heatmap clip — for raw MR display
    we want the breast tissue to be visible across all 16 sampled slices.
    """
    lo = float(np.percentile(volume, low_pct))
    hi = float(np.percentile(volume, high_pct))
    if hi - lo < 1e-6:
        return np.zeros_like(volume, dtype=np.float32)
    return np.clip((volume - lo) / (hi - lo), 0.0, 1.0)


def _sample_slices(depth: int) -> list[int]:
    return [int(round(i * (depth - 1) / (NUM_TILES - 1))) for i in range(NUM_TILES)]


def _draw_grid(ax: plt.Axes, volume_disp: np.ndarray, heatmap: np.ndarray | None) -> None:
    depth, height, width = volume_disp.shape
    indices = _sample_slices(depth)
    canvas_h = GRID_ROWS * height
    canvas_w = GRID_COLS * width
    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
    overlay = np.zeros((canvas_h, canvas_w), dtype=np.float32) if heatmap is not None else None

    for tile_idx, slice_idx in enumerate(indices):
        row = tile_idx // GRID_COLS
        col = tile_idx % GRID_COLS
        y = row * height
        x = col * width
        slice_disp = volume_disp[slice_idx]
        canvas[y : y + height, x : x + width, 0] = slice_disp
        canvas[y : y + height, x : x + width, 1] = slice_disp
        canvas[y : y + height, x : x + width, 2] = slice_disp
        if heatmap is not None:
            overlay[y : y + height, x : x + width] = heatmap[slice_idx]

    if heatmap is None:
        ax.imshow(canvas, interpolation="bilinear")
    else:
        # Pure jet colormap, no grayscale anatomy underneath. A gamma > 1
        # pushes mid-tones toward blue so only the genuinely-high regions
        # render as red — matches the user's reference figure where the
        # background is uniformly blue and the lesion stands out as a red
        # blob with clear localisation.
        gamma = HEATMAP_GAMMA
        overlay_g = np.clip(overlay, 0.0, 1.0) ** gamma
        cmap = plt.get_cmap("jet")
        rgba = cmap(overlay_g)[:, :, :3]
        ax.imshow(np.clip(rgba, 0, 1), interpolation="bilinear")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)


def render_grid(
    volume: np.ndarray,
    heatmap: np.ndarray | None,
    output_path: Path,
    *,
    title: str | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    volume_disp = _window(volume)
    fig, ax = plt.subplots(figsize=(6, 6), dpi=150)
    _draw_grid(ax, volume_disp, heatmap)
    if title:
        ax.set_title(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _render_layout(
    panels: Sequence[CasePanel],
    output_path: Path,
    *,
    case_labels: Sequence[str],
    methods: Sequence[str],
) -> None:
    """Render a composite figure with one row-band per case_label.

    Generic core used by both ``render_composite`` (TP, FP) and
    ``render_extended`` (TP, FP, FN, TN).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cohorts_order: list[str] = []
    for label in case_labels:
        for panel in panels:
            if panel.case_label == label and panel.cohort not in cohorts_order:
                cohorts_order.append(panel.cohort)

    method_display = {"gradcam": "GradCAM", "gradcam++": "GradCAM++", "oca": "OCA"}
    column_titles = ["2D-Raw Slices"] + [method_display.get(m, m) for m in methods]
    n_cols = 1 + len(column_titles)
    n_cohorts = len(cohorts_order)
    n_data_rows = n_cohorts * len(case_labels)
    n_rows = 1 + n_data_rows + 1  # header + data rows + colorbar row

    fig = plt.figure(figsize=(3.5 * len(column_titles) + 1.4, 3.5 * n_data_rows + 1.6), dpi=140)
    gs = gridspec.GridSpec(
        n_rows,
        n_cols,
        figure=fig,
        width_ratios=[0.7] + [1.0] * len(column_titles),
        height_ratios=[0.35] + [1.0] * n_data_rows + [0.45],
        wspace=0.05,
        hspace=0.08,
    )

    # Column headers
    for j, col_title in enumerate(["Test Cohort", *column_titles]):
        ax = fig.add_subplot(gs[0, j])
        ax.axis("off")
        ax.text(0.5, 0.4, col_title, ha="center", va="center",
                fontsize=14, fontweight="bold")

    def _find_panel(case_label: str, cohort: str) -> CasePanel | None:
        for p in panels:
            if p.case_label == case_label and p.cohort == cohort:
                return p
        return None

    row_idx = 1
    for case_label in case_labels:
        band_ax = fig.add_subplot(gs[row_idx : row_idx + n_cohorts, 0])
        band_ax.axis("off")
        band_ax.text(
            0.5, 0.5, case_label,
            ha="center", va="center", rotation=90,
            fontsize=14, fontweight="bold",
        )

        for cohort in cohorts_order:
            panel = _find_panel(case_label, cohort)

            raw_ax = fig.add_subplot(gs[row_idx, 1])
            if panel is not None:
                _draw_grid(raw_ax, _window(panel.volume), heatmap=None)
                raw_ax.text(
                    -0.05, 0.5, cohort, transform=raw_ax.transAxes,
                    ha="right", va="center", fontsize=12, fontweight="bold",
                )
            else:
                raw_ax.axis("off")

            for method_idx, method in enumerate(methods):
                ax = fig.add_subplot(gs[row_idx, 2 + method_idx])
                if panel is not None and method in panel.heatmaps:
                    _draw_grid(ax, _window(panel.volume), panel.heatmaps[method])
                else:
                    ax.axis("off")
            row_idx += 1

    cbar_ax = fig.add_subplot(gs[-1, 1:])
    cbar_ax.axis("off")
    inner = cbar_ax.inset_axes([0.2, 0.35, 0.6, 0.35])
    norm = matplotlib.colors.Normalize(vmin=0.0, vmax=1.0)
    cb = matplotlib.colorbar.ColorbarBase(
        inner, cmap=plt.get_cmap("jet"), norm=norm, orientation="horizontal",
    )
    cb.set_ticks([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    cbar_ax.text(0.21, 0.05, "Low\nattention", ha="center", va="top", fontsize=10)
    cbar_ax.text(0.79, 0.05, "High\nattention", ha="center", va="top", fontsize=10)

    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def render_composite(
    panels: Sequence[CasePanel],
    output_path: Path,
    *,
    methods: Sequence[str] = ("gradcam++", "oca"),
) -> None:
    """TP+FP figure mirroring the user's reference layout."""
    _render_layout(
        panels,
        output_path,
        case_labels=("True positive", "False positive"),
        methods=methods,
    )


def render_extended(
    panels: Sequence[CasePanel],
    output_path: Path,
    *,
    methods: Sequence[str] = ("gradcam++", "oca"),
) -> None:
    """Extended figure with all four prediction outcomes (TP, FP, FN, TN)."""
    _render_layout(
        panels,
        output_path,
        case_labels=("True positive", "False positive", "False negative", "True negative"),
        methods=methods,
    )


def render_case_strip(
    panel: CasePanel,
    output_path: Path,
    *,
    methods: Sequence[str] = ("gradcam", "gradcam++", "oca"),
    title: str | None = None,
) -> None:
    """Side-by-side per-case strip: Raw | <method panels>.

    Each panel is the case's 4×4 sampled-slice grid. A shared 0–1 jet colorbar
    sits below the method panels (the raw column gets no bar).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    method_display = {"gradcam": "GradCAM", "gradcam++": "GradCAM++", "oca": "OCA"}
    available = [m for m in methods if m in panel.heatmaps]
    column_titles = ["2D-Raw Slices"] + [method_display.get(m, m) for m in available]
    n_cols = len(column_titles)

    fig = plt.figure(figsize=(3.5 * n_cols + 0.4, 4.6), dpi=140)
    gs = gridspec.GridSpec(
        3, n_cols,
        figure=fig,
        height_ratios=[0.18, 1.0, 0.18],
        wspace=0.03,
        hspace=0.04,
    )

    for j, col_title in enumerate(column_titles):
        ax = fig.add_subplot(gs[0, j])
        ax.axis("off")
        ax.text(0.5, 0.3, col_title, ha="center", va="center",
                fontsize=12, fontweight="bold")

    volume_disp = _window(panel.volume)
    raw_ax = fig.add_subplot(gs[1, 0])
    _draw_grid(raw_ax, volume_disp, heatmap=None)
    for j, method in enumerate(available, start=1):
        ax = fig.add_subplot(gs[1, j])
        _draw_grid(ax, volume_disp, panel.heatmaps[method])

    cbar_ax = fig.add_subplot(gs[2, 1:])
    cbar_ax.axis("off")
    inner = cbar_ax.inset_axes([0.25, 0.4, 0.5, 0.35])
    norm = matplotlib.colors.Normalize(vmin=0.0, vmax=1.0)
    cb = matplotlib.colorbar.ColorbarBase(
        inner, cmap=plt.get_cmap("jet"), norm=norm, orientation="horizontal",
    )
    cb.set_ticks([0.0, 0.25, 0.5, 0.75, 1.0])
    cbar_ax.text(0.245, 0.05, "Low\nattention", ha="center", va="top", fontsize=9)
    cbar_ax.text(0.755, 0.05, "High\nattention", ha="center", va="top", fontsize=9)

    if title:
        fig.suptitle(title, fontsize=12, y=1.02)
    fig.savefig(output_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
