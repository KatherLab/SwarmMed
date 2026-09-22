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
| **Grad-CAM** | Where the gradient of the predicted class is large, projected back onto the image | The familiar baseline |
| **Grad-CAM++** | The same, with better behaviour when several regions contribute | Multi-focal disease |
| **Occlusion (OCA)** | How much the predicted probability drops when a patch of the image is hidden | Model-agnostic, uses no gradients, so it is a genuine check on the two above |

For a transformer backbone there is no "last convolutional layer" to hook, so the
gradient methods attach to the layer normalisation before the final attention
block; the token grid is reshaped and upsampled to the slice resolution. The
occlusion method needs no internals at all: it sweeps a patch across each slice
and records the change in predicted probability.

When the three disagree, treat the prediction as unexplained rather than picking
the prettiest map.

### Running it

```bash
cd experiments/duke_mst_cli
python explain/run_explainability.py \
    --checkpoint /path/to/FL_global_model.pt \
    --duke-root  /mnt/data/DUKE_iid \
    --odelia-root /mnt/data/medswarmdata \
    --output-dir results/explainability \
    --cohorts duke MHA
```

Add `--skip-occlusion` for a gradient-only pass (about two minutes instead of
twenty). The script selects the top cases per outcome — true positive, false
positive, false negative, true negative — per cohort, so the review covers the
model's failures and not only its successes. It also recomputes the cohort AUROC
and asserts it matches the value recorded for that checkpoint, which catches a
mismatched checkpoint or data root before anyone interprets a heatmap.

Output: per-case attribution maps as `.npy` plus rendered PNGs, a per-case
prediction table, and a `report.md` summarising the selected cases.

## 2. Case retrieval

Clinicians rarely ask only "where did the model look"; they ask "what similar
cases have we seen, and how did they turn out". `explain/case_retrieval.py`
answers that with the aggregated model as the similarity measure.

The classification head is bypassed so the model emits its latent embedding; the
site embeds its own reference set once, and a query exam is matched against it by
cosine similarity. The neighbours come back with their known outcomes.

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
fraction of neighbours that were positive — a quick indication of whether the
model has placed this patient among cases like the ones it was trained to call
positive.

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

The attribution pipeline has been run on the aggregated swarm model for the
internal (Duke) and one external (MHA) cohort, and reproduces the recorded cohort
AUROC exactly. The retrieval module is provided and documented here; it has not
yet been exercised in a multi-site study.
