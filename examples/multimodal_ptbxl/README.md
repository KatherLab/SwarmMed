# Multimodal template: ECG plus patient metadata from PTB-XL

A multimodal template for SwarmMed. Four recording sites each hold their own 12-lead ECGs from the
public PTB-XL dataset together with the patient's age, sex, height and weight, and a small network
with two inputs (a 1D convolutional branch for the ECG and a linear branch for the metadata) learns
to tell normal from abnormal records. It is the [`ecg_ptbxl`](../ecg_ptbxl/README.md) template with
a second input, on the same data split, so the two can be compared. The data are about 320 MB in
all, and a swarm run of five rounds finishes in minutes on a CPU. The template is meant to show
that two kinds of input train together on SwarmMed, and that data checks, result plots and per-case
explanations (with a separate map for the metadata) run on it. Its training has run to completion
on a SwarmMed local-test network (all sites on one host) and in NVIDIA FLARE's simulator, and its
data checks, data plots, result plots and explanations have run in a SwarmMedHub sandbox (see [What
was run](#what-was-run)). It does not make a performance claim: the metadata add very little here,
and their missingness mostly encodes the recording site.

## Task

Binary classification of a 10-second, 12-lead ECG at 100 Hz: **normal** (label 0: the record's
only diagnostic superclass is NORM) or **abnormal** (label 1: any of MI myocardial infarction,
STTC ST/T change, CD conduction disturbance, HYP hypertrophy). The five superclasses are kept in
the data files (`y_super`) for the explanation and result plots; training uses the binary label.
Records without any diagnostic superclass are dropped (411 of 21,799). Inputs: the ECG and a
vector of 6 metadata values per record: `age_z` = (age in years - 60) / 20 (PTB-XL stores ages of
90 and above as 300; they are mapped to 90 first), `sex_female` (0 or 1), `height_z` = (height in
cm - 170) / 10, `height_missing`, `weight_z` = (weight in kg - 75) / 15, `weight_missing`.
A missing or implausible height or weight (outside 120 to 220 cm and 30 to 250 kg for adults, 50 to
200 cm and 3 to 150 kg under 18; 12 heights and 3 weights in all) is set to 0 with its missing
indicator 1. The constants are fixed, not computed from the data.

## Data

- Dataset: PTB-XL, a large publicly available electrocardiography dataset, version 1.0.3
  (21,799 clinical ECGs from 18,869 patients, recorded with Schiller devices from 1989 to 1996).
- Citation: Wagner P, Strodthoff N, Bousseljot R, Samek W, Schaeffter T (2022). PTB-XL, a large
  publicly available electrocardiography dataset (version 1.0.3). PhysioNet.
  https://doi.org/10.13026/kfzx-aw45. Paper: Wagner P et al., Scientific Data 2020;7:154,
  https://doi.org/10.1038/s41597-020-0495-6. Also cite PhysioNet: Pollard T, Moody BE, Lehman L,
  Gow B, Fernandes C, Xie C, Johnson A, Mark RG, Heldt T. PhysioNet as a global platform for
  biomedical research. Nature Health. 2026;1(8):792-795.
  https://doi.org/10.1038/s44360-026-00096-z
- Licence: Creative Commons Attribution 4.0 International (CC BY 4.0). Keep the attribution
  above when you redistribute the data or files derived from it.
- Page: https://physionet.org/content/ptb-xl/1.0.3/ (no login needed). The full release is 1.8 GB
  (zip); this template needs the 100 Hz records only.
- Download: `prepare_data.py --download` fetches `ptbxl_database.csv`, `scp_statements.csv`,
  `LICENSE.txt`, `SHA256SUMS.txt` and the `records100/` folder (about 381 MB, using HTTP range
  requests into the official zip) into `--raw-dir`, and checks every file against PhysioNet's
  SHA-256 sums. Alternatively download the zip yourself and pass its extracted folder.

Sites follow PTB-XL's pseudonymised recording-site field. Inside each site, PTB-XL's
patient-wise folds 1 to 8 are the training split, fold 9 the validation split and fold 10 the test
split, so no patient appears in two splits.

| Site folder | PTB-XL recording site | Train (abnormal) | Validation (abnormal) | Test (abnormal) |
|---|---|---|---|---|
| `fl-client-1` | 0 | 7,691 (4,453) | 497 (175) | 483 (173) |
| `fl-client-2` | 1 | 5,038 (2,848) | 606 (351) | 598 (337) |
| `fl-client-3` | 2 | 3,318 (1,925) | 834 (548) | 867 (584) |
| `fl-client-4` | every other site, and records without a site | 1,037 (615) | 209 (158) | 210 (152) |

What `prepare_data.py` does (numpy, pandas and the standard library only; 20 s and about 0.5 GB of
memory on the host used for this README):

1. reads `ptbxl_database.csv`, `scp_statements.csv` and the 100 Hz WFDB records, and checks each
   record's 16-bit checksums;
2. derives the diagnostic superclasses of each record from its SCP statements (likelihoods are
   ignored), drops records without one, and sets the binary label;
3. assigns records to sites and splits as above;
4. converts age, sex, height and weight into the metadata vector above;
5. writes `train.npz`, `val.npz`, `test.npz` and `site_info.json` per site, reads them back, and
   compares the counts, the missingness shares and SHA-256 fingerprints of the arrays with the
   values of the reference preparation (`--skip-verify` turns this off).

No filtering or resampling is applied to the signals. `training.py` converts each record to
millivolts and subtracts each lead's median when it loads the data; nothing is computed across
records or sites. The ECG arrays are identical to those of `ecg_ptbxl`, so one prepared folder
serves both templates.

Height is missing for 99.8% of the records at fl-client-1 and about 98% at fl-client-3, but for
about 5% at fl-client-2; weight is missing for 76% (fl-client-1 training split), 98%
(fl-client-3) and 8% (fl-client-2). The missing-value indicators therefore mostly identify the
site, not the patient.

## Data layout

```text
fl-client-1/   train.npz  val.npz  test.npz  site_info.json     (PTB-XL recording site 0)
fl-client-2/   ...                                              (site 1)
fl-client-3/   ...                                              (site 2)
fl-client-4/   ...                                              (all other sites)
```

Arrays in each `.npz`, sorted by `ecg_id`: `X` int16 `[n, 12, 1000]` (microvolts; leads I, II, III,
aVR, aVL, aVF, V1 to V6), `y` int8 `[n]` (0 normal, 1 abnormal), `y_super` int8 `[n, 5]` (NORM, MI,
STTC, CD, HYP), `ecg_id` int32 `[n]` (PTB-XL's own pseudonymous record number, used as the case
ID in explanations), and the metadata: `tab` float32 `[n, 6]` (the model input above), `age`
float32 (years), `sex` int8 (0 male, 1 female), `height` float32 (cm, NaN when missing or
implausible), `weight` float32 (kg, NaN likewise). `site_info.json` holds the source, licence, counts, label map and
preprocessing.

- **Local-test network on one host:** import the `fl-client-k` folders themselves (see
  [Run it](#run-it)), so that every key starts with `fl-client-k/`. Each client reads only the
  folder named after its FLARE site name.
- **Real deployment:** each site runs `prepare_data.py --layout single --site-index K` and
  registers only its own files, at the root of its data. The client then finds `train.npz` and
  `val.npz` at the root. Files prepared for `ecg_ptbxl` have no metadata arrays; `training.py`
  stops with a message that names the template's `prepare_data.py`.

## Model

`FusionNet`: the ECG branch is the `ecg_ptbxl` network without its output layer (four blocks of
Conv1d with kernels 7, 7, 5, 5 and 32, 64, 64, 128 channels, no bias, BatchNorm, ReLU, max pooling
by 2, global average pooling; 128 features). The metadata branch is Linear(6, 16) and ReLU. The
two are concatenated (144 features) and a linear layer gives one output logit (the log-odds of
"abnormal"); 79,297 parameters. Each round a site scores the received global model on its
validation split, trains it for 1 local epoch (Adam, learning rate 1e-3, batch size 128, a new
optimiser each round) and returns the weights with its number of training records as the
aggregation weight. 5 swarm rounds (`SWARM_ROUNDS` in `training.py`). The schedule comes from a
plain-PyTorch check before the platform runs, in which the binary task is learned within one epoch
(`ecg_ptbxl` README). In that check, centrally trained for 3 epochs, the ECG alone reached a
pooled test AUROC of 0.922 and 0.921 (two seeds) and the fusion with the 6 metadata values 0.930
and 0.927; fusion with age and sex only gave 0.926.

The test split is used only by `evaluate.py`, `results_visualization.py` and `explainability.py`,
never during training. Every site prints one line per round:

```text
SWARMMED_METRIC site=fl-client-1 round=4 split=val val_auroc=0.9261 val_acc=0.8410 val_loss=0.3404 n=497 model=global
```

Round 0 scores the untrained, seeded initial weights (an AUROC below 0.5 there is noise from the
random weights, not a result); later rounds score the aggregated model of the previous round.

## Files

| File | Role |
|---|---|
| `training.py` | Client-API training loop (needs only numpy, scikit-learn, torch) |
| `requirements.txt` | Pinned runtime packages |
| `validation.py` | Data checks in the Hub sandbox (files, shapes, labels, `y` against `y_super`, the metadata vector against age, sex, height and weight, metadata missingness per site, unique record numbers, no record in two splits, flat leads, class counts, `site_info.json` against the arrays) |
| `visualization.py` | 4 data plots: records per site and split by label, superclass shares per site, one normal and one abnormal record, metadata per site (age by label, share of women, missing height and weight) |
| `results_visualization.py` | 4 result plots: validation AUROC per round, ROC curves, test AUROC with bootstrap 95% CI per site and pooled, test AUROC with the real and with shuffled metadata |
| `explainability.py` | Per-case explanations (see Explainability) |
| `prepare_data.py` | Converts PTB-XL into the layout above; run by a site before import; uploaded with `--code-dir` but not used by the platform |
| `evaluate.py` | Scores a saved global model on a site's test split (per site and pooled) |

`prepare_data.py` and `evaluate.py` are harmless in the code bundle but are not needed there.
Keep the prepared data outside this folder: every file in it is uploaded as training code.

## Run it

Prepare the data once, on the machine that holds the files. Run the commands from the
repository root: the CLI runs in the application container and reads only files inside the
repository, whose `tmp/` folder git ignores. `--download` fetches the files listed under Data;
leave it out if they are already in `--raw-dir`.

```bash
python examples/multimodal_ptbxl/prepare_data.py --download \
  --raw-dir tmp/ptbxl_multimodal/raw --out tmp/ptbxl_multimodal/local-test
# one site only, for a real deployment:
python examples/multimodal_ptbxl/prepare_data.py --raw-dir tmp/ptbxl_multimodal/raw \
  --out tmp/ptbxl_multimodal/site-2 --layout single --site-index 2
```

### Command line

Use your own user name and replace the UUIDs ([CLI](../../docs/cli.md)).

```bash
swarmed project create --user alice --title "PTB-XL ECG and metadata" \
  --description "Multimodal template" --code-dir examples/multimodal_ptbxl
swarmed data import --user alice --project <PROJECT_UUID> tmp/ptbxl_multimodal/local-test/fl-client-*
swarmed data validate  --user alice --project <PROJECT_UUID> --wait
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
swarmed network create --user alice --project <PROJECT_UUID> --name "Multimodal local" --local-test --clients 4
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
site in a real deployment imports its files instead (`tmp/ptbxl_multimodal/site-2/*`), which
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
   interface creates two clients, so only `fl-client-1` and `fl-client-2` (recording sites 0 and
   1) train; for all four sites, create the network with the CLI.
4. On the **Results** page, select the job, click **Run Visualization**, and click **Run
   Explainability** in the **Explainability** section to see and review the explained cases.

### Score a final model at a site

Download `FL_global_model.pt` from the **Results** page or with `swarmed results export`, save
it as `tmp/ptbxl_multimodal/FL_global_model.pt`, then, from the repository root:

```bash
python examples/multimodal_ptbxl/evaluate.py --checkpoint tmp/ptbxl_multimodal/FL_global_model.pt \
  --data tmp/ptbxl_multimodal/local-test --out tmp/ptbxl_multimodal/result.json   # all + pooled
python examples/multimodal_ptbxl/evaluate.py --checkpoint tmp/ptbxl_multimodal/FL_global_model.pt \
  --data tmp/ptbxl_multimodal/site-2                                              # one site
```

Use the final `FL_global_model.pt`; `best_FL_global_model.pt` is selected by the validation
accuracy the sites report, not by AUROC. `evaluate.py` imports the model and the normalisation
from `training.py` next to it. It also reports the AUROC with the metadata rows shuffled between the
records of each site (5 shuffles, mean), which shows how much the model relies on the metadata.

## What was run

| Where | Date | Sites | Rounds | Result (test split of each site) | Run time | Environment |
|---|---|---|---|---|---|---|
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 4 (PTB-XL recording sites 0, 1, 2, other) | 5 x 1 local epoch | see below | 6 s job build + 53 s simulator on 4 clients (second attempt, see below) | Host virtual environment: Python 3.12.13, torch 2.10.0, numpy 2.4.3, scikit-learn 1.8.0, pandas 2.3.3, NVFlare 2.7.1; CPU |
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 4 (PTB-XL recording sites 0, 1, 2, other) | 5 x 1 local epoch | pooled AUROC 0.925 (0.914-0.935); per site 0.877, 0.941, 0.914, 0.952 | 8 s job build + 62 s simulator on 4 clients | Runtime image `swarmcloud-app:latest` (torch 2.10, numpy 2.4, scikit-learn 1.8, NVFlare 2.7.1), container without network, CPU |
| SwarmMed local-test network on one host, CPU | 11 Nov 2026 | 4 (as above) | 5 x 1 local epoch | pooled AUROC 0.925 (0.914-0.935); per site 0.878, 0.941, 0.914, 0.952 | 11 s network start + 44 s job | Fresh installation of SwarmMed following its README, in an isolated Ubuntu 24.04 container with its own Docker Engine; runtime image built by the Hub for the network (python:3.12-slim with the platform's packages and this template's `requirements.txt`, torch 2.10.0, NVFlare 2.7.1); CPU |

SwarmMed run (11 Nov 2026, one run): the project was created from this folder with `swarmed project
create --code-dir`, the `fl-client-k` folders were imported with `swarmed data import`, and the job
was trained on a local-test network with 4 clients through the CLI steps in [Run it](#run-it). All
sites finished every round and hold identical `FL_global_model.pt` files. In the Hub's sandbox,
`validation.py` (54 checks: 51 ok, 3 warnings), `visualization.py` (4 plots) and
`results_visualization.py` (4 plots) completed, and on 12 Nov 2026 `explainability.py` stored 8
cases with 4 panels each and 3 run-level figures.

Simulator run on the host (one run, one data split, one seed; no repetition, so there is no estimate of
run-to-run spread). The first attempt stopped after 30 s with the known NVIDIA FLARE 2.7.1 swarm
race (a site is asked to start a round while its previous task is still finishing, aborts the
task and never trains again; an upstream bug, not a property of the template). The test harness
started the run again from round 0, and the second attempt completed: all four sites logged a
`SWARMMED_METRIC` line in each of the 5 rounds, and the `FL_global_model.pt` files of the four
sites are identical. The final model scored with `evaluate.py` (AUROC with a 95% percentile
bootstrap interval, 2,000 resamples of records, seed 0, which treats records as independent, see
Limitations; accuracy, sensitivity and specificity at probability 0.5; "shuffled" is the AUROC
with the metadata rows shuffled between the records of the site, mean of 5 shuffles):

| Site | Test records (abnormal) | AUROC (95% CI) | AUROC, metadata shuffled | Accuracy | Sensitivity | Specificity |
|---|---|---|---|---|---|---|
| fl-client-1 | 483 (173) | 0.878 (0.843-0.912) | 0.875 | 0.807 | 0.514 | 0.971 |
| fl-client-2 | 598 (337) | 0.941 (0.923-0.957) | 0.938 | 0.834 | 0.763 | 0.927 |
| fl-client-3 | 867 (584) | 0.914 (0.896-0.931) | 0.912 | 0.820 | 0.774 | 0.915 |
| fl-client-4 | 210 (152) | 0.952 (0.921-0.977) | 0.949 | 0.857 | 0.822 | 0.948 |
| pooled | 2,158 (1,246) | 0.925 (0.915-0.935) | 0.923 | 0.825 | 0.741 | 0.940 |

The metadata add very little. The pooled AUROC is 0.925, against 0.922 for the ECG-only model of
the [`ecg_ptbxl`](../ecg_ptbxl/README.md) template run the same way (0.874, 0.937, 0.913, 0.947
per site there); the difference is far inside the bootstrap intervals, comes from one run of each
model, and says nothing about whether the metadata help. Shuffling the metadata between records
lowers the pooled AUROC by 0.002 (by 0.002 to 0.003 at each site), so the model hardly uses
them. The 0.5 threshold is not tuned; the sensitivity at fl-client-1 is 0.51. The pooled AUROC
mixes sites with different prevalences (36% to 72%); compare the per-site values. The validation
AUROC of the received global model went from 0.66 to 0.73 at round 0 (untrained weights) to 0.91 to
0.96 at round 4 at the four sites.

A second simulator run in the runtime image (the second row; the same job, data and seeds, the
network disabled) completed on its first attempt with identical global models at the four sites. Its
results differ from the host run in the third decimal at most (the model files are not
byte-identical across the two environments), which is the size of difference to expect from
floating-point differences; it is not an independent repetition.

The sandbox scripts were first run with a local emulation of the Hub's sandbox wrapper on the same
data and the simulator's results (it runs the host virtual environment, so package versions and
memory accounting can differ from the Hub's image). `validation.py`: 54 checks, 51 ok and 3
warnings (metadata missingness at fl-client-1 and fl-client-3, and one record with a flat lead at
fl-client-4), 2.2 s, 0.37 GB peak memory. `visualization.py`: 4 plots, 2.2 s, 0.29 GB.
`results_visualization.py`: 4 plots, 13.7 s, 0.75 GB peak memory, which is close to the Hub's 1 GB
limit because importing torch alone takes about 0.5 GB. On 11 Nov 2026, `validation.py` (the same
54 checks: 51 ok, 3 warnings) and `visualization.py` (4 plots) also ran in a SwarmMedHub sandbox on
the same data. On the SwarmMed run in [What was run](#what-was-run), `results_visualization.py` and
`explainability.py` also ran in the Hub's sandbox. `explainability.py` is described below.

`prepare_data.py --download` was run on 11 Nov 2026 into an empty folder: the download (380.6 MB of
the official zip by range requests, 43,601 files all matching PhysioNet's `SHA256SUMS.txt`) took about
65 s, the whole run 83 s with 0.71 GB peak memory, and the output matched the reference counts and
fingerprints. Without the download the conversion takes about 20 s.

Not tested: GPU, more than four sites, sites on separate hosts, other random splits or seeds on
the platform, the PTB-XL 500 Hz records.

## Explainability

The paragraphs below describe the cases of the recorded run, with their outcomes. If you
want to try the blinded review on that run, do it before you read them.

`explainability.py` explains the final global model on the test split of the first site folder
(or the root data in a real deployment). It selects the two most confident cases of each outcome
(true positive, false positive, false negative, true negative; "abnormal" is the positive class;
8 cases) and draws four panels per case. The first, the input panel, is the record itself (the
12 leads in millivolts, each minus its median) with the patient's age, sex, height and weight in
its title; it shows nothing from the model. The other three are for the abnormal logit:

- **Integrated gradients** over the 12 leads, with the case's metadata held fixed: the gradient
  of the output integrated along a straight path from the all-zero record (every lead at its
  median) to the case, 32 steps; the contributions add up to the output difference from that
  reference, and the completeness gap is stored with the case. Drawn as colour behind each lead,
  smoothed over 0.25 s for display.
- **Occlusion** over the 12 leads, metadata fixed: the drop of the logit when a 0.5-s window of one
  lead is set to the lead's median, stride 0.25 s.
- **Metadata attribution**, ECG held fixed: integrated gradients over the 6 metadata inputs
  (from the test split's mean metadata vector to the case) and, per metadata group (age, sex,
  height with its missing indicator, weight with its missing indicator), the drop of the logit when
  the group is set to that mean. The panel title and the case details give the summed effect of
  the ECG and of the metadata on the logit.

Red marks input that supports "abnormal", blue input that opposes it. Each case stores the rank
agreement of the two ECG methods (the Spearman correlation of the two maps summed over the leads
per half second, and the overlap of their five largest half seconds). Three run-level figures
follow: the distribution of the model's scores on the test split by label, the agreement of the
two ECG methods per case, and the attribution per lead averaged over the explained records.

Run it locally from the repository root, with the paths of [Run it](#run-it) (the same way
the sandbox runs it; the bundle can then be checked or imported):

```bash
python -m swarmmed_explain.run_local --script examples/multimodal_ptbxl/explainability.py \
  --data tmp/ptbxl_multimodal/local-test --model tmp/ptbxl_multimodal/FL_global_model.pt \
  --out tmp/ptbxl_multimodal/explain_bundle
```

On the simulator's final model (11 Nov 2026), the run took 10.3 s pinned to 2 CPUs
(`taskset -c 0,1`) with 0.85 GB peak memory, inside the sandbox limits of 2 CPUs, 2 GB and
15 minutes, and wrote a valid bundle of 8 cases, 32 panels and 3 summaries. On the fl-client-1
test split the ECG maps are
not zero or uniform: they follow individual beats and leads. The rank correlation between
integrated gradients and occlusion over the 8 cases was between 0.41 and 0.89 (median 0.59), and
the overlap of their five largest half seconds between 2 and 4 of 5, so the methods agree on the
main regions but not on the fine ranking. The metadata effects were small next to the ECG
effects: in absolute value 0.07 to 0.51 in the logit against 1.6 to 9.4 for the ECG (the two
are measured against different references and are not strictly comparable). In the four
negative predictions (true and false negatives), all for patients aged 17 to 25 years, the
metadata as a whole push against "abnormal", which is plausible; their small size is what the
shuffling test above would lead one to expect. These are properties of one small model on eight records; they are not
clinical findings, and the maps were not compared with a clinician's reading.

Maps support review. They show what this model does with its inputs; they are not causal, they
depend on the reference (the median-lead record and the mean metadata vector), occlusion windows
of different leads overlap in their effect, the metadata inputs are correlated with each other and
with the site, and a plausible-looking map does not justify a prediction or show that the model is
correct. In SwarmMedHub a reviewer can agree, disagree or note each case. In blinded mode, a
case the reviewer has not read yet shows only its case ID, its label and the input panel, and the
Hub shows the cases in a random order, so a case's position says nothing about its outcome.

## Limitations

- One split, one seed and one training run per environment; no repetition on the platform and no
  external validation. The PTB-XL test fold comes from the same hospitals and devices as the
  training folds. The ECG-only and the multimodal model were each trained once, so the
  difference between them is not an estimate of the value of the metadata.
- **Metadata missingness encodes the site.** Height is missing for 99.8% of the records at
  fl-client-1 and about 98% at fl-client-3 but 5% at fl-client-2; weight likewise. A model can use
  the missing-value indicators to recognise the site, which the swarm then shares, so a
  site-specific shortcut could be mistaken for a clinical signal. The shuffling test shows that
  this model uses the metadata only slightly.
- Age and sex are strong predictors of ECG abnormality in general, but the metadata here are
  simple (6 values, a single linear layer), and ages of 90 and above are capped at 90.
- **Label provenance differs at fl-client-1.** Only 28.6% of its training records were validated
  by a human; the other 71.4% were not (their label is the initial automatic report), while its
  validation and test records are 100% human-validated. Abnormal prevalence there drops from 58%
  in training to 35% to 36% in validation and test, and its test AUROC (0.878) and sensitivity
  (0.51 at threshold 0.5) are the lowest. At the other sites at least 99.8% of the records in every
  split are human-validated.
- Sites differ in recording devices and population, so part of the site differences reflects data
  collection. `fl-client-4` pools every other recording site and is the smallest (1,037 training
  records).
- The label is a coarse summary (any abnormal superclass), taken from the dataset's SCP
  statements without their likelihoods; 411 records without a diagnostic superclass are excluded.
- 100 Hz records only; no filtering, no augmentation, no hyperparameter search. This is a small
  network chosen to run on CPUs, not a model for clinical use. Scores are not calibrated.
- The bootstrap intervals resample records, not patients. The four test splits hold 483, 598,
  867 and 210 records from 417, 563, 704 and 198 patients, so records of one patient are treated
  as independent and the intervals are somewhat too narrow.
- Explanation maps are not causal and are checked here only for consistency across two methods,
  not for clinical validity.
- Case selection ranks records, not patients, and the prepared files hold no patient number, so
  two explained cases can be records of the same patient. In the run above, the two false
  negatives are two records of one patient taken 17 minutes apart.
- Not a federated-privacy claim: swarm learning keeps records at the sites, but the shared model
  weights can leak information about training data; no privacy analysis was done.
