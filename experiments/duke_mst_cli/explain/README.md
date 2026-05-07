# MST Repetition 1 — Explainability Report Generator

This module produces the explainability artefacts (per-case predictions,
GradCAM / GradCAM++ / OCA heatmaps, and a composite multi-cohort figure) for
the Duke MST federated checkpoint described in
[`results/duke_mst_cli/duke_mst_full_runs_evaluation.xlsx`](../../../results/duke_mst_cli/duke_mst_full_runs_evaluation.xlsx).

## Why these methods

MST is a DINOv2 ViT-S/14 backbone applied per slice with a transformer slice-fusion head
(see [`/home/jeff/Projects/MediSwarm/application/jobs/_shared/custom/models/mst.py`](/home/jeff/Projects/MediSwarm/application/jobs/_shared/custom/models/mst.py)).
Because the backbone is a transformer, classic CAM hooks on a "last conv block" don't apply.
Instead:

- **GradCAM / GradCAM++** hook the LayerNorm before the last attention block
  (`model.mst.backbone.blocks[-1].norm1`). The 1+16×16 token sequence is reshaped
  back into a 16×16 patch grid and bilinearly upsampled to slice resolution.
- **OCA (Occlusion Confidence Activation)** sweeps a 48×48 occlusion patch (stride 32,
  fill = per-volume mean intensity) over each axial slice and records the drop in
  P(Malignant) versus the baseline. This is a black-box, model-agnostic attribution
  that complements gradient-based maps.

## Where the data and checkpoint live

Both Duke and ODELIA cohorts plus the Repetition 1 checkpoint live on the **dd-dl0** server:

| Artefact | Path on dd-dl0 |
|----------|----------------|
| Repetition 1 checkpoint | `/home/swarm/Projects/SwarmCloud/workspaces/627f6350-…/…/37bc58d0-…/app_node-A/FL_global_model.pt` (MD5 `172f8bcf…`) |
| Duke test set | `/mnt/dlhd0/DUKE_iid/test/{data_unilateral,metadata_unilateral}` |
| ODELIA cohorts | `/mnt/dlhd0/medswarmdata/{MHA,RUMC,UKA,UMCU,CAM,RSH}/{data_unilateral,metadata_unilateral}` |
| MediSwarm code | `/home/swarm/actions-runner/_work/MediSwarm/MediSwarm` |

## Running

On dd-dl0 (or any host with the same checkpoint, datasets, and the
`/home/swarm/Projects/SwarmCloud/.venv` Python environment):

```bash
ssh dd-dl0
cd /home/swarm/Projects/SwarmCloud
.venv/bin/python -m pip install grad-cam captum torchio einops x-transformers

MEDISWARM_PROJECT_ROOT=/home/swarm/actions-runner/_work/MediSwarm/MediSwarm \
PYTHONPATH=experiments/duke_mst_cli:experiments/duke_mst_cli/explain \
.venv/bin/python experiments/duke_mst_cli/explain/run_explainability.py \
    --checkpoint /home/swarm/Projects/SwarmCloud/workspaces/627f6350-4fa5-4caf-8658-89118e4cee6a/f93350f0-b4ed-4f3b-88b9-000c16b81e8f/workspace/duke_mst_iid_3_site_cli_2026_04_28/prod_00/node-A/37bc58d0-9425-4180-9846-7273cd55d2bf/app_node-A/FL_global_model.pt \
    --duke-root /mnt/dlhd0/DUKE_iid \
    --odelia-root /mnt/dlhd0/medswarmdata \
    --output-dir results/duke_mst_cli/explainability \
    --cohorts duke MHA
```

Add `--skip-occlusion` for a CAM-only fast path (~2 min instead of ~20 min).

## What gets produced

```
results/duke_mst_cli/explainability/
├── README.md                  # provenance + methodology
├── report.md                  # per-case table + AUROC reproduction
├── metrics_check.json         # measured vs. published cohort AUROC
├── per_case_predictions/
│   ├── duke_test.csv
│   └── odelia_MHA.csv
├── attribution_maps/          # raw .npy heatmaps per case+method
└── figures/
    ├── figure_main.png        # composite mirroring the reference layout
    └── <cohort>_<uid>_{raw,gradcam,gradcampp,oca}.png
```

## Validation

`run_explainability.py` reproduces the cohort AUROC and asserts it matches
the value published in `results/duke_mst_cli/repetition_1/<cohort>_eval.json`
within `--auroc-tolerance` (default 0.01). On the verified setup:

| Cohort | Measured | Published | Δ |
|--------|----------|-----------|---|
| MHA    | 0.6082   | 0.6082    | 0.0000 |
| Duke   | (filled in by `report.md`) | 0.9007 | (filled in) |

If the AUROC mismatches, the run aborts before producing figures.
