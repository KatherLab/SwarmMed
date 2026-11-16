# EHR template: in-hospital mortality from the first 24 hours of an ICU stay (eICU)

An example for SwarmMed with electronic health record data. Three groups of US hospitals (the
Midwest, South and West census regions of the eICU Collaborative Research Database) each act as one
site, and a multilayer perceptron predicts from 41 values charted in the first 24 hours of an
adult's first ICU stay whether the patient dies before hospital discharge. The data are small (994
patients in all, about 80 kilobytes per site), so a complete swarm run takes about a minute of
computation. The template is meant to show that training, data checks, result plots and
explanations run on SwarmMed with natural, hospital-group-level site splits. Its training has run
to completion on a SwarmMed local-test network (all sites on one host) and in NVIDIA FLARE's
simulator, and its data checks, data plots, result plots and explanations have run in a SwarmMedHub
sandbox (see [What was run](#what-was-run)). It does not make a performance claim.

## Task

Binary classification: died before hospital discharge (label 1) or discharged alive (label 0),
predicted at 24 hours after ICU admission from the data of those 24 hours. Cohort (adult first ICU
stays of at least 24 hours with a known discharge status): 994 patients at the three sites, 9% of
them died. Each site is one US census region, as recorded for the patient's hospital. Sites differ in
case mix, in how often variables such as lactate and the Glasgow Coma Scale were charted, and in
the number of hospitals behind them (62, 54 and 39).

## Data

- Dataset: eICU Collaborative Research Database Demo, version 2.0.1 (a public subset of 2,520 ICU
  stays at 186 hospitals of the eICU Collaborative Research Database).
- Authors: Johnson A, Pollard T, Badawi O, Raffa J (Philips eICU Research Institute, MIT
  Laboratory for Computational Physiology).
- Citations:
  - Johnson A, Pollard T, Badawi O, Raffa J (2021). eICU Collaborative Research Database Demo
    (version 2.0.1). PhysioNet. RRID:SCR_007345. https://doi.org/10.13026/4mxk-na84
  - Pollard TJ, Johnson AEW, Raffa JD, Celi LA, Mark RG, Badawi O. The eICU Collaborative
    Research Database, a freely available multi-center database for critical care research.
    Scientific Data. 2018;5:180178. https://doi.org/10.1038/sdata.2018.178
  - Pollard T, Moody BE, Lehman L, Gow B, Fernandes C, Xie C, Johnson A, Mark RG, Heldt T.
    PhysioNet as a global platform for biomedical research. Nature Health. 2026;1(8):792-795.
    https://doi.org/10.1038/s44360-026-00096-z
- Licence: Open Data Commons Open Database License v1.0 (ODbL), https://physionet.org/content/eicu-crd-demo/view-license/2.0.1/.
  The demo is open access (no credentialing). **The ODbL is a share-alike licence: the prepared
  files are a derived database. Do not publish them (or the explanation panels, which show
  patients' values) except under the ODbL with the attribution above, and never commit them to a
  repository.** `prepare_data.py` warns when its output folder lies inside a git work tree.
- Page: https://physionet.org/content/eicu-crd-demo/2.0.1/
- Download (done by `prepare_data.py --download`): https://physionet.org/files/eicu-crd-demo/2.0.1/
  (the seven tables used, 44.9 MB). `prepare_data.py` checks each table against the SHA-256
  values PhysioNet publishes and stops if one differs.

| Site folder | Region | Hospitals | Patients | Deaths | Train / val / test | Deaths in train / val / test |
|---|---|---|---|---|---|---|
| `fl-client-1` | Midwest | 62 | 376 | 29 | 226 / 75 / 75 | 17 / 6 / 6 |
| `fl-client-2` | South | 54 | 364 | 33 | 218 / 73 / 73 | 19 / 7 / 7 |
| `fl-client-3` | West | 39 | 254 | 25 | 152 / 51 / 51 | 15 / 5 / 5 |

What `prepare_data.py` does (numpy, pandas, scipy and the standard library only):

1. reads the tables `patient`, `hospital`, `vitalPeriodic`, `vitalAperiodic`, `nurseCharting`,
   `lab` and `apachePatientResult` and checks their SHA-256;
2. selects adult (age 18 or more; "> 89" is coded as 90) first ICU stays of the hospital stay, one
   stay per patient, with a known discharge status and an ICU stay of at least 24 hours: 2,520
   stays become 1,187 (the steps are listed in each `site_info.json`);
3. assigns each stay to the region of its hospital and keeps Midwest, South and West (994 patients).
   Stays in other or unrecorded regions are left out; the Northeast, with 83 patients, can be
   added as a fourth site with `--regions Midwest,South,West,Northeast` (not used here);
4. builds 41 features from the first 24 hours: age, height, weight, hospital days before the ICU
   (4); minimum, maximum and mean values of heart rate, respiratory rate, SpO2, systolic and mean
   arterial pressure and temperature (not every statistic of each) and the lowest Glasgow Coma
   Scale (15 columns); the extreme values of 12 laboratory tests from 6 hours before to 24
   hours after admission (creatinine, urea nitrogen, sodium, potassium, glucose, bicarbonate,
   white cells, haemoglobin, platelets, lactate, bilirubin, albumin; 13 columns); sex; ICU type (4 categories, one-hot) and admission source (4
   categories, one-hot); values outside a physiological range are dropped before the statistics;
5. splits each site's patients 60/20/20 into train, validation and test, stratified by outcome,
   with seed 42 (one ICU stay per patient, so no patient is in two splits);
6. fills missing values from the site's own training split (median for continuous values, majority
   value for sex), z-scores the continuous features and clips them to [-5, 5]; no statistic is
   pooled across sites; no missingness indicator is added;
7. writes `train.npz`, `val.npz`, `test.npz` and `site_info.json` per site, and checks that the
   cohort counts, the per-site counts and a fingerprint of the test cases match the values for
   seed 42.

`X_raw` in each file keeps the values before imputation (NaN = missing) for the data checks and
the explanation panels. `ref_apache_iva` holds the APACHE IVa predicted hospital mortality that
the database stores for the stay (missing for some stays). It is a reference score for
`evaluate.py`, never a model input, and it uses more information than this model (including the
admission diagnosis), so it is not a like-for-like comparison.

Lactate is missing for 58% to 70% of the patients and the Glasgow Coma Scale for 30% to 62%
(charted when clinically indicated), so the imputed values carry no information for them and
the missingness itself, which the model cannot see, is informative.

## Data layout

```text
fl-client-1/   train.npz  val.npz  test.npz  site_info.json     (Midwest)
fl-client-2/   ...                                              (South)
fl-client-3/   ...                                              (West)
```

Arrays in each `.npz`: `X` float32 `[n, 41]` (model input), `X_raw` the same columns before
imputation and scaling, `y` int64, `case_id` strings (the database's pseudonymous stay id),
`feature_names`, `ref_apache_iva`.

- **Local-test network on one host:** import the `fl-client-k` folders themselves (see
  [Run it](#run-it)), so that every key starts with `fl-client-k/`. Each client reads only the
  folder named after its FLARE site name.
- **Real deployment:** each site runs `prepare_data.py --layout single --site-index K` and
  registers only its own four files, at the root of its data. The client then finds `train.npz`
  and `val.npz` at the root.

## Model

`MortalityMLP`: 41 inputs, one hidden layer of 32 units (ReLU, dropout 0.2), one output logit (the
log-odds of death); 1,377 parameters, no batch normalisation (its running statistics would be
averaged across sites with different distributions). Each round a site scores the received global
model on its validation split, trains it for 5 local epochs (Adam, learning rate 1e-3, weight
decay 1e-4, batch size 32, new optimiser each round) and returns the weights with its number of
training cases as the aggregation weight and its validation accuracy as the model-selection
metric. 20 swarm rounds (`SWARM_ROUNDS` in `training.py`). With 150 to 230 training patients per
site the model needs many small steps: in a plain-PyTorch emulation of the three sites, 20 rounds
of 5 epochs reached a validation AUROC of 0.90 to 0.92 at all three sites, and the simulator run below
agrees with it. On a local-test network the rounds, not the computation, set
the run time (about 43 seconds of platform overhead per round on the Hub host, so allow roughly 15
minutes for 20 rounds).

The test split is used only by `evaluate.py`, `results_visualization.py` and
`explainability.py`, never during training. Every site prints one line per round:

```text
SWARMMED_METRIC site=fl-client-1 round=19 split=val auroc=0.9179 auprc=0.6658 accuracy=0.9467 loss=0.1574 n=75 n_pos=6
```

The site also writes the per-round validation metrics to `val_metrics.json` in its job folder,
which the Hub uploads with the results, so `results_visualization.py` can plot the rounds even
when the client log is not among the result files.

## Files

| File | Role |
|---|---|
| `training.py` | Client-API training loop (needs only numpy, scikit-learn, torch) |
| `requirements.txt` | Pinned runtime packages |
| `validation.py` | 32 data checks for three sites in the Hub sandbox (shapes, labels, splits, duplicates, missing values, value ranges, same features at all sites) |
| `visualization.py` | 4 data plots: patients per site and outcome, missing values, eight clinical variables by site and outcome, case mix |
| `results_visualization.py` | 4 result plots: validation AUROC per round, ROC curves with the pooled curve, test AUROC with bootstrap 95% CI per site and pooled, predicted probabilities |
| `explainability.py` | Per-case explanations (see Explainability) |
| `prepare_data.py` | Converts the eICU Demo tables into the layout above; run by a site before import; uploaded with `--code-dir` but not used by the platform |
| `evaluate.py` | Scores a saved global model on a site's test split (per site and pooled) |

`prepare_data.py` and `evaluate.py` are harmless in the code bundle but are not needed there.
Keep the prepared data outside this folder: every file in it is uploaded as training code.

## Run it

Prepare the data once, on the machine that holds the files. Run the commands from the
repository root: the CLI runs in the application container and reads only files inside the
repository, whose `tmp/` folder git ignores. The prepared files fall under the ODbL: keep them
in a git-ignored folder and never commit or publish them (`prepare_data.py` warns when its
output lies inside a git work tree). `--download` fetches the tables; leave it out if they are
already in `--raw-dir`.

```bash
python examples/ehr_eicu_mortality/prepare_data.py --download \
  --raw-dir tmp/eicu_demo/raw --out tmp/eicu_demo/local-test
# one site only, for a real deployment (K = 1, 2 or 3):
python examples/ehr_eicu_mortality/prepare_data.py --raw-dir tmp/eicu_demo/raw \
  --out tmp/eicu_demo/site-2 --layout single --site-index 2
```

### Command line

Use your own user name and replace the UUIDs ([CLI](../../docs/cli.md)).

```bash
swarmed project create --user alice --title "eICU mortality" \
  --description "EHR template" --code-dir examples/ehr_eicu_mortality
swarmed data import --user alice --project <PROJECT_UUID> tmp/eicu_demo/local-test/fl-client-*
swarmed data validate  --user alice --project <PROJECT_UUID> --wait
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
swarmed network create --user alice --project <PROJECT_UUID> --name "eICU local" --local-test --clients 3
swarmed network start  --user alice <NETWORK_UUID> --wait
swarmed training start --user alice --network <NETWORK_UUID>
swarmed training watch --user alice <JOB_UUID>
swarmed results sync   --user alice --project <PROJECT_UUID>
swarmed results visualize    --user alice --project <PROJECT_UUID> --job <JOB_UUID> --wait
swarmed results explain      --user alice --project <PROJECT_UUID> --job <JOB_UUID> --wait
swarmed results explanations --user alice --project <PROJECT_UUID> --job <JOB_UUID>
swarmed network stop   --user alice <NETWORK_UUID> --wait
```

Import the three `fl-client-k` folders themselves and do not use `--dest`: each folder is
stored under its own name (`fl-client-1/train.npz`, ...), which is where each client looks.
Importing `local-test/` or adding `--dest` puts a prefix in front of every key
(`local-test/fl-client-1/train.npz`), and training fails because no client finds its folder. A
site in a real deployment imports its files instead (`tmp/eicu_demo/site-2/*`), which stores
them at the root of its data.

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
   interface creates two clients, so only `fl-client-1` (Midwest) and `fl-client-2` (South)
   train; for all three regions, create the network with the CLI.
4. On the **Results** page, select the job, click **Run Visualization**, and click **Run
   Explainability** in the **Explainability** section to see and review the explained cases.

### Score a final model at a site

Download `FL_global_model.pt` from the **Results** page or with `swarmed results export`, save
it as `tmp/eicu_demo/FL_global_model.pt`, then, from the repository root:

```bash
python examples/ehr_eicu_mortality/evaluate.py --model tmp/eicu_demo/FL_global_model.pt \
  --data tmp/eicu_demo/local-test --out tmp/eicu_demo/result.json   # all sites + pooled
python examples/ehr_eicu_mortality/evaluate.py --model tmp/eicu_demo/FL_global_model.pt \
  --data tmp/eicu_demo/site-2                                        # one site
```

Use the final `FL_global_model.pt`; `best_FL_global_model.pt` is selected by the validation
accuracy the sites report, and at a 9% death rate accuracy is about 0.9 whatever the ranking
quality.

## What was run

| Where | Date | Sites | Rounds | Result (test split of each site) | Run time | Environment |
|---|---|---|---|---|---|---|
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 3 (Midwest, South, West) | 20 x 5 local epochs | see below | 12 s job build + 64 s simulator on 3 clients (third attempt, see below) | Host virtual environment: Python 3.12.13, torch 2.10.0, numpy 2.4.3, scikit-learn 1.8.0, pandas 2.3.3, NVFlare 2.7.1; CPU |
| SwarmMed local-test network on one host, CPU | 12 Nov 2026 | 3 (as above) | 20 x 5 local epochs | pooled AUROC 0.815 (0.681-0.930); the AUROC of every site equals the simulator run's | 11 s network start + 44 s job (second attempt: the first was stopped by the NVIDIA FLARE fault described below) | Fresh installation of SwarmMed following its README, in an isolated Ubuntu 24.04 container with its own Docker Engine; runtime image built by the Hub for the network (python:3.12-slim with the platform's packages and this template's `requirements.txt`, torch 2.10.0, NVFlare 2.7.1); CPU |

SwarmMed run (12 Nov 2026, one run): the project was created from this folder with `swarmed project
create --code-dir`, the `fl-client-k` folders were imported with `swarmed data import`, and the job
was trained on a local-test network with 3 clients through the CLI steps in [Run it](#run-it). All
sites finished every round and hold identical `FL_global_model.pt` files. In the Hub's sandbox,
`validation.py` (32 checks: 26 ok, 6 warnings), `visualization.py` (4 plots) and
`results_visualization.py` (4 plots) completed, and on 12 Nov 2026 `explainability.py` stored 8
cases with 3 panels each and 2 run-level figures.

Simulator run (seed 42, one data split; no repetition, so there is no estimate of run-to-run
spread). The first two attempts were stopped by a known NVIDIA FLARE 2.7.1 fault in the swarm
workflow ("already running a task: aborting it": the next round's request reaches a site while its
previous task is still finishing, and that site stops training), after 50 s and 26 s; the harness
restarted the run from round 0, and the third attempt completed. This is not a fault of the
template, but it is likely to recur in runs with many short rounds. In the completed run all three
sites logged a `SWARMMED_METRIC` line in each of the 20 rounds, and the `FL_global_model.pt` files
of the three sites are identical. The final model scored with `evaluate.py` (AUROC with a 95%
percentile bootstrap interval, 2,000 resamples, seed 42):

| Site | Test patients (deaths) | AUROC (95% CI) | AUPRC | Accuracy | Sensitivity | Specificity | APACHE IVa AUROC (n) |
|---|---|---|---|---|---|---|---|
| fl-client-1 Midwest | 75 (6) | 0.802 (0.531-0.991) | 0.533 | 0.920 | 0.333 | 0.971 | 0.847 (68) |
| fl-client-2 South | 73 (7) | 0.937 (0.842-0.996) | 0.713 | 0.945 | 0.429 | 1.000 | 0.872 (62) |
| fl-client-3 West | 51 (5) | 0.661 (0.273-0.965) | 0.308 | 0.882 | 0.200 | 0.957 | 0.700 (39) |
| pooled | 199 (18) | 0.815 (0.681-0.930) | 0.498 | 0.920 | 0.333 | 0.978 | 0.814 (169) |

Accuracy, sensitivity and specificity use the probability threshold 0.5. With 5 to 7 deaths per
test split the per-site AUROCs differ by amounts that the intervals do not separate; the pooled
value is the more stable figure. The APACHE IVa column is the severity score stored in the
database, on the stays that have one; it uses more variables, including the admission diagnosis.
The validation AUROC of the received global model at round 0 (the seeded initial weights) and round
19 was 0.36 and 0.92 (Midwest), 0.59 and 0.92 (South), 0.22 and 0.89 (West).

The sandbox scripts were first run with a local emulation of the Hub's sandbox wrapper on the same
data and the simulator's results: `validation.py` 32 checks, 26 ok and 6 warnings (each site: 5 to
7 deaths in the validation and test splits; lactate missing for 58% to 70% and, at West, the
Glasgow Coma Scale for 62% of the patients), `visualization.py` 4 plots in 2.6 s, and
`results_visualization.py` 4 plots in 2.2 s with 0.70 GB peak memory, inside the Hub's 1 GB and 5
minute limits. On 11 Nov 2026, `validation.py` (the same 32 checks: 26 ok, 6 warnings) and
`visualization.py` (4 plots) also ran in a SwarmMedHub sandbox on the same data. On the SwarmMed
run in [What was run](#what-was-run), `results_visualization.py` and `explainability.py` also ran
in the Hub's sandbox. `explainability.py` is described below.

Not tested: GPU, more than three sites, the Northeast as a fourth site on the platform, sites on
separate hosts, other random splits on the platform.

## Explainability

The paragraphs below describe the cases of the recorded run, with their outcomes. If you
want to try the blinded review on that run, do it before you read them.

`explainability.py` explains the final global model on the test split of the first site folder
(or the root data in a real deployment). It scores the test patients, selects the two most
confident cases of each outcome (true positive, false positive, false negative, true negative; 8
cases; "positive" means a predicted probability of death of at least 0.2, because only about 9%
of the patients die and the model rarely exceeds 0.5) and draws three panels for each case. The
first, the input panel, is a table of the values recorded for the patient in the first 24 hours
next to the site's training median (or most common value for sex, ICU type and admission source);
it shows nothing from the model. The other two are bar panels in the log-odds of death. Both
compare the patient with a reference patient, the median of the site's training split for every
input:

- **Feature ablation:** how much the model's output changes when one feature is replaced by the
  reference value.
- **Integrated gradients:** the gradient of the output integrated along a straight path from the
  reference patient to the patient (64 steps; the contributions add up to the output difference,
  and the completeness gap is stored with the case).

The one-hot columns of ICU type and admission source are one item each (setting one column of a
one-hot code alone would describe a patient without an ICU type), so the 41 inputs give 35
items. A bar panel shows the 15 items with the largest effect, each labelled with the value
recorded for the patient ("missing" when the model saw an imputed value). Red bars push the
prediction towards death, blue bars towards survival. Each case stores the rank agreement of the
two methods (Spearman correlation of the absolute values over the 35 items and the overlap of the
five largest). Two run-level figures follow: permutation importance (drop of the site's test AUROC
when one item is shuffled across the test patients, 30 repeats, mean and standard deviation) and
the agreement of the two methods per selected patient.

Run it locally from the repository root, with the paths of [Run it](#run-it) (the same way
the sandbox runs it; the bundle can then be checked or imported):

```bash
python -m swarmmed_explain.run_local --script examples/ehr_eicu_mortality/explainability.py \
  --data tmp/eicu_demo/local-test --model tmp/eicu_demo/FL_global_model.pt \
  --out tmp/eicu_demo/explain_bundle
```

On the simulator's final model (11 Nov 2026), the run took 3.3 s pinned to 2 CPUs with 0.74 GB
peak memory, inside the sandbox limits of 2 CPUs, 2 GB and 15 minutes, and wrote a valid bundle
of 8 cases, 24 panels and 2 summaries. On the Midwest
test split (6 deaths) the permutation importance was largest for the lowest Glasgow Coma Scale
(AUROC drop about 0.15), the highest lactate (about 0.11) and age (about 0.05); in the first
true positive both case-level methods put a very low Glasgow Coma Scale and a high lactate
far ahead of everything else. The two methods agreed on the most important items: the rank correlation of their absolute
values was 0.60 to 1.00 over the 8 cases (below 0.9 in the two true and two false positives), and
their five largest items overlapped in 3 to 5 items (all 5 for 3 cases). In the first false
negative (scored 0.005) the integrated-gradients effects were spread over many items with no
single dominant one. These are properties of one small model on one
split; they are not clinical findings.

Maps support review. They show what this model does with its inputs; they are not causal, they
depend on the reference patient, correlated features (the minimum, maximum and mean of one vital
sign) share credit, and a plausible-looking map does not justify a prediction or show that the
model is correct. In SwarmMedHub a reviewer can agree, disagree or note each case. In blinded
mode, a case the reviewer has not read yet shows only its case ID, its label and the input panel,
and the Hub shows the cases in a random order, so a case's position says nothing about its
outcome. The panels show patient-level values: treat them under the data licence (ODbL) and your
site's rules.

## Limitations

- Small data and very small test splits: 51 to 75 test patients and 5 to 7 deaths per site. The
  per-site AUROC intervals are 0.15 to 0.69 wide and overlap; the sites cannot be ranked.
- The sites are groups of hospitals by census region, not single hospitals, and the demo draws
  at most a few dozen stays from each hospital; differences between sites reflect charting
  practice and case mix as much as population.
- One split (seed 42) and one training run per environment; no repetition on the platform.
- The model is a small MLP on summary statistics of 24 hours; it sees no diagnosis, medication
  or trend. It is not calibrated and the 0.5 threshold gives low sensitivity at a 9% death rate.
  It is not a model for clinical use.
- Missing values are imputed with training medians and without missingness indicators, so the
  model cannot tell an imputed value from a measured one, although whether a lactate was
  ordered is itself informative. The explanation panels show "missing" for such values and the
  maps describe the imputed value.
- Deaths are in-hospital deaths: patients transferred or discharged to hospice are labelled by
  the discharge status in the database.
- Explanation maps are not causal and are checked here only for consistency across methods,
  not for clinical validity.
- The best-model file chosen by the platform (`best_FL_global_model.pt`) uses validation accuracy
  and is not recommended here; use the final model.
- Not a federated-privacy claim: swarm learning keeps records at the sites, but the shared
  model weights can leak information about training data; no privacy analysis was done.
