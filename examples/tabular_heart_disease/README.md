# Tabular template: coronary artery disease from the UCI Heart Disease data

A small tabular example for SwarmMed. Four hospitals each hold the clinical record of their own
patients, and a multilayer perceptron predicts from 10 routine variables whether angiography showed
coronary artery disease. The data are small (920 patients in all, a few kilobytes per site), so a
complete swarm run takes seconds of computation and the template is a good first project for
checking a SwarmMed installation. The template is meant to show that training, data checks, result
plots and explanations run on SwarmMed with natural hospital-level site splits. Its training has
run to completion on a SwarmMed local-test network (all sites on one host) and in NVIDIA FLARE's
simulator, and its data checks, data plots, result plots and explanations have run in a SwarmMedHub
sandbox (see [What was run](#what-was-run)). It does not make a performance claim.

## Task

Binary classification: heart disease present (`num` > 0 in the source files, label 1) or absent
(`num` = 0, label 0), from age, sex, resting blood pressure, serum cholesterol, fasting blood
sugar, maximum heart rate, exercise-induced angina, ST depression, chest pain type and resting
ECG result. Each site is one of the four source hospitals. Sites differ strongly in
prevalence (36% to 93% positive), in which variables were recorded, and in the case mix, so the
sites are not identically distributed.

## Data

- Dataset: UCI Heart Disease (four "processed" files, 14 attributes per patient).
- Authors: Andras Janosi (Hungarian Institute of Cardiology, Budapest), William Steinbrunn
  (University Hospital, Zurich), Matthias Pfisterer (University Hospital, Basel), Robert Detrano
  (VA Medical Center, Long Beach, and Cleveland Clinic Foundation).
- Citation: Janosi A, Steinbrunn W, Pfisterer M, Detrano R (1989). Heart Disease [Dataset]. UCI
  Machine Learning Repository. https://doi.org/10.24432/C52P4X. Paper: Detrano R et al.,
  Am J Cardiol 1989;64(5):304-310, https://doi.org/10.1016/0002-9149(89)90524-9.
- Licence: Creative Commons Attribution 4.0 International (CC BY 4.0), as stated on the dataset
  page. Keep the attribution above when you redistribute the data or derived files.
- Page: https://archive.ics.uci.edu/dataset/45/heart+disease
- Download (done by `prepare_data.py --download`):
  https://archive.ics.uci.edu/static/public/45/heart+disease.zip (about 129 kB). UCI publishes
  no checksums; `prepare_data.py` checks each file against the SHA-256 values it was written
  with (the files as downloaded on 11 Nov 2026) and stops if a file differs.

| Site folder | Source | Patients | Positive (disease) | Train / val / test |
|---|---|---|---|---|
| `fl-client-1` | Cleveland | 303 | 46% | 181 / 61 / 61 |
| `fl-client-2` | Hungarian (Budapest) | 294 | 36% | 178 / 58 / 58 |
| `fl-client-3` | VA Long Beach | 200 | 74% | 120 / 40 / 40 |
| `fl-client-4` | Switzerland (Zurich) | 123 | 93% | 73 / 25 / 25 |

What `prepare_data.py` does (numpy, pandas and the standard library only):

1. reads each hospital's processed file (or the UCI zip) and checks its SHA-256;
2. labels a patient 1 if `num` > 0, else 0;
3. splits each hospital's patients 60/20/20 into train, validation and test, stratified by
   label, with seed 42; exact duplicate rows stay in one split;
4. treats `?` and a recorded 0 for resting blood pressure and cholesterol as missing; fills
   missing continuous values with the training median, missing binary values with the training
   majority value and missing chest pain or ECG categories with the most frequent training
   category; z-scores the continuous variables and clips them to [-5, 5]; all statistics come
   from the hospital's own training split and nothing is pooled across hospitals;
5. writes `train.npz`, `val.npz`, `test.npz` and `site_info.json` per site, and checks that the
   counts and a fingerprint of the test cases match the values for seed 42.

The columns `slope`, `ca` and `thal` are not used: they are missing for most patients at three of
the four sites. `X_raw` in each file keeps the values before imputation (NaN = missing) for the
data checks and the explanation panels.

Switzerland has only 8 negative patients in all (2 in the validation split, 2 in the test split)
and records no cholesterol at all (the source stores 0); its AUROC and other class-based metrics are
not interpretable. Long Beach VA misses about 26% of several variables.

## Data layout

```text
fl-client-1/   train.npz  val.npz  test.npz  site_info.json     (Cleveland)
fl-client-2/   ...                                              (Hungarian)
fl-client-3/   ...                                              (VA Long Beach)
fl-client-4/   ...                                              (Switzerland)
```

Arrays in each `.npz`: `X` float32 `[n, 15]` (model input: 5 continuous variables, 3 binary,
chest pain type one-hot over 4 values, resting ECG one-hot over 3 values), `X_raw` the same
columns before imputation and scaling, `y` int64, `case_id` strings such as `cleveland-123`
(the source file and row, no direct identifier), `feature_names`.

- **Local-test network on one host:** import the `fl-client-k` folders themselves (see
  [Run it](#run-it)), so that every key starts with `fl-client-k/`. Each client reads only the
  folder named after its FLARE site name.
- **Real deployment:** each hospital runs `prepare_data.py --layout single --site-index K` and
  registers only its own four files, at the root of its data. The client then finds `train.npz`
  and `val.npz` at the root.

## Model

`HeartDiseaseMLP`: 15 inputs, one hidden layer of 32 units (ReLU, dropout 0.2), one output
logit (the log-odds of disease); 545 parameters. Each round a site scores the received global
model on its validation split, trains it for 10 local epochs (Adam, learning rate 1e-3, weight
decay 1e-4, batch size 32, new optimiser each round) and returns the weights with its number of
training cases as the aggregation weight. 10 swarm rounds (`SWARM_ROUNDS` in `training.py`). In a
plain-PyTorch simulation of the four sites over ten random splits, 10 rounds of 10 epochs gave the
same test AUROC as 20 rounds of 5 epochs (0.856 and 0.855 pooled, sd 0.02) with half the
rounds, which is why the template uses 10 x 10; on a local-test network the rounds, not the
computation, set the run time.

The test split is used only by `evaluate.py`, `results_visualization.py` and
`explainability.py`, never during training. Every site prints one line per round:

```text
SWARMMED_METRIC site=fl-client-1 round=9 split=val auroc=0.9329 auprc=0.9333 accuracy=0.8525 ...
```

## Files

| File | Role |
|---|---|
| `training.py` | Client-API training loop (needs only numpy, scikit-learn, torch) |
| `requirements.txt` | Pinned runtime packages |
| `validation.py` | 38 data checks in the Hub sandbox (shapes, labels, splits, duplicates, missing values, same features at all sites) |
| `visualization.py` | 4 data plots: patients by diagnosis, missing values, continuous variables, binary and categorical variables |
| `results_visualization.py` | 4 result plots: validation AUROC per round, ROC curves, test AUROC with bootstrap 95% CI, predicted probabilities |
| `explainability.py` | Per-case explanations (see Explainability) |
| `prepare_data.py` | Converts the UCI files into the layout above; run by a site before import; uploaded with `--code-dir` but not used by the platform |
| `evaluate.py` | Scores a saved global model on a site's test split (per site and pooled) |

`prepare_data.py` and `evaluate.py` are harmless in the code bundle but are not needed there.
Keep the prepared data outside this folder: every file in it is uploaded as training code.

## Run it

Prepare the data once, on the machine that holds the files. Run the commands from the
repository root: the CLI runs in the application container and reads only files inside the
repository, whose `tmp/` folder git ignores. `--download` fetches the UCI zip; leave it out if
the files are already in `--raw-dir`.

```bash
python examples/tabular_heart_disease/prepare_data.py --download \
  --raw-dir tmp/heart_disease/raw --out tmp/heart_disease/local-test
# one hospital only, for a real deployment:
python examples/tabular_heart_disease/prepare_data.py --raw-dir tmp/heart_disease/raw \
  --out tmp/heart_disease/site-2 --layout single --site-index 2
```

### Command line

Use your own user name and replace the UUIDs ([CLI](../../docs/cli.md)).

```bash
swarmed project create --user alice --title "Heart disease" \
  --description "Tabular template" --code-dir examples/tabular_heart_disease
swarmed data import --user alice --project <PROJECT_UUID> tmp/heart_disease/local-test/fl-client-*
swarmed data validate  --user alice --project <PROJECT_UUID> --wait
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
swarmed network create --user alice --project <PROJECT_UUID> --name "Heart local" --local-test --clients 4
swarmed network start  --user alice <NETWORK_UUID> --wait
swarmed training start --user alice --network <NETWORK_UUID>
swarmed training watch --user alice <JOB_UUID>
swarmed results sync   --user alice --project <PROJECT_UUID>
swarmed results visualize    --user alice --project <PROJECT_UUID> --job <JOB_UUID> --wait
swarmed results explain      --user alice --project <PROJECT_UUID> --job <JOB_UUID> --wait
swarmed results explanations --user alice --project <PROJECT_UUID> --job <JOB_UUID>
swarmed network stop   --user alice <NETWORK_UUID> --wait
```

Import the four `fl-client-k` folders themselves and do not use `--dest`: each folder is stored
under its own name (`fl-client-1/train.npz`, ...), which is where each client looks. Importing
`local-test/` or adding `--dest` puts a prefix in front of every key
(`local-test/fl-client-1/train.npz`), and training fails because no client finds its folder. A
hospital in a real deployment imports its files instead (`tmp/heart_disease/site-2/*`), which
stores them at the root of its data.

### Web interface

1. Create a project. Under **Training Code**, choose **Select individual files** and select
   `training.py`. Put `requirements.txt`, `validation.py`, `visualization.py`,
   `results_visualization.py` and `explainability.py` into their own fields. **Select project
   folder** drops the folder's own name, so `training.py` lands at the root, but it uploads only
   the `.py` files, all as training code; the other scripts and `requirements.txt` still go into
   their own fields. The Hub refuses training code without a root-level `training.py`.
2. On the **Data** page, import the `fl-client-k` folders one at a time, with the destination
   folder (root); each file is stored below its folder's name, as the clients expect. Then run
   the validation and the visualization.
3. Create a network with **Test in local environment**, start it and start the training. The web
   interface creates two clients, so only `fl-client-1` (Cleveland) and `fl-client-2`
   (Hungarian) train; for all four hospitals, create the network with the CLI.
4. On the **Results** page, select the job, click **Run Visualization**, and click **Run
   Explainability** in the **Explainability** section to see and review the explained cases.

### Score a final model at a site

Download `FL_global_model.pt` from the **Results** page or with `swarmed results export`, save
it as `tmp/heart_disease/FL_global_model.pt`, then, from the repository root:

```bash
python examples/tabular_heart_disease/evaluate.py --model tmp/heart_disease/FL_global_model.pt \
  --data tmp/heart_disease/local-test --out tmp/heart_disease/result.json   # all sites + pooled
python examples/tabular_heart_disease/evaluate.py --model tmp/heart_disease/FL_global_model.pt \
  --data tmp/heart_disease/site-2                                            # one site
```

Use the final `FL_global_model.pt`; `best_FL_global_model.pt` is selected by the validation
accuracy the sites report, not by AUROC.

## What was run

| Where | Date | Sites | Rounds | Result (test split of each site) | Run time | Environment |
|---|---|---|---|---|---|---|
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 4 (Cleveland, Hungarian, VA Long Beach, Switzerland) | 10 x 10 local epochs | see below | 8 s job build + 46 s simulator on 4 clients | Host virtual environment: Python 3.12.13, torch 2.10.0, numpy 2.4.3, scikit-learn 1.8.0, pandas 2.3.3, NVFlare 2.7.1; CPU |
| SwarmMed local-test network on one host, CPU | 11 Nov 2026 | 4 (as above) | 10 x 10 local epochs | pooled AUROC 0.861 (0.807-0.906); the AUROC of every site equals the simulator run's | 11 s network start + 29 s job | Fresh installation of SwarmMed following its README, in an isolated Ubuntu 24.04 container with its own Docker Engine; runtime image built by the Hub for the network (python:3.12-slim with the platform's packages and this template's `requirements.txt`, torch 2.10.0, NVFlare 2.7.1); CPU |

SwarmMed run (11 Nov 2026, one run): the project was created from this folder with `swarmed project
create --code-dir`, the `fl-client-k` folders were imported with `swarmed data import`, and the job
was trained on a local-test network with 4 clients through the CLI steps in [Run it](#run-it). All
sites finished every round and hold identical `FL_global_model.pt` files. In the Hub's sandbox,
`validation.py` (38 checks: 36 ok, 2 warnings), `visualization.py` (4 plots) and
`results_visualization.py` (4 plots) completed, and on 12 Nov 2026 `explainability.py` stored 8
cases with 3 panels each and 2 run-level figures.

Simulator run (one run, seed 42, one data split; no repetition, so there is no estimate of
run-to-run spread). All four sites logged a `SWARMMED_METRIC` line in each of the 10 rounds, and
the `FL_global_model.pt` files of the four sites are identical. The final model scored with
`evaluate.py` (AUROC with a 95% percentile bootstrap interval, 2,000 resamples, seed 0):

| Site | Test patients (disease) | AUROC (95% CI) | AUPRC | Accuracy | Sensitivity | Specificity |
|---|---|---|---|---|---|---|
| fl-client-1 Cleveland | 61 (28) | 0.915 (0.832-0.978) | 0.911 | 0.787 | 0.857 | 0.727 |
| fl-client-2 Hungarian | 58 (21) | 0.897 (0.814-0.962) | 0.855 | 0.793 | 0.810 | 0.784 |
| fl-client-3 VA Long Beach | 40 (30) | 0.770 (0.610-0.907) | 0.927 | 0.700 | 0.767 | 0.500 |
| fl-client-4 Switzerland | 25 (23) | 0.522 (0.000-1.000) | 0.914 | 0.760 | 0.783 | 0.500 |
| pooled | 184 (102) | 0.861 (0.807-0.906) | 0.890 | 0.766 | 0.804 | 0.720 |

Accuracy, sensitivity and specificity use the probability threshold 0.5. The Switzerland test
split holds 2 patients without disease, so its AUROC and interval carry no information. The
pooled AUROC mixes sites with different prevalences; compare the per-site values. The
validation AUROC of the received global model at round 0 (the seeded initial weights) and round 9
was 0.54 and 0.93 (Cleveland), 0.75 and 0.94 (Hungarian), 0.61 and 0.80 (VA Long Beach), 0.65 and
0.67 (Switzerland). The test AUROCs per site and pooled are the same to three decimals as in a
plain-PyTorch simulation of the swarm without NVIDIA FLARE (written to choose the schedule).

The sandbox scripts were first run with a local emulation of the Hub's sandbox wrapper on the same
data and the simulator's results: `validation.py` 38 checks, 36 ok and 2 warnings (Switzerland: 2
negatives in the validation and test splits; cholesterol 100% and fasting blood sugar 61% missing,
both expected), `visualization.py` 4 plots in 1.9 s, and `results_visualization.py` 4 plots in 3.0
s with 0.7 GB peak memory, inside the Hub's 1 GB and 5 minute limits. On 11 Nov 2026,
`validation.py` (the same 38 checks: 36 ok, 2 warnings) and `visualization.py` (4 plots) also ran
in a SwarmMedHub sandbox on the same data. On the SwarmMed run in [What was run](#what-was-run),
`results_visualization.py` and `explainability.py` also ran in the Hub's sandbox.
`explainability.py` is described below.

Not tested: GPU, more than four sites, sites on separate hosts, other random splits on the
platform.

## Explainability

The paragraphs below describe the cases of the recorded run, with their outcomes. If you
want to try the blinded review on that run, do it before you read them.

`explainability.py` explains the final global model on the test split of the first site folder
(or the root data in a real deployment). It selects the two most confident cases of each outcome
(true positive, false positive, false negative, true negative; 8 cases) and draws three panels
for each case. The first, the input panel, is a table of the patient's recorded values next to
the site's training median (continuous variables) or most common value (binary and categorical
variables); it shows nothing from the model. The other two are bar panels in the log-odds of
disease:

- **Feature ablation:** how much the model's output changes when one variable is replaced by the
  site's mean training case (z-scored variables 0, binary variables their prevalence).
- **Integrated gradients:** the gradient of the output integrated along a straight path from
  that mean case to the patient (64 steps; the contributions add up to the output difference
  from the mean case, and the completeness gap is stored with the case).

The 15 model inputs are grouped into the 10 clinical variables (the one-hot columns of chest pain
type and resting ECG are one group each), so both methods give one bar per variable and each
bar carries the patient's value (for example "ST depression = 4.2"). Red bars push the prediction
towards disease, blue bars against it. Each case stores the rank agreement of the two methods
(Spearman correlation of the absolute values and the overlap of the three largest). Two run-level
figures follow: permutation importance (drop of the site's test AUROC when one variable is
shuffled across test patients, 30 repeats, mean and standard deviation) and the mean absolute
feature-ablation effect over all test patients.

Run it locally from the repository root, with the paths of [Run it](#run-it) (the same way
the sandbox runs it; the bundle can then be checked or imported):

```bash
python -m swarmmed_explain.run_local --script examples/tabular_heart_disease/explainability.py \
  --data tmp/heart_disease/local-test --model tmp/heart_disease/FL_global_model.pt \
  --out tmp/heart_disease/explain_bundle
```

On the simulator's final model (11 Nov 2026), the run took 2.5 s pinned to 2 CPUs with 0.73 GB
peak memory, inside the sandbox limits of 2 CPUs, 2 GB and 15 minutes, and wrote a valid bundle
of 8 cases, 24 panels and 2 summaries. On the Cleveland test split the
largest effects were ST depression, chest pain type and exercise-induced angina by both case
maps and the permutation importance (drop in AUROC about 0.12, 0.06 and 0.04 for chest pain,
ST depression and angina). The two case-level methods agreed closely: the rank correlation of their
absolute values was 0.75 to 1.00 over the 8 cases, and their three largest variables were the same
in 7 of the 8 cases (2 of 3 in the other).
Resting ECG, age and blood pressure contribute little in this model. These are properties of one
small model; they are not clinical findings.

Maps support review. They show what this model does with its inputs; they are not causal, they
depend on the reference case, correlated variables share credit, and a plausible-looking map does
not justify a prediction or show that the model is correct. In SwarmMedHub a reviewer can agree,
disagree or note each case. In blinded mode, a case the reviewer has not read yet shows only its
case ID, its label and the input panel, and the Hub shows the cases in a random order, so a
case's position says nothing about its outcome.

## Limitations

- Small data, small test splits: 25 to 61 test patients per site and 2 negatives at
  Switzerland. Intervals are wide; the per-site AUROC at VA Long Beach and Switzerland
  cannot be compared with Cleveland or Hungary.
- Sites differ in prevalence, recorded variables and population (Switzerland records no
  cholesterol, Long Beach is almost all male). Part of the differences between sites reflects data collection, not hospital quality.
- One split (seed 42) and one training run per environment; no repetition on the platform.
- The data were collected in the 1980s; this is not a model for clinical use and the labels
  come from angiography at four hospitals.
- Missing values are imputed with training statistics and without missingness indicators, so
  the model cannot tell an imputed value from a measured one; the explanation panels show
  "missing" for such values and the maps for them describe the imputed value.
- Explanation maps are not causal and are checked here only for consistency across methods,
  not for clinical validity.
- Not a federated-privacy claim: swarm learning keeps records at the sites, but the shared
  model weights can leak information about training data; no privacy analysis was done.
