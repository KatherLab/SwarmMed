---
title: Explainability and case retrieval
description: Making an aggregated model's predictions inspectable at a site.
---

# Explainability and case retrieval

A swarm-trained model arrives at a site as a set of weights. Before a site relies
on it, the site needs to see *why* it predicts what it predicts on its own cases.
SwarmMedHub runs the project's `explainability.py` on the site's data and shows the
per-case explanations for review ([In SwarmMedHub](#in-swarmmedhub));
[Guidelines](#guidelines-for-explanations) gives general advice on choosing,
checking and reviewing explanations for any kind of data. The breast MRI worked
example also has stand-alone scripts for attribution maps and case retrieval
(sections 1 to 3). All of them run locally on the aggregated model and send nothing
to other sites.

Everything here happens **after** aggregation, on the final global checkpoint,
using the site's own data.

## In SwarmMedHub

A project can include an `explainability.py`. The Hub runs it in its script sandbox
on a site's data and a job's model, like `results_visualization.py`, and shows the
cases it explains on the **Results** page, where project members review them. The
numbered sections further down describe the breast MRI worked example's own scripts,
which run outside the Hub on a GPU; their output can be converted and imported (see
below).

### How it works

1. Add `explainability.py` to the project: on the project form (field **Explainability
   Script**; the form also lists and removes it) or at the root of a code directory
   (`swarmed project create|update --code-dir`). It is stored apart from the training
   code and never sent to the sites' training jobs. A web *folder* upload stores every
   `.py` file as training code, so use the field.
2. On the **Results** page, select a job and click **Run Explainability** in the
   **Explainability** section, or run `swarmed results explain`. Starting a new run
   cancels the pending or running runs of the same job that you may stop (your own;
   the project author may stop any run). While a run of the job started by another
   member is pending or running, the start is refused. **Stop** (for the user who
   started the run and the project author) marks the run cancelled and removes its
   sandbox container; the worker then stores nothing and removes the run's temporary
   folders.
3. The Celery worker runs the script in the sandbox (run type `explainability`): the
   run folder holds the script, a wrapper and a copy of the `swarmmed_explain` package;
   the manifest lists the site's data files and the job's result files
   (`results/<participant>/<file>`), as for results visualization.
4. Nothing is stored unless the script exits normally within the time limit and its
   output passes the bundle checks: fixed file names only, no symbolic links or other
   special files, size limits, strict JSON, and every PNG decoded and re-encoded (which
   drops any text stored in the PNG). Otherwise the run is marked as failed with the
   reason and the end of the script's output.
5. The Hub stores the cases of every run, computed or imported, in a random order
   (drawn from the operating system's random source when the run is stored), so a
   case's position says nothing about its outcome; the gallery, the bundle download
   and the CLI use that order.
6. The section shows the run's methods, target, notes and metrics, the run-level
   figures, a gallery of the cases (case ID, outcome, label, prediction, score and the
   first details) and, for a selected case, its panels side by side, its details, the
   review form and each reviewer's latest review. **Download bundle** saves the run as
   a zip archive in the bundle format; **Delete** removes a run, its images and its
   reviews (allowed for the user who started or imported the run and for the project
   author; once other users have reviewed the run, only the project author can delete
   it).

![Explainability section with run information, run-level figures and the case gallery](assets/usage/explainability_light.png#only-light)
![Explainability section with run information, run-level figures and the case gallery](assets/usage/explainability_dark.png#only-dark)

*The Explainability section for the ECG template on [PTB-XL](https://physionet.org/content/ptb-xl/1.0.3/) (CC BY 4.0), from a run on a
fresh SwarmMedHub installation (12 November 2026): run information, the three run-level
figures and the gallery of eight cases in random order.*

### The script contract

The sandbox defines a global `explain` before the script runs:

```python
explain.open(path, mode="rb") / explain.exists(path) / explain.listdir(path)
explain.get_model(client_name="fl-client-1", model_filename="model.pt")
explain.load_weights(model, client_name="fl-client-1", model_filename="model.pt")
explain.add_case(case_id, *, label=None, prediction=None, score=None, target=None,
                 outcome=None, details=None)
explain.add_panel(case_id, method, title=None)  # saves the current matplotlib figure
explain.add_summary(title)                      # saves it as a run-level figure
explain.set_info(methods=[...], target=..., notes=..., metrics={...})
```

- At most 24 cases, 4 panels per case and 4 run-level figures. Each call checks its
  values and raises `ValueError` when one breaks a rule, so mistakes stop the script.
- `case_id` is the site's pseudonymous ID (letters, digits and `. _ : -`, at most 128
  characters). `outcome` is `TP`, `FP`, `FN`, `TN`, `correct`, `incorrect` or `None`.
  `details` is a small flat dict of text, numbers or booleans; keys containing words
  such as `name`, `patient`, `birth` or `mrn` are refused.
- A panel whose method is `raw` or `input` shows the model's input rather than a map.
  Before a read is saved, blinded review shows only a case's ID, its label and these
  panels ([Reviewing cases](#reviewing-cases)); add one so that reviewers can make
  their own read from the input. The templates in `examples/` add one first in every
  case.
- The method library `swarmmed_explain` (occlusion, integrated gradients, gradient ×
  input, Grad-CAM, feature ablation, permutation importance, case selection by outcome,
  rank agreement between methods, and plots for images, signals, spectrograms and
  feature bars) can be imported. Its README (`swarmmed_explain/README.md`) has a
  complete example, and each template in `examples/` has an `explainability.py`. The
  [Developer Guide](developer.md#explainability-scripts) lists the full interface.

### Reviewing cases

![One case with its input panel, method panels, details and review form](assets/usage/explainability_case_light.png#only-light)
![One case with its input panel, method panels, details and review form](assets/usage/explainability_case_dark.png#only-dark)

*A PTB-XL record (CC BY 4.0), one case opened: label, prediction, score and outcome, the input panel (the 12-lead
record) followed by the method panels, the case details and the review form.*


A review has a verdict (agree, disagree or unsure) and an optional note of at most
2,000 characters. Reviews are never changed: saving again adds a new review, the page
shows each reviewer's latest one, and `swarmed results explanations --reviews` lists
all of them. The project author and members can review; users with emergency
(break-glass) access can view but not review. The reviewer is always the logged-in
user.

**Blinded review** is a per-reviewer switch, kept per run. A reviewer who opens a
project's explanations for the first time starts in blinded mode; later runs of the
project start in the mode the reviewer last chose.

In blinded mode, as long as the run has not been seen (below), a case the reviewer
has not yet reviewed shows only its case ID, its label and its input panels (method
`raw` or `input`). Everything that comes from the
model stays hidden until the reviewer saves a read of that case: prediction, score,
outcome, target, details, all other panels and other reviewers' reviews. The Hub
leaves them out of what it sends to the browser and does not serve the image of a
hidden panel. The run-level figures and the script output of a successful run stay
hidden until every case has been read. The run information from `set_info` (methods,
target, notes and metrics such as a test AUROC) is shown from the start, so it must
not hold anything about single cases. The gallery shows the cases in the random
order fixed when the run was stored (step 5 above), so a case's position says nothing
about its outcome. A blinded read records whether the reviewer agrees with the case's
label and, in the note, their own read (for example the finding and where it is);
after it is saved, the model output of that case appears and a further review records
the verdict on the model output and its explanation.

Once a run's model output has been given to the reviewer, the run counts as seen for
that reviewer. This happens when the reviewer switches blinded review off, downloads
the bundle, lists or exports the run with the CLI, or gets a map panel or run-level
figure while not in blinded mode. From then on all cases of the run are shown, even
if blinded review is switched on again; the page then says that the run's output was
already shown, and the reviews the reviewer saves for that run are stored and shown
as made after seeing the output. The page asks for confirmation before it switches
blinded review off or downloads the bundle of a run that is being read blinded.

The Hub sets each review's `blinded` flag itself: it is set only if the case was
shown with its output hidden, that is, blinded mode was on, the run had not been seen
by the reviewer, and the reviewer had not reviewed the case before. This implements
item 4 of the review in section 3 below.

![Blinded review before the reviewer has saved a read](assets/usage/explainability_blinded_light.png#only-light)
![Blinded review before the reviewer has saved a read](assets/usage/explainability_blinded_dark.png#only-dark)

*Blinded review of a PTB-XL record (CC BY 4.0) before a read is saved: each case shows its ID, its label and the
input panel; prediction, score, outcome, target, details and the map panels are
hidden, and the run-level figures and the script output wait until every case has
been read.*

### Explanations computed elsewhere

A model that is too large or too slow for the sandbox can be explained at the site
with `python -m swarmmed_explain.run_local` (same script, same bundle, no sandbox
limits, GPU allowed), and the breast MRI script's output can be converted with
`experiments/duke_mst_cli/explain/export_hub_bundle.py --input <run_explainability
output folder> --out <bundle folder>`. The converter copies only the selected cases'
per-method PNGs (`raw`, `gradcam`, `gradcam++`, `oca`), the two composite figures and
derived metadata (labels, prediction, probability of malignancy, outcome, cohort, the
AUROC check); never the attribution arrays, the per-cohort prediction tables or the
logs. Either bundle is imported with `swarmed results explain-import`, which checks
it like sandbox output.

### Command line

```bash
swarmed results explain --project <PROJECT_UUID> --job <JOB_UUID> --wait
swarmed results explanations --project <PROJECT_UUID> --job <JOB_UUID> [--out DIR]
swarmed results explain-import --project <PROJECT_UUID> --job <FLARE_JOB_UUID> --dir DIR
```

See the [CLI reference](cli.md#10-explain-the-models-predictions) for the options.

### Limits

Explainability runs have their own sandbox limits, set in `.env` for the Celery worker:
`SWARMMEDHUB_EXPLAIN_MEM_LIMIT` (default `2g`), `SWARMMEDHUB_EXPLAIN_CPUS` (default `2`)
and `SWARMMEDHUB_EXPLAIN_TIMEOUT` (seconds, default `900`, at most `3000`). Validation
and visualization scripts keep 1 CPU, 1 GB and 300 s. The sandbox daemon
(`sandbox-dind`) is limited to 4 GB of memory, so raising the memory limit beyond that
has no effect. The script's threads follow the container's CPU limit.

### Privacy

Explanations, reviews and case embeddings are derived from patient data: a panel
shows a case's own signal, image slices or feature values, a review describes the
case, and an embedding is a derived representation of the patient's data (case
retrieval, section 2). Treat them as patient data. They stay at the site:

- The script runs in the site's own Hub on that site's data, in the sandbox, whose
  containers cannot reach the platform's database or Docker proxy. The images are
  stored in the Hub's own object storage under `<project>/plots/explanations/`
  (encrypted at rest like the other stored files); they are not result files, so the
  results listing and **Export All** do not include them, and nothing is sent to other
  sites or to the coordinating server. Hub backups that include object storage do
  include them.
- Reviews stay in the site's database; notes are encrypted at rest. A user's own runs
  and reviews are part of their personal data export (without the cases' IDs).
- Deleting a run removes its images and reviews; deleting a training job or the
  project does the same.
- Use pseudonymous case IDs and keep identifiers out of `details`. The checks on
  `case_id` and `details` guard against mistakes; they are not an anonymisation step.
  Pictures can show identifying content that no check detects. A downloaded bundle or
  a CLI export is as sensitive as the data it was made from.
- Sharing explanations or case embeddings with other sites needs its own legal basis,
  as for case retrieval (section 2).

### Status

As of 12 November 2026 this was tested with the project's test suite (models, bundle
ingestion including malicious bundles, the random case order, the worker task with
the sandbox mocked, stopping a run, views including access checks, escaping and
blinded review, the CLI, the migrations) and once end to end on 11 November: a test
script on synthetic tabular data (8 cases, 3 panels each) ran through the worker's
code path in a local container with the sandbox's limits (2 GB, 2 CPUs, no network,
unprivileged user) using the Hub runtime image, in 3.5 s. The Results page section
was exercised in headless Chromium against a test server with synthetic cases, on
11 November (blinded read, reveal, review, run start and completion, download) and
on 12 November after the blinded-review rule above was introduced (a first-time
reviewer starting in blinded mode, a blinded read, blinded review switched off and on
again). On 12 November 2026 the `explainability.py` of each of the six templates ran in
the sandbox (`sandbox-dind` with the `swarmmedhub-sandbox` image) of a fresh SwarmMedHub
installation, on the model trained there, through `swarmed results explain`: 7 or 8
cases per run, 3 or 4 panels per case with the input panel first, 2 or 3 run-level
figures, each run in 6 to 15 seconds ([Templates](templates.md)). The
converter was run on the recorded breast MRI output described under Status below
(16 cases, 64 panels, 2 figures; the import checks accepted the bundle).

## Guidelines for explanations

These guidelines hold for any `explainability.py`, whatever the data. They describe
what `swarmmed_explain` implements and how the templates in `examples/` use it; the
Hub does not enforce them.

### Choosing methods for the data

| Data | Per-case methods | Used in |
|---|---|---|
| Tabular data, EHR | Feature ablation (one variable at a time set to a reference value) and integrated gradients, drawn as bars; permutation importance[^perm] on the test split as a run-level figure | `tabular_heart_disease`, `ehr_eicu_mortality` |
| Multichannel signals (ECG, EEG) | Integrated gradients and occlusion of short windows of each lead or channel, drawn behind the signal | `ecg_ptbxl`, `eeg_sleep_staging` |
| Spectrograms (heart sounds) | Occlusion of time-frequency patches and integrated gradients | `audio_heart_sounds` |
| Signal plus metadata | The signal methods with the metadata held fixed, plus integrated gradients and group ablation for the metadata with the signal held fixed | `multimodal_ptbxl` |
| Images and volumes | Grad-CAM[^gradcam] on a convolutional layer, occlusion of patches, integrated gradients | No template yet; the breast MRI scripts (section 1) use Grad-CAM, Grad-CAM++[^gradcampp] and occlusion |

- **Occlusion and feature ablation**[^occlusion] need no gradients and work with any
  model: they measure how much the output drops when part of the input is replaced
  by a reference. They cost one forward pass per window or feature.
- **Integrated gradients**[^ig] needs a differentiable model. Its attributions add up
  to the difference between the output for the case and for the reference, which
  gives a check (below).
- **Gradient × input**[^gxi] and **saliency**[^saliency] need one backward pass and
  are a quick first look; saliency shows sensitivity, not contribution.
- **The reference decides the question.** Occlusion, feature ablation and integrated
  gradients explain the difference from a reference input: zeros (a flat line for a
  standardised signal, silence for audio, black for an image) or the training mean
  or median for a table row. Choose one that means "nothing remarkable" for your
  data and name it in `set_info(notes=...)`.
- **Name the explained output** with `set_info(target=...)`. The binary templates
  explain the logit of the positive class, the sleep staging template the predicted
  stage; the breast MRI maps explain the malignant class whatever the prediction.

### Choosing cases

- Select by outcome, errors included. `select_cases` takes the most confident true
  positives, false positives, false negatives and true negatives of a binary task,
  or the most confident correct and incorrect cases of a multiclass task. The
  binary templates take two cases per outcome, the sleep staging template four
  correct and four incorrect epochs: 8 cases at most, from the test split, never
  from the training data. `select_cases` returns the cases grouped by outcome; the
  Hub stores them in a random order, so that order does not show in the gallery.
- Confident errors are often the most informative cases: they show what the model
  relies on when it is wrong.
- The most confident cases are extremes and say nothing about cases near the
  decision threshold. Add such cases yourself if the review needs them (at most 24
  cases per run).

### Checking the explanations

- **Completeness.** For integrated gradients, `swarmmed_explain` returns
  `completeness_gap`: the sum of the attributions minus the output difference between
  the case and the reference. Every template stores it with each case. A gap that is
  not small compared with that difference means too few integration steps.
- **Agreement between two methods.** `rank_agreement` gives the Spearman correlation
  and the top-k overlap of two maps of the same case, and every template records it.
  When two methods disagree, treat the prediction as unexplained rather than picking
  the map that looks best.
- **What `swarmmed_explain` does not do.** It has no model or label randomisation
  test[^sanity], which shows whether a map changes at all when the trained weights
  are replaced by random ones; some methods give convincing maps for an untrained
  model. It does not compare maps with annotated regions or measure how faithful a
  map is. A site that needs such checks has to run them itself.

### Reviewing: blinded first, then reveal

- Record your own read before you see the model's output. With blinded review on
  (the default the first time you open a project's explanations), the Hub hides a
  case's prediction, score, outcome, target, details and maps until you save a read
  ([Reviewing cases](#reviewing-cases)); the output then appears, and a further
  review records your verdict on it and on its explanation. A read made after seeing
  a map is anchored by it.
- Give blinded reviewers something to read: a panel with the method `raw` or
  `input` shows the case's input and stays visible while the rest is hidden. The
  templates add such a panel first in every case.
- Record disagreements. They are the most useful result of a review for the
  consortium.

### What maps cannot show

- A map shows what the model's output is sensitive to, for the chosen reference and
  window. It does not show that the prediction is right or how the model reasons. A
  map that looks plausible does not make a prediction correct, and an odd one does
  not make it wrong. Explanations support a review; they do not justify a
  prediction[^ghassemi].
- Maps do not reveal confounding. If the label goes together with the site, the
  device or the population, the model can learn those, and its maps still highlight
  parts of the input. In the `audio_heart_sounds` template, the healthy volunteers
  and the patients of one source were recorded with different devices in different
  places; the model separates them perfectly there (AUROC 1.000). At the other
  sources it is much weaker: AUROC 0.71 (95% CI 0.58-0.83) at source a, and at or
  below chance at b and f. It has partly learned the recording conditions rather
  than heart disease. Confounding of this kind shows in per-site results and in how the
  data were collected, not in the map of a single case.
- Each plot scales its colours to the case's own attributions, so colour intensity
  cannot be compared between cases.

[^occlusion]: Zeiler MD, Fergus R. Visualizing and understanding convolutional networks. ECCV 2014. <https://arxiv.org/abs/1311.2901>
[^ig]: Sundararajan M, Taly A, Yan Q. Axiomatic attribution for deep networks. ICML 2017. <https://arxiv.org/abs/1703.01365>
[^gxi]: Shrikumar A, Greenside P, Shcherbina A, Kundaje A. Not just a black box: learning important features through propagating activation differences. 2016. <https://arxiv.org/abs/1605.01713>
[^saliency]: Simonyan K, Vedaldi A, Zisserman A. Deep inside convolutional networks: visualising image classification models and saliency maps. ICLR Workshop 2014. <https://arxiv.org/abs/1312.6034>
[^gradcam]: Selvaraju RR, Cogswell M, Das A, Vedantam R, Parikh D, Batra D. Grad-CAM: visual explanations from deep networks via gradient-based localization. ICCV 2017. <https://arxiv.org/abs/1610.02391>
[^gradcampp]: Chattopadhay A, Sarkar A, Howlader P, Balasubramanian VN. Grad-CAM++: generalized gradient-based visual explanations for deep convolutional networks. WACV 2018. <https://arxiv.org/abs/1710.11063>
[^perm]: Breiman L. Random forests. Machine Learning 45, 5-32 (2001), <https://doi.org/10.1023/A:1010933404324>; for the model-agnostic form, Fisher A, Rudin C, Dominici F. All models are wrong, but many are useful: learning a variable's importance by studying an entire class of prediction models simultaneously. JMLR 20(177), 2019, <https://arxiv.org/abs/1801.01489>.
[^sanity]: Adebayo J, Gilmer J, Muelly M, Goodfellow I, Hardt M, Kim B. Sanity checks for saliency maps. NeurIPS 2018. <https://arxiv.org/abs/1810.03292>
[^ghassemi]: Ghassemi M, Oakden-Rayner L, Beam AL. The false hope of current approaches to explainable artificial intelligence in health care. Lancet Digital Health 3(11), e745-e750 (2021). <https://doi.org/10.1016/S2589-7500(21)00208-9>

## 1. Breast MRI worked example: attribution maps

The scripts in `experiments/duke_mst_cli/explain/` explain the worked example's MST
model only; they need the MediSwarm model code and a CUDA GPU.

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
`--auroc-tolerance` (default 0.01). Pass `--ref-json-dir` explicitly: the default,
`results/duke_mst_cli/repetition_1` in the working directory, is where the original
run kept its files, and if the file is missing the check is skipped with only a
warning. Only the AUROC is compared, not the checkpoint
checksum, so a different checkpoint whose AUROC lies within the tolerance is not
detected. The check runs per cohort, so the heatmaps of earlier cohorts are
already written when a later cohort fails it.

A training site can also explain cases of its own share, for example its
validation cases, with the global model it holds: `--site-institution node_A
--split val --cohorts site`, with `--duke-root` set to the folder that holds the
site's folder (the site's `DATA_DIR`). These cases have no recorded AUROC, so the
check is skipped with a warning; their AUROC is still computed, so the split must
contain malignant and other cases. `--split` applies to every cohort of a run.

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

## 2. Breast MRI worked example: case retrieval

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

## 3. Using explanations in a review

A workable review, for each of a handful of cases:

1. The prediction and its probability.
2. The explanations side by side (for the worked example, the three maps above).
3. The five nearest reference cases with their outcomes.
4. The reviewing clinician's own read, recorded before seeing the model output
   where that is practical.

Attribution maps support clinical judgement; they do not justify a prediction and
they are not evidence of causal reasoning by the model. A map that looks
anatomically sensible does not make a prediction correct, and a map that looks
odd does not make it wrong. Record disagreements — they are the most useful
output of the review for the consortium.

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
