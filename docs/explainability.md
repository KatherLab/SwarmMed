---
title: Explainability and case retrieval
description: Stage 6 — making an aggregated model's predictions inspectable at a site.
---

# Explainability and case retrieval (Stage 6)

A swarm-trained model arrives at a site as a set of weights. Before anyone uses
it clinically, the site needs to see *why* it predicts what it predicts on their
own patients. This page describes the two tools the protocol provides for that,
both of which run locally on the aggregated model and never send anything back to
the consortium.

Everything here happens **after** aggregation, on the final global checkpoint,
using the site's own data.

## 1. Attribution maps

Three complementary methods are provided, because no single one is trustworthy on
its own:

| Method | What it shows | Why it is included |
|---|---|---|
| **Grad-CAM** | Where the hooked layer's activations, weighted by the gradient of the malignant-class (class 2) logit, are large, projected back onto the image | The familiar baseline |
| **Grad-CAM++** | The same, with better behaviour when several regions contribute | Multi-focal disease |
| **Occlusion (OCA)** | How much the predicted probability of malignancy drops when a patch of the image is replaced by its own mean | Model-agnostic, uses no gradients, so it is a genuine check on the two above |

All three explain the malignant class (class 2 of no lesion, benign, malignant)
for every case, whatever the model predicted. For a case the model called benign
or no lesion, including the false negatives and true negatives the script
selects, a map shows what pushes the model towards malignancy, not why it chose
the other class. Each map is also scaled to [0, 1] per case, so brightness cannot
be compared between cases: a case with a malignancy probability of 0.01 can get as
bright a map as one with 0.99.

