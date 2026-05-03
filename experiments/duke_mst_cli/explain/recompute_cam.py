#!/usr/bin/env python3
"""Recompute GradCAM/GradCAM++ heatmaps for all cases listed in
``selected_cases.csv`` while keeping the existing OCA .npy files in place.

Used after tuning ``compute_cam_volume`` (e.g. switching back to single-block
CAMs or disabling smoothing) without paying the cost of re-running the slow
OCA sweep.

Also re-normalises OCA .npy arrays in place: a percentile clip applied to a
heavily zero-skewed score smashes the bright dots into the background, so we
prefer per-volume min-max with the negatives clipped (the natural raw OCA
output, just rescaled). This requires the .npy files to be present already.

After this finishes, run ``render_from_npy.py`` to regenerate the composite,
extended, and per-case strip figures from the freshly-saved .npy files.
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

import numpy as np
import torch

from predictor import build_dataloader, load_model
from vit_cam import compute_cam_volume

LOGGER = logging.getLogger("duke_mst_explain.recompute_cam")

COHORT_TO_DATASET = {
    "DUKE": ("duke", "test"),
    "MHA": ("odelia", "MHA"),
    "UKA": ("odelia", "UKA"),
    "CAM": ("odelia", "CAM"),
    "RUMC": ("odelia", "RUMC"),
    "UMCU": ("odelia", "UMCU"),
}


def _fetch_volume(dataset, target_uid: str) -> torch.Tensor:
    for index in range(len(dataset)):
        item = dataset[index]
        if str(item["uid"]) == str(target_uid):
            tensor = item["source"]
            if not isinstance(tensor, torch.Tensor):
                tensor = torch.as_tensor(tensor)
            return tensor.unsqueeze(0)
    raise LookupError(f"UID {target_uid} not found")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--duke-root", type=Path, default=Path("/mnt/dlhd0/DUKE_iid"))
    parser.add_argument("--odelia-root", type=Path, default=Path("/mnt/dlhd0/medswarmdata"))
    parser.add_argument("--last-n-blocks", type=int, default=1)
    parser.add_argument("--smooth-sigma", type=float, default=1.0)
    parser.add_argument("--percentile-lo", type=float, default=0.0)
    parser.add_argument("--percentile-hi", type=float, default=100.0)
    parser.add_argument("--target-class", type=int, default=2)
    parser.add_argument("--oca-gamma", type=float, default=2.5,
                        help="Gamma stretch applied to existing OCA .npy arrays so the bright "
                             "spots stand out against a near-zero background. 1.0 disables it.")
    args = parser.parse_args()

    model = load_model(args.checkpoint)
    device = next(model.parameters()).device
    cam_kwargs = dict(
        target_class=args.target_class,
        last_n_blocks=args.last_n_blocks,
        percentile_lo=args.percentile_lo,
        percentile_hi=args.percentile_hi,
        smooth_sigma=args.smooth_sigma,
    )

    selected_csv = args.output_dir / "selected_cases.csv"
    with selected_csv.open() as handle:
        rows = list(csv.DictReader(handle))

    cache: dict[tuple[str, str], object] = {}
    for row in rows:
        cohort = row["cohort"]
        family, institution = COHORT_TO_DATASET[cohort]
        cohort_root = args.duke_root if family == "duke" else args.odelia_root
        cache_key = (cohort, institution)
        if cache_key not in cache:
            cache[cache_key], _ = build_dataloader(cohort_root, institution=institution, num_workers=2)
        dataset = cache[cache_key]

        uid = row["uid"]
        LOGGER.info("Recomputing CAM for %s/%s", cohort, uid)
        volume = _fetch_volume(dataset, uid).to(device)

        for method, fname in (("gradcam", "gradcam"), ("gradcam++", "gradcampp")):
            heatmap = compute_cam_volume(model, volume, method=method, **cam_kwargs)
            np.save(args.output_dir / "attribution_maps" / f"{cohort}_{uid}_{fname}.npy", heatmap)

        if args.oca_gamma != 1.0:
            oca_path = args.output_dir / "attribution_maps" / f"{cohort}_{uid}_oca.npy"
            if oca_path.exists():
                oca = np.load(oca_path).astype(np.float32)
                stretched = np.clip(oca, 0.0, 1.0) ** float(args.oca_gamma)
                if stretched.max() > 1e-8:
                    stretched = stretched / stretched.max()
                np.save(oca_path, stretched)


if __name__ == "__main__":
    main()
