# swarmmed_explain

Per-case explanations for models trained with SwarmMed, and the bundle format in which
SwarmMedHub receives them. A project's `explainability.py` runs in the Hub sandbox (like
`results_visualization.py`) with a helper object `explain`. It picks cases from the site's
test data, computes attribution maps with the methods here, draws them with the plotting
helpers and saves each figure as a panel of a case. The Hub then shows the cases and lets
reviewers annotate them.

The package needs numpy, torch and matplotlib for the methods and plots, and Pillow for
re-encoding PNGs. `swarmmed_explain.bundle` and `swarmmed_explain.helper` import only the
standard library at module level, so the Hub can check bundles without torch or
matplotlib. No module imports Django.

## Privacy

Explanations are derived from patient data. A panel shows a case's own ECG trace,
spectrogram, image slices or feature values, so a bundle is as sensitive as the data it
was made from. It stays at the site:

- The library only reads the files listed in the run's manifest (in the sandbox, from
  the Hub's own object store) and writes the bundle to a local folder. It uploads
  nothing.
- `case_id` must be the site's pseudonymous ID. The format check (letters, digits,
  `. _ : -`) cannot tell a pseudonym from a real record number; that is the site's
  responsibility.
- `details` must not hold direct identifiers. Keys containing `name`, `patient`, `birth`,
  `dob`, `mrn`, `address`, `phone`, `email`, `ssn`, `insurance` or `accession` (in any
  case) are refused. This is a guard against mistakes, not an anonymisation step: values
  are not inspected, and pictures can show identifying content (faces in head scans,
  burned-in text) that no check here detects.
- `run_local` writes the bundle on the machine it runs on. Copy a bundle only to systems
  that are allowed to hold the site's patient data.

## Modules

| Module | Contents |
|---|---|
| `methods` | `occlusion`, `feature_ablation`, `integrated_gradients`, `gradient_x_input`, `saliency`, `grad_cam`, `permutation_importance`; helpers `as_predict_fn`, `roc_auc`, `accuracy` |
| `selection` | `select_cases` (most confident cases per outcome), `binary_outcome` |
| `agreement` | `rank_agreement`, `spearman`, `top_k_overlap`, `rankdata`, `resolve_k` |
| `plotting` | `plot_feature_bars`, `plot_signal`, `plot_spectrogram`, `plot_image`, `plot_volume`, `symmetric_limit`; from `swarmmed_explain.plotting` only: `plot_feature_table`, `method_style`, `METHOD_STYLES`, `AGREEMENT_COLORS` |
| `helper` | `ExplainHelper` (the `explain` object), `run_explain_script`, `wrapper_source`, `copy_package`, `thread_limit` |
| `bundle` | schema constants, `BundleWriter`, `write_bundle`, `validate_bundle`, `read_bundle`, `safe_copy_bundle`, `validate_cases`, `loads_cases`, `dumps_cases`, `sanitize_png`, `check_png`, `check_details`, `describe` |
| `run_local` | `python -m swarmmed_explain.run_local`: run `explainability.py` on a local machine the way the sandbox runs it |

The names in the table are also importable from `swarmmed_explain` directly; they are
loaded on first use.

## Writing explainability.py

The sandbox defines a global `explain` before the script starts:

```python
explain.listdir(relative_path="")              # manifest paths below a folder, sorted
explain.exists(relative_path)                   # a file or a folder that holds one
explain.open(relative_path, mode="rb")          # read-only; streamed for URLs
explain.find_model(client_name="fl-client-1", model_filename="model.pt")
explain.get_model(client_name="fl-client-1", model_filename="model.pt")
explain.load_weights(model, client_name="fl-client-1", model_filename="model.pt", *, strict=False)

explain.add_case(case_id, *, label=None, prediction=None, score=None, target=None,
                 outcome=None, details=None)    # returns the case position, 1 to 24
explain.add_panel(case_id, method, title=None, *, figure=None)  # saves and closes a figure
explain.add_summary(title, *, figure=None)      # run-level figure
explain.set_info(*, methods=None, target=None, notes=None, metrics=None)
explain.case_ids                                # the case IDs added so far
```