MST's slice encoder is a vision transformer (DINOv2 ViT-S/14) whose only
convolution is the patch embedding, so the gradient methods attach to the layer
normalisation before self-attention (`norm1`) in the last block of that per-slice
encoder (`model.mst.backbone.blocks[-1].norm1`; `--cam-last-n-blocks N` averages
the last N blocks), not to the slice-fusion transformer that follows it. The
16 × 16 grid of patch tokens (CLS token dropped) is reshaped and upsampled to the
slice resolution. The occlusion method needs no internals at all: it slides a
16 × 16-pixel patch (stride 8, filled with the patch's own mean) over every fourth
slice by default (`--occlusion-slice-stride 4`, so 9 of the 32 slices, including
the last), records the drop in the predicted probability of malignancy (increases
are clipped to zero), and copies each swept slice's scores to the neighbouring
slices that were not swept.

When the three disagree, treat the prediction as unexplained rather than picking
the prettiest map.

### Running it

The script imports MediSwarm's MST model and dataset code. It finds that code
under `/MediSwarm` inside the MediSwarm training image, or on any other host
through `MEDISWARM_PROJECT_ROOT` pointing at a MediSwarm checkout. Use the
MediSwarm version the model was trained with (for the worked example's
checkpoints, the code in `odelia:paper-apr2026`): the loader silently skips
checkpoint weights that do not fit the model, so the AUROC check described below
is the only guard against a mismatch. Inside that image only `grad-cam` needs to
be added; elsewhere install both `experiments/duke_mst_cli/requirements.txt` and
`experiments/duke_mst_cli/explain/requirements.txt`. A CUDA GPU is required. MST
loads its DINOv2 backbone through `torch.hub`, so the first run needs internet
access or a populated `TORCH_HOME` cache.

```bash
cd experiments/duke_mst_cli
MEDISWARM_PROJECT_ROOT=/path/to/MediSwarm python explain/run_explainability.py \
    --checkpoint /path/to/FL_global_model.pt \
    --ref-json-dir /path/to/eval_jsons_for_this_checkpoint \
    --duke-root  /mnt/data/DUKE_iid \
    --odelia-root /mnt/data/medswarmdata \
    --output-dir results/explainability \
    --cohorts duke MHA
```

The script selects the most confident cases per outcome — true positive, false
positive, false negative, true negative — per cohort (two each by default,
`--cases-per-outcome`), so the review covers the model's failures and not only
its successes. Positive means malignant, and the prediction is the most probable
of the three classes; true and false positives are those with the highest
probability of malignancy, false and true negatives those with the lowest.

It also recomputes each cohort's AUROC and compares it with the AUROC in
`<cohort>_eval.json` under `--ref-json-dir` (the `--output-json` files of
`evaluate_global_model.py` for that checkpoint, named `duke_test_eval.json` and
`odelia_<cohort>_eval.json`), aborting if they differ by more than
`--auroc-tolerance` (default 0.01). Pass `--ref-json-dir` explicitly: the default
is a hard-coded path from the original run, and if the file is missing the check
is skipped with only a warning. Only the AUROC is compared, not the checkpoint
checksum, so a different checkpoint whose AUROC lies within the tolerance is not
detected. The check runs per cohort, so the heatmaps of earlier cohorts are
already written when a later cohort fails it.

Occlusion dominates the run time. In the recorded run of the default 16 cases
(83 minutes on one NVIDIA Quadro RTX 6000), occlusion took about 5 minutes per
case at the default patch size, stride and slice stride, and everything else took
about 3 minutes in total. Add `--skip-occlusion` for a gradient-only pass.

Output: per-case attribution maps as `.npy` plus rendered PNGs, a per-cohort
prediction table, `selected_cases.csv`, `metrics_check.json` with the AUROC
check, two composite figures, and a `report.md` listing the selected cases. The
report's title, checkpoint MD5 and "Method notes" are fixed text from the
original run (the notes describe averaging over the last four blocks and
smoothing with sigma 1.5, whereas the defaults are one block and no smoothing),
so do not rely on them for provenance.

## 2. Case retrieval

Clinicians rarely ask only "where did the model look"; they ask "what similar
cases have we seen, and how did they turn out". `explain/case_retrieval.py`
answers that with the aggregated model as the similarity measure.

The embedding is the input to the model's final linear (classification) layer,
captured during an ordinary forward pass. The site embeds its own reference set
once, and a query exam is matched against it by cosine similarity. The
neighbours come back with their known outcomes. The script loads the model the
same way as the attribution script, so it has the same requirements, including a
CUDA GPU.

```bash
# once: embed the site's reference set (labels known)
python explain/case_retrieval.py build \
    --checkpoint /path/to/FL_global_model.pt \
    --data-root /mnt/data/DUKE_iid --institution node_A --split train \
    --out reference_embeddings.npz

# per query
python explain/case_retrieval.py query \
    --checkpoint /path/to/FL_global_model.pt \
    --data-root /mnt/data/DUKE_iid --institution test --split test \
    --reference reference_embeddings.npz \
    --uid Breast_MRI_002_left --top-k 5 --output-json retrieval.json
```

Each result reports the neighbours, their similarity, their known labels, and the
fraction of neighbours that were positive (malignant, by default) — a quick
indication of whether the model has placed this patient among cases like the ones
it was trained to call positive.

### Why this stays inside one site

The embedding is a derived representation of a patient's images and must be
treated as patient data. Retrieval across sites would mean exchanging embeddings,
which is exactly the kind of transfer the protocol exists to avoid, and would
require its own legal basis and privacy analysis. The implementation is therefore
deliberately single-site: the cache is written locally and never leaves the host.
A consortium that wants cross-site retrieval should treat it as a separate study
with its own approval, not as a feature of the training protocol.

## 3. Using these in a clinical review

A workable Stage 6 review, for each of a handful of cases:

1. The prediction and its probability.
2. The three attribution maps side by side.
3. The five nearest reference cases with their outcomes.
4. The reviewing clinician's own read, recorded before seeing the model output
   where that is practical.

Attribution maps support clinical judgement; they do not justify a prediction and
they are not evidence of causal reasoning by the model. A map that looks
anatomically sensible does not make a prediction correct, and a map that looks
odd does not make it wrong. Record disagreements — they are the most useful
output of this stage for the consortium.

## Status

The attribution outputs on record come from one aggregated swarm model: the
second of the three swarm repetitions in the statistics (FLARE job
`37bc58d0-9425-4180-9846-7273cd55d2bf`, checkpoint MD5
`172f8bcf98f1fb6199261be8bbdfc9a3`), which the explainability outputs and
`experiments/duke_mst_cli/explain/README.md` call "Repetition 1". The run covered
the internal cohort (Duke test split, 262 exams) and one external cohort (MHA, 40
exams, 11 malignant) and reproduced the recorded cohort AUROCs exactly (0.9007 and
0.6082). Of the three swarm repetitions, this checkpoint has the highest Duke
AUROC (0.875, 0.901 and 0.882 for repetitions 1 to 3) and the lowest MHA AUROC
(0.646, 0.608 and 0.712); keep that in mind when reading its MHA maps.

About five hours after the run, its `.npy` arrays were rewritten with
`recompute_cam.py`, which recomputes the Grad-CAM and Grad-CAM++ maps and raises
the occlusion maps to a power (2.5 by default), and the composite figures were
redrawn from them with `render_from_npy.py`. The parameters used were not
recorded, and the per-method PNGs written during the run were not updated. None
of these outputs are in the repository (`results/` is git-ignored).

The retrieval module is provided and documented here but has not yet been run on
study data, at a single site or across sites.
