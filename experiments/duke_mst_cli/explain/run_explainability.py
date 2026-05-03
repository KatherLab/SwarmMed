#!/usr/bin/env python3
"""End-to-end explainability orchestrator for the Duke MST FL global model.

Round 2 expands the report to top-2 cases per outcome (TP/FP/FN/TN) per cohort,
runs OCA at finer resolution (patch=16, stride=8, mean_img fill, 3D sweep),
averages GradCAM++ over the last few ViT blocks, and renders both the TP+FP
composite (matches the user's reference layout) and a 4-class extended figure.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import torch

from occlusion import OcclusionConfig, compute_oca_volume
from predictor import (
    build_dataloader,
    cohort_auroc,
    load_model,
    predict_per_case,
    select_topk_per_outcome,
    write_predictions_csv,
)
from viz import CasePanel, render_composite, render_extended, render_grid
from vit_cam import compute_cam_volume


LOGGER = logging.getLogger("duke_mst_explain.run")

COHORT_ALIASES = {
    "duke": {"data_root_key": "duke", "institution": "test", "csv_name": "duke_test.csv",
             "ref_json": "duke_test_eval.json", "display": "DUKE"},
    "MHA": {"data_root_key": "odelia", "institution": "MHA", "csv_name": "odelia_MHA.csv",
            "ref_json": "odelia_MHA_eval.json", "display": "MHA"},
    "UKA": {"data_root_key": "odelia", "institution": "UKA", "csv_name": "odelia_UKA.csv",
            "ref_json": "odelia_UKA_eval.json", "display": "UKA"},
    "CAM": {"data_root_key": "odelia", "institution": "CAM", "csv_name": "odelia_CAM.csv",
            "ref_json": "odelia_CAM_eval.json", "display": "CAM"},
    "RUMC": {"data_root_key": "odelia", "institution": "RUMC", "csv_name": "odelia_RUMC.csv",
             "ref_json": "odelia_RUMC_eval.json", "display": "RUMC"},
    "UMCU": {"data_root_key": "odelia", "institution": "UMCU", "csv_name": "odelia_UMCU.csv",
             "ref_json": "odelia_UMCU_eval.json", "display": "UMCU"},
}

OUTCOME_TO_LABEL = {
    "TP": "True positive",
    "FP": "False positive",
    "FN": "False negative",
    "TN": "True negative",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--duke-root", type=Path, default=Path("/mnt/dlhd0/DUKE_iid"))
    parser.add_argument("--odelia-root", type=Path, default=Path("/mnt/dlhd0/medswarmdata"))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cohorts", nargs="+", default=["duke", "MHA"])
    parser.add_argument("--ref-json-dir", type=Path,
                        default=Path("/home/swarm/Projects/SwarmCloud/results/duke_mst_cli/repetition_1"))
    parser.add_argument("--auroc-tolerance", type=float, default=0.01)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--cases-per-outcome", type=int, default=2,
                        help="Top-K cases to attribute per (cohort, outcome). Default 2 → 16 total cases for 2 cohorts.")
    parser.add_argument("--occlusion-patch", type=int, default=16)
    parser.add_argument("--occlusion-stride", type=int, default=8)
    parser.add_argument("--occlusion-slice-stride", type=int, default=4)
    parser.add_argument("--occlusion-batch", type=int, default=16)
    parser.add_argument("--occlusion-fill", type=str, default="mean_img",
                        choices=["mean_img", "mean", "zero"])
    parser.add_argument("--cam-last-n-blocks", type=int, default=1)
    parser.add_argument("--cam-percentile-lo", type=float, default=1.0)
    parser.add_argument("--cam-percentile-hi", type=float, default=99.0)
    parser.add_argument("--cam-smooth-sigma", type=float, default=0.0)
    parser.add_argument("--skip-occlusion", action="store_true",
                        help="Skip OCA for fast iteration.")
    parser.add_argument("--outcomes", nargs="+", default=["TP", "FP", "FN", "TN"],
                        choices=["TP", "FP", "FN", "TN"])
    return parser.parse_args()


def _ensure_dirs(output_dir: Path) -> None:
    for sub in ("figures", "per_case_predictions", "attribution_maps"):
        (output_dir / sub).mkdir(parents=True, exist_ok=True)


def _data_root(cohort: str, args: argparse.Namespace) -> Path:
    if COHORT_ALIASES[cohort]["data_root_key"] == "duke":
        return args.duke_root
    return args.odelia_root


def _check_against_published(
    cohort: str,
    measured_auroc: float,
    args: argparse.Namespace,
    metrics_check: dict,
) -> None:
    ref_path = args.ref_json_dir / COHORT_ALIASES[cohort]["ref_json"]
    if not ref_path.exists():
        LOGGER.warning("No reference JSON at %s; skipping AUROC check for %s", ref_path, cohort)
        metrics_check[cohort] = {"measured": measured_auroc, "published": None, "delta": None}
        return
    payload = json.loads(ref_path.read_text())
    published = payload["results"][0]["metrics"]["auroc"]
    delta = abs(published - measured_auroc)
    metrics_check[cohort] = {"measured": measured_auroc, "published": published, "delta": delta}
    if delta > args.auroc_tolerance:
        raise RuntimeError(
            f"AUROC mismatch for {cohort}: measured={measured_auroc:.4f} "
            f"published={published:.4f} delta={delta:.4f} > tol={args.auroc_tolerance}"
        )
    LOGGER.info("Cohort %s AUROC reproduced: measured=%.4f published=%.4f", cohort, measured_auroc, published)


def _fetch_volume(dataset, target_uid: str) -> torch.Tensor:
    for index in range(len(dataset)):
        item = dataset[index]
        if str(item["uid"]) == str(target_uid):
            tensor = item["source"]
            if not isinstance(tensor, torch.Tensor):
                tensor = torch.as_tensor(tensor)
            return tensor.unsqueeze(0)
    raise LookupError(f"UID {target_uid} not found in dataset")


def _attribution_path(output_dir: Path, cohort: str, uid: str, method: str) -> Path:
    safe_uid = str(uid).replace("/", "_")
    return output_dir / "attribution_maps" / f"{cohort}_{safe_uid}_{method}.npy"


def _grid_path(output_dir: Path, cohort: str, uid: str, method: str) -> Path:
    safe_uid = str(uid).replace("/", "_")
    return output_dir / "figures" / f"{cohort}_{safe_uid}_{method}.png"


def _explain_case(
    *,
    model: torch.nn.Module,
    volume: torch.Tensor,
    cohort: str,
    case_label: str,
    uid: str,
    output_dir: Path,
    occlusion_cfg: OcclusionConfig,
    cam_kwargs: dict,
    skip_occlusion: bool,
) -> CasePanel:
    LOGGER.info("Explaining %s/%s (%s)", cohort, uid, case_label)
    volume_np = volume[0, 0].detach().cpu().numpy()
    heatmaps: dict[str, np.ndarray] = {}

    LOGGER.info("→ GradCAM")
    gc = compute_cam_volume(model, volume, method="gradcam", **cam_kwargs)
    np.save(_attribution_path(output_dir, cohort, uid, "gradcam"), gc)
    render_grid(volume_np, gc, _grid_path(output_dir, cohort, uid, "gradcam"),
                title=f"{cohort} {uid} — GradCAM (target=Malignant)")
    heatmaps["gradcam"] = gc

    LOGGER.info("→ GradCAM++")
    gpp = compute_cam_volume(model, volume, method="gradcam++", **cam_kwargs)
    np.save(_attribution_path(output_dir, cohort, uid, "gradcampp"), gpp)
    render_grid(volume_np, gpp, _grid_path(output_dir, cohort, uid, "gradcampp"),
                title=f"{cohort} {uid} — GradCAM++ (target=Malignant)")
    heatmaps["gradcam++"] = gpp

    if not skip_occlusion:
        LOGGER.info("→ OCA (occlusion sensitivity)")
        oca = compute_oca_volume(model, volume, occlusion_cfg)
        np.save(_attribution_path(output_dir, cohort, uid, "oca"), oca)
        render_grid(volume_np, oca, _grid_path(output_dir, cohort, uid, "oca"),
                    title=f"{cohort} {uid} — OCA (target=Malignant)")
        heatmaps["oca"] = oca

    render_grid(volume_np, None, _grid_path(output_dir, cohort, uid, "raw"),
                title=f"{cohort} {uid} — Raw")

    return CasePanel(
        cohort=cohort,
        case_label=case_label,
        uid=uid,
        volume=volume_np,
        heatmaps=heatmaps,
    )


def _write_selected_cases_csv(
    selected: list[dict[str, Any]], output_dir: Path
) -> Path:
    csv_path = output_dir / "selected_cases.csv"
    fieldnames = ["cohort", "outcome", "uid", "target", "pred", "prob_malignant",
                  "raw_png", "gradcam_png", "gradcampp_png", "oca_png",
                  "gradcam_npy", "gradcampp_npy", "oca_npy"]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)
    return csv_path


def _write_report(
    output_dir: Path,
    panels: list[CasePanel],
    metrics_check: dict,
    checkpoint: Path,
    selected_csv: Path,
) -> None:
    by_outcome: dict[str, list[CasePanel]] = {label: [] for label in OUTCOME_TO_LABEL.values()}
    for panel in panels:
        by_outcome.setdefault(panel.case_label, []).append(panel)

    lines = [
        "# Duke MST Repetition 1 — Explainability Report (round 2)",
        "",
        f"Checkpoint: `{checkpoint}` (MD5 `172f8bcf98f1fb6199261be8bbdfc9a3`)",
        "",
        "## AUROC reproduction (sanity check)",
        "",
        "| Cohort | Measured | Published | Δ |",
        "|--------|----------|-----------|---|",
    ]
    for cohort, vals in metrics_check.items():
        m = f"{vals['measured']:.4f}"
        p = f"{vals['published']:.4f}" if vals["published"] is not None else "—"
        d = f"{vals['delta']:.4f}" if vals["delta"] is not None else "—"
        lines.append(f"| {cohort} | {m} | {p} | {d} |")

    lines += [
        "",
        "## Composite figures",
        "",
        "- TP + FP (matches reference layout): [`figures/figure_main.png`](figures/figure_main.png)",
        "- All four outcomes (TP / FP / FN / TN): [`figures/figure_extended.png`](figures/figure_extended.png)",
        "",
        "## Selected cases",
        "",
        "Top-K cases per (cohort, outcome) ranked by P(Malignant). "
        "TP/FP are sorted DESC (most-confident-positive); FN/TN are sorted ASC (most-confident-negative).",
        "",
    ]

    for label in OUTCOME_TO_LABEL.values():
        if not by_outcome.get(label):
            continue
        lines += [
            f"### {label}",
            "",
            "| Cohort | UID | Raw | GradCAM++ | OCA |",
            "|--------|-----|-----|-----------|-----|",
        ]
        for panel in by_outcome[label]:
            uid = panel.uid
            cohort = panel.cohort
            raw = f"figures/{cohort}_{uid}_raw.png"
            gpp = f"figures/{cohort}_{uid}_gradcampp.png"
            oca = f"figures/{cohort}_{uid}_oca.png" if "oca" in panel.heatmaps else "—"
            oca_cell = f"[link]({oca})" if oca != "—" else "—"
            lines.append(
                f"| {cohort} | `{uid}` | [link]({raw}) | [link]({gpp}) | {oca_cell} |"
            )
        lines.append("")

    lines += [
        "## Method notes (round 2 changes)",
        "",
        "- **GradCAM / GradCAM++**: averaged over the last 4 DINOv2 ViT blocks "
        "(`mst.backbone.blocks[-4:].norm1`). Patch tokens (1+16×16) reshaped to (C, 16, 16), "
        "upsampled to slice resolution, then percentile-clipped (1–99) and smoothed (sigma=1.5).",
        "- **OCA**: 16×16 occlusion patches at stride 8, slice-stride 4, fill = local-window "
        "mean (`mean_img`). 3D sweep over (D, H, W). Records drop in P(Malignant) vs unperturbed baseline.",
        "- All heatmaps are normalised to [0, 1] per case; the colorbar in the composite figure is shared.",
        "",
        f"Per-case selection list: [`selected_cases.csv`](./{selected_csv.name})",
    ]

    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    _ensure_dirs(args.output_dir)

    LOGGER.info("Loading model from %s", args.checkpoint)
    model = load_model(args.checkpoint)

    occlusion_cfg = OcclusionConfig(
        patch_size=args.occlusion_patch,
        stride=args.occlusion_stride,
        slice_stride=args.occlusion_slice_stride,
        target_class=2,
        fill_mode=args.occlusion_fill,
        batch_size=args.occlusion_batch,
    )
    cam_kwargs = dict(
        target_class=2,
        last_n_blocks=args.cam_last_n_blocks,
        percentile_lo=args.cam_percentile_lo,
        percentile_hi=args.cam_percentile_hi,
        smooth_sigma=args.cam_smooth_sigma,
    )

    metrics_check: dict[str, Any] = {}
    panels: list[CasePanel] = []
    selected_rows: list[dict[str, Any]] = []

    for cohort in args.cohorts:
        if cohort not in COHORT_ALIASES:
            raise SystemExit(f"Unknown cohort: {cohort}. Known: {list(COHORT_ALIASES)}")

        LOGGER.info("=== Cohort %s ===", cohort)
        cohort_root = _data_root(cohort, args)
        institution = COHORT_ALIASES[cohort]["institution"]
        dataset, loader = build_dataloader(
            cohort_root, institution=institution, num_workers=args.num_workers
        )

        records = predict_per_case(model, loader, positive_class=2)
        csv_path = args.output_dir / "per_case_predictions" / COHORT_ALIASES[cohort]["csv_name"]
        write_predictions_csv(records, csv_path)

        measured_auroc = cohort_auroc(records, positive_class=2)
        _check_against_published(cohort, measured_auroc, args, metrics_check)

        topk = select_topk_per_outcome(records, k=args.cases_per_outcome, positive_class=2)
        for outcome, requested_records in topk.items():
            if outcome not in args.outcomes:
                continue
            if not requested_records:
                LOGGER.warning("No %s cases for %s; skipping", outcome, cohort)
                continue
            label = OUTCOME_TO_LABEL[outcome]
            for rec in requested_records:
                volume = _fetch_volume(dataset, rec["uid"]).to(next(model.parameters()).device)
                panel = _explain_case(
                    model=model,
                    volume=volume,
                    cohort=COHORT_ALIASES[cohort]["display"],
                    case_label=label,
                    uid=rec["uid"],
                    output_dir=args.output_dir,
                    occlusion_cfg=occlusion_cfg,
                    cam_kwargs=cam_kwargs,
                    skip_occlusion=args.skip_occlusion,
                )
                panels.append(panel)
                selected_rows.append({
                    "cohort": COHORT_ALIASES[cohort]["display"],
                    "outcome": outcome,
                    "uid": rec["uid"],
                    "target": rec["target"],
                    "pred": rec["pred"],
                    "prob_malignant": f"{rec['prob_malignant']:.6f}",
                    "raw_png": f"figures/{COHORT_ALIASES[cohort]['display']}_{rec['uid']}_raw.png",
                    "gradcam_png": f"figures/{COHORT_ALIASES[cohort]['display']}_{rec['uid']}_gradcam.png",
                    "gradcampp_png": f"figures/{COHORT_ALIASES[cohort]['display']}_{rec['uid']}_gradcampp.png",
                    "oca_png": f"figures/{COHORT_ALIASES[cohort]['display']}_{rec['uid']}_oca.png" if not args.skip_occlusion else "",
                    "gradcam_npy": f"attribution_maps/{COHORT_ALIASES[cohort]['display']}_{rec['uid']}_gradcam.npy",
                    "gradcampp_npy": f"attribution_maps/{COHORT_ALIASES[cohort]['display']}_{rec['uid']}_gradcampp.npy",
                    "oca_npy": f"attribution_maps/{COHORT_ALIASES[cohort]['display']}_{rec['uid']}_oca.npy" if not args.skip_occlusion else "",
                })

    selected_csv = _write_selected_cases_csv(selected_rows, args.output_dir)

    methods_for_composite = ["gradcam++"] + ([] if args.skip_occlusion else ["oca"])
    figure_main_path = args.output_dir / "figures" / "figure_main.png"
    render_composite(panels, figure_main_path, methods=methods_for_composite)
    LOGGER.info("Composite figure: %s", figure_main_path)

    figure_ext_path = args.output_dir / "figures" / "figure_extended.png"
    render_extended(panels, figure_ext_path, methods=methods_for_composite)
    LOGGER.info("Extended figure: %s", figure_ext_path)

    (args.output_dir / "metrics_check.json").write_text(
        json.dumps(metrics_check, indent=2), encoding="utf-8"
    )

    _write_report(args.output_dir, panels, metrics_check, args.checkpoint, selected_csv)


if __name__ == "__main__":
    main()