The manifest lists the site's data under their own paths (for example
`fl-client-1/test.npz`) and the job's result files under `results/<participant>/<file>`.
`get_model` looks for `results/<client_name>/<model_filename>`, then that file name in any
results folder, then `FL_global_model.pt`, `best_FL_model.pt`, `global_model.pt`,
`FL_model.pt` or `model_weights.npz`, then any file with a model extension. PyTorch files
are loaded with `weights_only=True`, and NVIDIA FLARE's checkpoint dict is unwrapped (the
state dict under `model`). `load_weights` uses `strict=False` by default and prints the
number of missing and unexpected keys; pass `strict=True` to fail instead.

`add_case`, `add_panel`, `add_summary` and `set_info` check every value when they are
called and raise `ValueError` (`swarmmed_explain.bundle.BundleError`) when one breaks a
rule, so mistakes stop the script instead of being dropped. `add_panel` and
`add_summary` save the current matplotlib figure (or `figure=`) as a PNG at 100 dpi and
close it. After the script ends, the runner marks the bundle complete and validates it.
A script that raises, or exits with a non-zero code, leaves an incomplete bundle that the
Hub does not import.

Give every case an input panel first: a figure of the case's input only (the record, the
image, the recorded values), with method `input` (or `raw`) and nothing computed by the
model in it or in its title. In the Hub a reviewer can read the cases blinded. A case the
reviewer has not read yet then shows only its case ID, its label and its input panels;
the prediction, score, outcome, target, details, all other panels and the other
reviewers' reviews stay hidden until the reviewer saves a read of that case, and the
summary figures until every case is read. The Hub stores and shows the cases of a run in
a random order, so a case's position says nothing about its outcome. Keep model output
out of case IDs and labels, since those are always shown.

A minimal script for a tabular binary task:

```python
import numpy as np
import torch
from torch import nn

from swarmmed_explain import (
    as_predict_fn,
    feature_ablation,
    integrated_gradients,
    permutation_importance,
    plot_feature_bars,
    roc_auc,
    select_cases,
)
from swarmmed_explain.plotting import plot_feature_table

FEATURES = ["age", "bp", "chol", "hr", "st"]
model = nn.Sequential(nn.Linear(5, 8), nn.Tanh(), nn.Linear(8, 1))
explain.load_weights(model, strict=True)

with explain.open("fl-client-1/test.npz") as handle:
    archive = np.load(handle)
    X, y, ids = archive["X"], archive["y"], archive["case_ids"]

scores = as_predict_fn(model, output="probability")(X)
reference = X.mean(axis=0)
for case in select_cases(y, scores, n_per_outcome=2):  # 2 each of TP, FP, FN, TN
    case_id = str(ids[case.index])
    explain.add_case(
        case_id,
        label=case.label,
        prediction=case.prediction,
        score=case.score,
        target="class 1",
        outcome=case.outcome,
    )
    x = X[case.index]
    plot_feature_table(FEATURES, x, reference=reference, title=f"Values of {case_id}")
    explain.add_panel(case_id, "input", "Recorded values")  # first; no model output
    plot_feature_bars(
        feature_ablation(model, x, baseline=reference),
        FEATURES,
        feature_values=x,
        title="Feature ablation",
    )
    explain.add_panel(case_id, "feature_ablation", "drop when set to the mean")
    ig = integrated_gradients(model, x, baseline=reference, steps=32)
    plot_feature_bars(ig.attributions, FEATURES, title="Integrated gradients")
    explain.add_panel(case_id, "integrated_gradients")

result = permutation_importance(model, X, y, roc_auc, n_repeats=3, seed=0)
plot_feature_bars(
    result.mean, FEATURES, errors=result.std, title="Permutation importance"
)
explain.add_summary("Permutation importance on the test split")
explain.set_info(
    methods=["feature_ablation", "integrated_gradients", "permutation_importance"],
    target="class 1",
    metrics={"auroc_test": roc_auc(y, scores)},
)
```

The test suite runs a script of this form end to end through `run_local`
(`tests/test_run_local.py`).

