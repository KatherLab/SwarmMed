#!/usr/bin/env python3
"""Re-render the composite figures from the saved .npy heatmaps.

Loads selected_cases.csv to know which UIDs to render, fetches the raw
volumes from ODELIA_Dataset3D, and writes both ``figure_main.png`` (TP+FP)
and ``figure_extended.png`` (TP/FP/FN/TN) without re-running OCA.
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

import numpy as np
import torch

from predictor import build_dataloader, load_model
from viz import CasePanel, render_case_strip, render_composite, render_extended

LOGGER = logging.getLogger("duke_mst_explain.rerender")

COHORT_TO_DATASET = {
    "DUKE": ("duke", "test"),
    "MHA": ("odelia", "MHA"),
    "UKA": ("odelia", "UKA"),
    "CAM": ("odelia", "CAM"),
    "RUMC": ("odelia", "RUMC"),
    "UMCU": ("odelia", "UMCU"),
}

OUTCOME_TO_LABEL = {
    "TP": "True positive",
    "FP": "False positive",
    "FN": "False negative",
    "TN": "True negative",
}


def _fetch_volume(dataset, target_uid: str) -> np.ndarray:
    for index in range(len(dataset)):
        item = dataset[index]
        if str(item["uid"]) == str(target_uid):
            tensor = item["source"]
            if not isinstance(tensor, torch.Tensor):
                tensor = torch.as_tensor(tensor)
            return tensor[0].numpy()
    raise LookupError(f"UID {target_uid} not found")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--duke-root", type=Path, default=Path("/mnt/dlhd0/DUKE_iid"))
    parser.add_argument("--odelia-root", type=Path, default=Path("/mnt/dlhd0/medswarmdata"))
    parser.add_argument("--checkpoint", type=Path, required=True,
                        help="Used only for env/path side-effects; no inference.")
    args = parser.parse_args()

    load_model(args.checkpoint)

    selected_csv = args.output_dir / "selected_cases.csv"
    with selected_csv.open() as handle:
        rows = list(csv.DictReader(handle))

    panels: list[CasePanel] = []
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
        volume = _fetch_volume(dataset, uid)
        heatmaps: dict[str, np.ndarray] = {}
        for method, fname in (("gradcam", "gradcam"), ("gradcam++", "gradcampp"), ("oca", "oca")):
            path = args.output_dir / "attribution_maps" / f"{cohort}_{uid}_{fname}.npy"
            if path.exists():
                heatmaps[method] = np.load(path)
        panels.append(CasePanel(
            cohort=cohort,
            case_label=OUTCOME_TO_LABEL[row["outcome"]],
            uid=uid,
            volume=volume,
            heatmaps=heatmaps,
        ))

    methods = ["gradcam++"] + (["oca"] if any("oca" in p.heatmaps for p in panels) else [])
    figure_main = args.output_dir / "figures" / "figure_main.png"
    render_composite(panels, figure_main, methods=methods)
    LOGGER.info("Wrote %s", figure_main)
    figure_ext = args.output_dir / "figures" / "figure_extended.png"
    render_extended(panels, figure_ext, methods=methods)
    LOGGER.info("Wrote %s", figure_ext)

    strips_dir = args.output_dir / "figures" / "strips"
    strips_dir.mkdir(parents=True, exist_ok=True)
    for panel in panels:
        title = f"{panel.cohort} — {panel.case_label} — {panel.uid}"
        out = strips_dir / f"{panel.cohort}_{panel.uid}_strip.png"
        render_case_strip(
            panel,
            out,
            methods=("gradcam", "gradcam++", "oca"),
            title=title,
        )
        LOGGER.info("Wrote %s", out)


if __name__ == "__main__":
    main()
