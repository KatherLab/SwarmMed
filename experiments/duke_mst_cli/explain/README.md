# MST Repetition 1 — Explainability Report Generator

This module produces the explainability artefacts (per-case predictions,
GradCAM / GradCAM++ / OCA heatmaps, and a composite multi-cohort figure) for
the Duke MST federated checkpoint described in
[`results/duke_mst_cli/duke_mst_full_runs_evaluation.xlsx`](../../../results/duke_mst_cli/duke_mst_full_runs_evaluation.xlsx).

## Why these methods

MST is a DINOv2 ViT-S/14 backbone applied per slice with a transformer slice-fusion head
(see `application/jobs/_shared/custom/models/mst.py` in MediSwarm).
Because the backbone is a transformer, classic CAM hooks on a "last conv block" don't apply.
Instead:

- **GradCAM / GradCAM++** hook the LayerNorm before the last attention block
  (`model.mst.backbone.blocks[-1].norm1`). The 1+16×16 token sequence is reshaped
  back into a 16×16 patch grid and bilinearly upsampled to slice resolution.
- **OCA (Occlusion Confidence Activation)** sweeps a 48×48 occlusion patch (stride 32,
  fill = per-volume mean intensity) over each axial slice and records the drop in
  P(Malignant) versus the baseline. This is a black-box, model-agnostic attribution
  that complements gradient-based maps.

## What the script needs

| Artefact | Where |
|----------|-------|
| Checkpoint | `FL_global_model.pt` in the job's app folder in a training site's FLARE workspace, `<job-id>/app_<site>/FL_global_model.pt` (Repetition 1's has MD5 `172f8bcf…`) |
| Duke test set | `<duke-root>/test/{data_unilateral,metadata_unilateral}` |
| ODELIA cohorts | `<odelia-root>/<cohort>/{data_unilateral,metadata_unilateral}` for the cohorts in `COHORT_ALIASES` in `run_explainability.py` |
| MediSwarm code | a MediSwarm checkout, `<mediswarm-checkout>` |
| Published AUROCs | the `--output-json` files of `evaluate_global_model.py` for the checkpoint, in `<eval-json-dir>` |

## Running

On a host with a CUDA GPU, the checkpoint, the datasets and SwarmMed's Python
environment, from the repository root:

```bash
.venv/bin/python -m pip install grad-cam captum torchio einops x-transformers

MEDISWARM_PROJECT_ROOT=<mediswarm-checkout> \
PYTHONPATH=experiments/duke_mst_cli:experiments/duke_mst_cli/explain \
.venv/bin/python experiments/duke_mst_cli/explain/run_explainability.py \
    --checkpoint <checkpoint-path> \
    --ref-json-dir <eval-json-dir> \
    --duke-root <duke-root> \
    --odelia-root <odelia-root> \
    --output-dir results/duke_mst_cli/explainability \
    --cohorts duke MHA
```

Add `--skip-occlusion` for a CAM-only fast path (~2 min instead of ~20 min).

### One site's own cases

A training site can explain cases of its own share with the global model it holds,
for example its validation cases. `--site-institution` adds a cohort `site` for the
site's folder under `--duke-root`, and `--split` picks the split (a site's share has
`train` and `val`; the default is `test`):

```bash
MEDISWARM_PROJECT_ROOT=<mediswarm-checkout> \
PYTHONPATH=experiments/duke_mst_cli:experiments/duke_mst_cli/explain \
.venv/bin/python experiments/duke_mst_cli/explain/run_explainability.py \
    --checkpoint <checkpoint-path> \
    --duke-root <data-dir> \
    --site-institution node_A --split val --cohorts site \
    --output-dir results/duke_mst_cli/explainability_node_A_val
```

`<data-dir>` is the folder that holds the site's folder `node_A`, the site's
`DATA_DIR`. The predictions go to `per_case_predictions/site_node_A_val.csv`, and
the figures name the cohort `node_A`. These cases have no published AUROC, so the
check under [Validation](#validation) is skipped with a warning; the script still
computes their AUROC, so the split must hold malignant and other cases. `--split`
applies to every cohort of a run, so explain a site's cases in a run of their own.

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
the value published in `<cohort>_eval.json` under `--ref-json-dir` (by default
`results/duke_mst_cli/repetition_1` in the working directory) within
`--auroc-tolerance` (default 0.01). On the verified setup:

| Cohort | Measured | Published | Δ |
|--------|----------|-----------|---|
| MHA    | 0.6082   | 0.6082    | 0.0000 |
| Duke   | (filled in by `report.md`) | 0.9007 | (filled in) |

If the AUROC mismatches, the run aborts before producing figures.