## Methods

Conventions shared by all methods:

- `x` is one case without a batch axis: `(features,)` for a tabular row,
  `(channels, samples)` for a signal, `(channels, height, width)` for an image or a
  spectrogram, `(channels, depth, height, width)` for a volume.
- `model` is a `torch.nn.Module` or a function from a batch to scores of shape `(batch,)`
  or `(batch, outputs)`. Perturbation methods call a function with a numpy batch;
  gradient methods call it with a torch batch and need a differentiable result.
- `target` is the output column to explain; it may be None only for a single-output
  model. `output="probability"` explains the sigmoid (one output) or softmax (several
  outputs) instead of the raw scores.
- A module is put in evaluation mode for the call and restored afterwards. Nothing is
  random except the seeded permutations of `permutation_importance`, so results repeat
  exactly on CPU.
- Attributions are float64 numpy arrays shaped like the input. Positive values support the
  explained output.

| Method | Function | What it computes | Fits | Cost per case |
|---|---|---|---|---|
| Occlusion | `occlusion(model, x, target, *, window, stride=None, baseline=0.0)` | Drop of the target score when a window is replaced by a baseline (a number, an array, or `"mean"` of the window); overlapping windows are averaged | Signals (windows over time, per lead or all leads together), spectrograms (time-frequency patches), images and volumes (patches); any model, no gradients needed | One forward pass per window position (refused above `max_evaluations=100000`) |
| Feature ablation | `feature_ablation(model, x, target, *, baseline, groups=None)` | Drop of the target score when a feature, or a group of features, is set to a reference value | Tabular and EHR rows, metadata vectors; groups for one-hot blocks or a value with its missing indicator | Features + 1 forward passes |
| Integrated gradients | `integrated_gradients(model, x, target, *, baseline=0.0, steps=64, rule="gausslegendre")` | Gradients integrated along the straight path from the baseline to `x`; the attributions add up to `f(x) - f(baseline)` up to `completeness_gap` | Any differentiable model: tabular, signals, spectrograms, images; a tuple `x` for models with several inputs (signal plus metadata) | `steps` forward and backward passes |
| Gradient x input | `gradient_x_input(model, x, target)` | `x * d f / d x` | A quick first look on any differentiable model | One forward and one backward pass |
| Saliency | `saliency(model, x, target, *, absolute=True)` | `abs(d f / d x)` (signed with `absolute=False`): sensitivity, not contribution | As above | One forward and one backward pass |
| Grad-CAM | `grad_cam(model, x, target, *, layer, relu=True, upsample=True)` | A convolutional layer's activations weighted by the spatial mean of their gradients, summed over channels, ReLU, resized to the input | CNNs on images (2-D), volumes (3-D), signals (1-D), spectrograms (2-D) | One forward and one backward pass |
| Permutation importance | `permutation_importance(predict, X, y, metric, *, n_repeats=5, seed=0, groups=None)` | Drop of a metric (for example `roc_auc`) when a feature, lead or channel is shuffled across cases | A run-level summary: tabular and EHR features, ECG leads, EEG channels | `n_repeats` x features passes over the data set |

Choosing among them:

- The baseline decides what an attribution means: zeros are a flat line for a standardised
  signal, silence for audio, black for an image; the training mean is usually the better
  reference for tabular rows. Say which one you used in `set_info(notes=...)`.
- `integrated_gradients` returns `completeness_gap`; when it is not small compared with
  `f(x) - f(baseline)`, increase `steps`. Recording it in a case's `details` lets reviewers
  see it.
- Two methods that disagree are a reason for caution. `rank_agreement(map_a, map_b)`
  returns the Spearman correlation and the top-k overlap (default k: 10% of the elements)
  as `{"spearman", "top_k_overlap", "k"}`; flatten it into `details` with your own key
  names (details must be flat), for example
  `{"ig_vs_occlusion_spearman": agreement["spearman"]}`.
- Permutation importance measures the whole test split, not one case, and features that
  are correlated share their importance.

Suggested combinations by data type:

| Data | Per-case panels | Summary figure | Plot |
|---|---|---|---|
| Tabular, EHR | feature ablation, integrated gradients | permutation importance | `plot_feature_bars` |
| ECG, EEG (multichannel signals) | integrated gradients, occlusion over time | permutation importance over leads or channels | `plot_signal` |
| Signal plus metadata | the above plus integrated gradients for the metadata input | | `plot_signal`, `plot_feature_bars` |
| Audio (spectrogram input) | occlusion over time-frequency patches, integrated gradients | | `plot_spectrogram` |
| Images, volumes | Grad-CAM, occlusion patches, integrated gradients | | `plot_image`, `plot_volume` |

## Case selection

`select_cases(y_true, scores, *, n_per_outcome=2, threshold=0.5, task="auto")` returns
`SelectedCase(index, outcome, label, prediction, score)` tuples. For a binary task
(scores of shape `(n,)` or `(n, 2)`) it takes the most confident cases of each outcome in
the order TP, FP, FN, TN: the highest scores for TP and FP, the lowest for FN and TN.
Pass `threshold=0.0` for logits. For a multiclass task (more score columns; pass
probabilities) it takes the most confident `correct` and then `incorrect` cases. Ties keep
the lower index first. An outcome with fewer cases contributes fewer.

## Plotting

Each function returns a matplotlib figure; call `explain.add_panel` next. Attributions are
drawn with a diverging scale centred on zero (red supports, blue opposes the output),
limited at the 99th percentile of their magnitude (`percentile=`). Without an attribution,
`plot_signal`, `plot_spectrogram`, `plot_image` and `plot_volume` draw the input alone, for
the input panel. Red and blue never identify a method: where two methods are compared in
one figure, `method_style(name)` gives each method the same grey and line style in every
template (`METHOD_STYLES`; `AGREEMENT_COLORS` for the two agreement statistics).

| Function | Input |
|---|---|
| `plot_feature_bars(values, names, *, errors=None, feature_values=None, max_features=15)` | One value per feature; largest magnitude at the top |
| `plot_signal(signal, attribution, *, fs=None, channel_names=None, channels=None)` | `(channels, samples)` or `(samples,)`; attribution shading behind each channel |
| `plot_spectrogram(spectrogram, attribution, *, times=None, freqs=None)` | `(freqs, frames)` or `(1, freqs, frames)`; one row per frequency bin, so with unevenly spaced `freqs` (mel bands) the ticks are labelled with the bins' frequencies and their spacing in Hz is uneven |
| `plot_image(image, attribution)` | `(H, W)`, `(H, W, 3)`, `(H, W, 4)` or `(C, H, W)` with C = 1, 3 or 4 |
| `plot_volume(volume, attribution, *, axis=0, n_slices=6, slices=None)` | `(D, H, W)` or `(1, D, H, W)`; without `slices`, the slices with the largest total attribution |
| `plot_feature_table(names, values, *, reference=None, value_header="value", reference_header="reference", missing="missing")` | One value per feature, as a table, optionally next to a reference column (for example the site's training median); NaN and None show as `missing`; for the input panel of tabular data |

## Bundle format

A bundle is one flat folder:

```text
cases.json            schema, run information, cases and their panels, summaries
case_01_panel_1.png   panel 1 of the first case (cases 01 to 24, panels 1 to 4)
summary_1.png         run-level figure 1 (1 to 4)
```

File names are fixed by position, so no name in a bundle is chosen by the script that
wrote it. `cases.json` (the values below are placeholders):

```json
{
 "schema": "swarmmed.explanations/1",
 "complete": true,
 "info": {"methods": ["integrated_gradients", "occlusion"], "target": "abnormal",
          "notes": "baseline: zeros", "metrics": {"auroc_test": 0.5}},
 "cases": [
  {"case_id": "fl-client-1:00042", "label": "1", "prediction": "1", "score": 0.5,
   "target": "abnormal", "outcome": "TP", "details": {"age_group": "60-69"},
   "panels": [{"method": "integrated_gradients", "title": "Lead II",
               "file": "case_01_panel_1.png"}]}
 ],
 "summaries": [{"title": "Permutation importance", "file": "summary_1.png"}]
}
```

`complete` is false while a script runs and true once it has finished. The same format is
used for sandbox output, local runs, the CLI export and import, and converters for other
explanation scripts.

### Limits

| Item | Limit |
|---|---|
| Cases | 24 per bundle, unique `case_id` matching `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` |
| Panels | 4 per case |
| Summary figures | 4 |
| `outcome` | `TP`, `FP`, `FN`, `TN`, `correct`, `incorrect` or null |
| `label`, `prediction`, `target` of a case | text, at most 64 characters (`explain.add_case` stores numbers and booleans as text) |
| `score` | a finite number or null |
| `details` | flat, at most 16 keys matching `[A-Za-z0-9_.-]{1,64}`, no identifier words (see Privacy); values text (at most 256 characters), finite numbers, booleans or null; at most 4 KiB as JSON |
| `info` | at most 16 method names (64 characters each), target 255 characters, notes 4,000 characters, at most 32 metrics (finite numbers or null) |
| Panel `method`, `title`; summary `title` | 64 and 255 characters |
| Text | no control characters (line breaks and tabs only in `notes`) |
| `cases.json` | 1 MiB, strict JSON: no NaN or Infinity, no duplicate keys, UTF-8, no unknown keys |
| PNG | 8 MiB and 40 megapixels each; re-encoded copies are at most 4096 pixels on the longer side and, after re-encoding, still at most 8 MiB each |
| Bundle | 256 MiB in total, before and after re-encoding; all images together at most 400 megapixels (`safe_copy_bundle` adds up the PNG headers and stops before decoding the image that passes the limit); only the fixed file names, regular files only |

### Checking and copying bundles

- `validate_bundle(path)` checks a folder: no symbolic links (folder or files), no hard
  links, regular files with the fixed names only, the size caps, a strict and valid
  `cases.json`, every referenced PNG present with a PNG header within the pixel limit, no
  unreferenced files, and `complete: true` (unless `require_complete=False`). It reads
  image headers only.
- `safe_copy_bundle(src, dst)` is for folders written by untrusted code. It opens the
  folder and every file with `O_NOFOLLOW` relative to one directory handle (files also
  with `O_NONBLOCK`, so a FIFO cannot block it), reads each file once within its cap,
  parses and validates `cases.json`, writes it again in canonical form, and decodes and
  re-encodes every PNG with Pillow (first frame, RGB or RGBA, all metadata dropped). It
  stops, before decoding, at the first image that brings the images' header sizes to more
  than 400 megapixels in all, and applies the 8 MiB and 256 MiB caps again to the
  re-encoded files.
  Nothing is written unless the whole bundle passes. It returns
  `{"data": <normalised cases.json>, "files": {name: {"width", "height", "bytes"}}}`.
- `write_bundle(dst, data, images)` writes a complete bundle in one step (for exports and
  converters); `BundleWriter(dst)` builds one case at a time (the `explain` helper uses
  it); `read_bundle(path)` returns the description and the PNG bytes.

## Running locally

```bash
python -m swarmmed_explain.run_local --script PATH/TO/explainability.py --data DATA_DIR \
    --model FL_global_model.pt --out BUNDLE_DIR [--client fl-client-1] \
    [--threads 2] [--timeout 900] [--force]
```

Run it from the repository root, so that Python finds the package; each template's
README gives the full command with its own paths. `--results RESULTS_DIR` (one folder
per participant) can replace `--model`. The data
folder is listed under its relative paths, hidden files skipped, and result files under
`results/...`. The script is copied into a new temporary folder next to a copy of this
package and run in a new Python process, as in the sandbox, so a script that needs other
local files fails here as it would in the Hub. `--force` replaces an earlier bundle in
`--out` and removes only bundle files. Exit codes: 0 bundle written and valid, 1 the
script failed or timed out, 2 bad arguments or an invalid bundle.

Unlike the sandbox, a local run limits neither memory nor network access; `--threads` and
`--timeout` approximate the CPU and time limits. This is also the route for a model that
needs a GPU: write the bundle at the site and import it into the Hub with
`swarmed results explain-import`.

## Integration with the Hub sandbox

For a run of type `explainability`, the worker:

1. Prepares the sandbox run folder (mounted at `/home/sandboxuser/run`, the working
   directory):
   - writes the project's script as `explainability.py`;
   - writes `script.py` with the text of `swarmmed_explain.helper.wrapper_source()`, whose
     defaults are the sandbox paths (`explainability.py`,
     `/home/sandboxuser/data/data_manifest.json`, output folder `explain`);
   - calls `swarmmed_explain.helper.copy_package(run_folder)`, which copies the runtime
     modules (not the tests or this README) to `run_folder/swarmmed_explain/`. The
     package is not built into the sandbox image.
2. Provides the manifest at `/home/sandboxuser/data/data_manifest.json`, as for
   `results_visualization.py`: the site's data files under their relative paths and the
   job's result files under `results/<participant>/<file>`, with presigned MinIO URLs as
   values. The helper points http(s) URLs at `minio:9000`, as the results-visualisation
   helper does, and never prints them.
3. Runs `script.py` in the sandbox image, as for the other run types, with the
   explainability limits (`SWARMMEDHUB_EXPLAIN_MEM_LIMIT`,
   default `2g`; `SWARMMEDHUB_EXPLAIN_CPUS`, default `2`; `SWARMMEDHUB_EXPLAIN_TIMEOUT`,
   default `900` seconds). The helper sets torch and BLAS threads to the container's CPU
   quota; `SWARMMED_EXPLAIN_THREADS` overrides it.
4. Imports nothing unless the container exited with code 0 within the time limit. Then it
   calls `swarmmed_explain.bundle.safe_copy_bundle(run_folder / "explain", staging)` with
   a new or empty private `staging` folder, stores the copied files and creates its
   records from the returned `data` and `files`. `BundleError` (a `ValueError`) means the
   bundle is rejected; its message says which rule failed. Files in the run folder are
   never served directly.

## Tests

The tests are in `tests/` and use no database. Run them like the project's other tests
(the repository's pytest configuration loads the Django settings):

```bash
python -m pytest swarmmed_explain/tests -q
```

On 12 November 2026, 200 tests passed in 22 s on CPU (Python 3.12, torch 2.10, numpy
2.4, matplotlib 3.10, Pillow 11.1). They cover: occlusion on a linear model with window 1
equal to `w * (x - baseline)`; integrated-gradients completeness on smooth networks
(float64 and float32); Grad-CAM shapes and non-negativity for 1-D, 2-D and 3-D layers and
agreement with the definition; feature ablation; permutation importance ranking the
informative feature first on synthetic data; case selection for binary and multiclass
tasks; rank agreement for identical and anti-correlated maps; bundle validation rejecting
symbolic and hard links, unknown names, oversize files, bad JSON, non-PNG data, too many
cases, panels or summaries and identifier-like detail keys; PNG re-encoding by
`safe_copy_bundle`; the limits of `ExplainHelper`; `run_local` end to end on a toy
model (8 cases, 24 panels, 1 summary); and the six templates' `explainability.py` on
small synthetic data (`tests/test_templates.py`: an input panel first, no model output in
it, neutral method colours).

## Limitations

- Attribution maps show what a model's output is sensitive to under a chosen baseline;
  they do not show that the model is right, and they have not been validated against
  clinical judgement. Reviewer annotations in the Hub are meant to collect exactly that
  judgement.
- Results depend on the baseline, the occlusion window and the Grad-CAM layer. Gradient
  methods can understate features on which a model saturates; Grad-CAM has the
  resolution of its layer.
- Everything runs on CPU inside the sandbox's limits. A model too large or too slow for
  2 CPUs, 2 GB and 15 minutes needs a local run.
- The tests ran on a host, in the project's development environment. The library was not
  run inside the sandbox image as part of this work: the worker path ran once (11 Nov
  2026) with the sandbox emulated by a container of the runtime image
  (`apps/results/test_explanations_sandbox_emulation.py`), not in the
  `swarmmedhub-sandbox` image.
