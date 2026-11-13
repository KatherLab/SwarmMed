# EEG template: sleep staging from the Sleep-EDF Expanded recordings

A time-series example for SwarmMed. Four sites each hold whole-night EEG recordings of nine healthy
subjects, and a small 1-D convolutional network scores every 30-second epoch of the night as wake,
N1, N2, N3 or REM sleep from two EEG channels. The sites are formed from the public data by subject
(age-stratified), so every site holds different people, and each site's validation and test split
is a subject the site's training data never contains. The template is meant to show that training,
data checks, result plots and explanations run on SwarmMed with a subject-level split. Its training
has run to completion on a SwarmMed local-test network (all sites on one host) and in NVIDIA
FLARE's simulator, and its data checks, data plots, result plots and explanations have run in a
SwarmMedHub sandbox (see [What was run](#what-was-run)). It does not make a performance claim.

## Task

Five-class classification of 30-s epochs: W (wake), N1, N2, N3 (the Rechtschaffen and Kales
stages 3 and 4 merged, as in the AASM scheme) and REM. The input is one epoch of the two EEG
channels Fpz-Cz and Pz-Oz at 100 Hz, 3,000 samples per channel. The reference labels are the
manual hypnograms of the dataset. Stages are very unequal in frequency (N1 and N3 are rare), so
the template reports macro-F1 and Cohen's kappa next to accuracy and trains with class weights.

## Data

- Dataset: Sleep-EDF Database Expanded (sleep-edfx), version 1.0.0, the sleep-cassette (SC)
  study: whole-night polysomnograms of healthy Caucasian volunteers aged 25 to 101 recorded at
  home in 1987 to 1991, with hypnograms scored by technicians.
- Citation: Kemp B, Zwinderman AH, Tuk B, Kamphuisen HAC, Oberye JJL. Analysis of a
  sleep-dependent neuronal feedback loop: the slow-wave microcontinuity of the EEG. IEEE
  Transactions on Biomedical Engineering 47(9):1185-1194 (2000).
  https://doi.org/10.1109/10.867928. The study is described in Mourtazaev MS, Kemp B, Zwinderman
  AH, Kamphuisen HAC. Sleep 18(7):557-564 (1995), https://doi.org/10.1093/sleep/18.7.557.
  Dataset DOI: https://doi.org/10.13026/C2X676 (PhysioNet). Also cite PhysioNet: Pollard T,
  Moody BE, Lehman L, Gow B, Fernandes C, Xie C, Johnson A, Mark RG, Heldt T. PhysioNet as a
  global platform for biomedical research. Nature Health. 2026;1(8):792-795.
  https://doi.org/10.1038/s44360-026-00096-z
- Licence: Open Data Commons Attribution License v1.0 (ODC-By 1.0), as stated on the PhysioNet
  page. Keep the attribution above when you redistribute the data or derived files.
- Page: https://physionet.org/content/sleep-edfx/1.0.0/
- Download (done by `prepare_data.py --download`): the 72 EDF files below, from
  `https://physionet.org/files/sleep-edfx/1.0.0/`, 1.78 GB in all, plus `SHA256SUMS.txt`.
  `prepare_data.py` checks every file against the PhysioNet checksums.

The template uses 36 of the 78 subjects of the SC study, one night each (night 1; night 2 for
subjects 36 and 52, whose first night is missing from the dataset): the subjects at evenly
spaced age ranks, so the age range of the dataset (25 to 101 years) is covered. Subjects sorted
by age are dealt round-robin to four sites, so every site has young and old subjects. Each site
holds 7 training subjects, 1 validation subject and 1 test subject (drawn once with a fixed
seed and written into `prepare_data.py`).

| Site folder | Subjects (train / val / test) | Epochs train | Epochs val | Epochs test | Validation / test subject |
|---|---|---|---|---|---|
| `fl-client-1` | 7 / 1 / 1 | 7,718 | 972 | 983 | 25 / 46 |
| `fl-client-2` | 7 / 1 / 1 | 10,970 | 1,535 | 904 | 19 / 23 |
| `fl-client-3` | 7 / 1 / 1 | 9,859 | 1,022 | 843 | 20 / 6 |
| `fl-client-4` | 7 / 1 / 1 | 8,738 | 1,840 | 1,078 | 59 / 41 |

46,462 labelled epochs in all, 493 MiB prepared. What `prepare_data.py` does (numpy, scipy and
the standard library only; it has its own small EDF reader):

1. optionally downloads the 36 PSG and 36 hypnogram files and `SHA256SUMS.txt`, and checks the
   SHA-256 of every EDF file;
2. reads the two EEG channels in microvolts and cuts the recording into 30-s epochs by sample
   index (3,000 samples);
3. labels each epoch from the hypnogram (W 0, N1 1, N2 2, stages 3 and 4 as N3 3, REM 4) and
   drops "Sleep stage ?", "Movement time" and unannotated epochs;
4. keeps only the span from 30 minutes (60 epochs) before the first to 30 minutes after the last
   sleep epoch, so the long recorded daytime wake periods do not dominate;
5. band-pass filters the kept segment per channel (4th-order Butterworth, 0.3 to 35 Hz, zero
   phase) and scales each recording and channel by (x - median) / IQR, clipped to [-20, 20].
   The scaling values come from the kept segment of the subject's own recording, never from
   other subjects or sites; the scored hypnogram chooses that segment (step 4), so the labels
   decide which part of the night the median and IQR are computed over;
6. writes `train.npz`, `val.npz`, `test.npz` and `site_info.json` per site (float16 signals) and
   stops if the epoch counts differ from the values it was written for.

## Data layout

```text
fl-client-1/   train.npz  val.npz  test.npz  site_info.json
fl-client-2/   ...
fl-client-3/   ...
fl-client-4/   ...
```

Arrays in each `.npz`: `X` float16 `[n, 2, 3000]` (channels Fpz-Cz, Pz-Oz), `y` int8 (0 to 4 for
W, N1, N2, N3, REM), `subject` int16 (the subject number of the SC study, a pseudonym), `night`
int8, `epoch` int32 (index of the epoch in the recording; epochs of one subject are in time
order). `site_info.json` lists the files, subjects, counts per stage, array fingerprints and the
preprocessing steps.

- **Local-test network on one host:** import the `fl-client-k` folders themselves (see
  [Run it](#run-it)), so that every key starts with `fl-client-k/`. Each client reads only the
  folder named after its FLARE site name.
- **Real deployment:** each site runs `prepare_data.py --layout single --site-index K` (or
  prepares its own recordings in the same format) and registers only its own files at the root of
  its data.

## Model

`SleepStageCNN`: a 1-D convolutional network with three convolution blocks (batch normalisation,
ReLU, max pooling), global average pooling and one linear layer; 52,997 parameters. It is a
small model written for this template, not the published EEGNet or DeepSleepNet. Each round a
site scores the received global model on its validation subject, trains it for 2 local epochs
over its training epochs (Adam, learning rate 1e-3, batch size 128, new optimiser each round,
cross-entropy with class weights from the site's training counts) and returns the weights with
its number of training epochs as the aggregation weight. 10 swarm rounds (`SWARM_ROUNDS` in
`training.py`). Every site starts from the same seeded initial weights. Batch normalisation
statistics are averaged with the weights.

The round and epoch counts come from an in-process simulation of the swarm with the template's
own functions (one seed): 10 rounds of 1 local epoch gave a pooled test accuracy of 0.744 and
10 rounds of 2 epochs 0.760; the validation accuracy levels off after rounds 4 to 5. On a
local-test network the rounds, not the computation, set the run time (a site trains a round in
a few seconds on 4 CPU threads).

The test split is used only by `evaluate.py`, `results_visualization.py` and
`explainability.py`, never during training. Every site prints one line per round:

```text
SWARMMED_METRIC site=fl-client-1 round=9 split=val val_acc=0.7407 val_macro_f1=0.4980 val_kappa=0.5894 val_loss=0.6538 n_val=972
```

and sends the same values as metrics. The reported validation accuracy is that of the global
model received at the start of the round, so round 0 is the seeded initial model (accuracy near
chance or below) and round 9 is the model after 9 rounds of aggregation.

## Files

| File | Role |
|---|---|
| `training.py` | Client-API training loop (needs only numpy, scikit-learn, torch) |
| `requirements.txt` | Pinned runtime packages |
| `validation.py` | 43 data checks in the Hub sandbox (shapes, labels, class counts, subjects not shared between splits or sites, counts and fingerprints against `site_info.json`, and a summary line) |
| `visualization.py` | 4 data plots: epochs per stage by site and split, an example epoch per stage, mean power spectrum per stage, hypnogram of a validation subject |
| `results_visualization.py` | 4 result plots: pooled confusion matrix, accuracy, macro-F1 and kappa per site, F1 per stage and site, scored and predicted hypnogram of a test subject |
| `explainability.py` | Per-case explanations (see Explainability) |
| `prepare_data.py` | Downloads and converts the Sleep-EDF files into the layout above; run by a site before import; uploaded with `--code-dir` but not used by the platform |
| `evaluate.py` | Scores a saved global model on a site's test split (per site and pooled), optionally with bootstrap intervals |

`prepare_data.py` and `evaluate.py` are harmless in the code bundle but are not needed there.
Keep the prepared data and the raw EDF files outside this folder: every file in it is uploaded
as training code.

## Run it

Prepare the data once, on the machine that holds the files (about 1.8 GB to download, 201 s at
the rate measured on the day, then 30 s of computation). Run the commands from the repository
root: the CLI runs in the application container and reads only files inside the repository,
whose `tmp/` folder git ignores. `--download` fetches the recordings; leave it out if they are
already in `--raw-dir`.

```bash
python examples/eeg_sleep_staging/prepare_data.py --download \
  --raw-dir tmp/sleep_edf/raw --out tmp/sleep_edf/local-test
# one site only, for a real deployment:
python examples/eeg_sleep_staging/prepare_data.py --raw-dir tmp/sleep_edf/raw \
  --out tmp/sleep_edf/site-2 --layout single --site-index 2
```

### Command line

Use your own user name and replace the UUIDs ([CLI](../../docs/cli.md)).

```bash
swarmed project create --user alice --title "Sleep staging" \
  --description "EEG template" --code-dir examples/eeg_sleep_staging
swarmed data import --user alice --project <PROJECT_UUID> tmp/sleep_edf/local-test/fl-client-*
swarmed data validate  --user alice --project <PROJECT_UUID> --wait
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
swarmed network create --user alice --project <PROJECT_UUID> --name "Sleep local" --local-test --clients 4
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
site in a real deployment imports its files instead (`tmp/sleep_edf/site-2/*`), which stores
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
   interface creates two clients, so only `fl-client-1` and `fl-client-2` (18 of the 36
   subjects) train; for all four sites, create the network with the CLI.
4. On the **Results** page, select the job, click **Run Visualization**, and click **Run
   Explainability** in the **Explainability** section to see and review the explained cases.

### Score a final model at a site

Download `FL_global_model.pt` from the **Results** page or with `swarmed results export`, save
it as `tmp/sleep_edf/FL_global_model.pt`, then, from the repository root:

```bash
python examples/eeg_sleep_staging/evaluate.py --model tmp/sleep_edf/FL_global_model.pt \
  --data tmp/sleep_edf/local-test --bootstrap 1000 \
  --out tmp/sleep_edf/result.json                    # all sites + pooled
python examples/eeg_sleep_staging/evaluate.py --model tmp/sleep_edf/FL_global_model.pt \
  --data tmp/sleep_edf/site-2                        # one site
```

Use the final `FL_global_model.pt`; `best_FL_global_model.pt` is selected by the validation
accuracy of one subject per site and is not a reliable choice.

## What was run

| Where | Date | Sites | Rounds | Result (test split of each site) | Run time | Environment |
|---|---|---|---|---|---|---|
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 4 (36 subjects) | 10 x 2 local epochs | see below | 11 s job build + 70 s simulator on 4 clients | Host virtual environment: Python 3.12.13, torch 2.10.0, numpy 2.4.3, scikit-learn 1.8.0, NVFlare 2.7.1; CPU (4 threads per client) |
| SwarmMed local-test network on one host, CPU | 12 Nov 2026 | 4 (36 subjects) | 10 x 2 local epochs | pooled accuracy 0.751, macro-F1 0.705, kappa 0.661 (simulator: 0.754, 0.707, 0.665) | 11 s network start + 60 s job | Fresh installation of SwarmMed following its README, in an isolated Ubuntu 24.04 container with its own Docker Engine; runtime image built by the Hub for the network (python:3.12-slim with the platform's packages and this template's `requirements.txt`, torch 2.10.0, NVFlare 2.7.1); CPU |

SwarmMed run (12 Nov 2026, one run): the project was created from this folder with `swarmed project
create --code-dir`, the `fl-client-k` folders were imported with `swarmed data import`, and the job
was trained on a local-test network with 4 clients through the CLI steps in [Run it](#run-it). All
sites finished every round and hold identical `FL_global_model.pt` files. In the Hub's sandbox,
`validation.py` (43 checks: 39 ok, 4 warnings), `visualization.py` (4 plots) and
`results_visualization.py` (4 plots) completed, and on 12 Nov 2026 `explainability.py` stored 8
cases with 4 panels each and 2 run-level figures.

Simulator run (one training run with seed 0 and one subject split). All four sites logged a
`SWARMMED_METRIC` line in each of the 10 rounds, and the `FL_global_model.pt` files of the four
sites are identical. An earlier run of the same code (before a whitespace and constant clean-up
of `training.py`) gave a pooled accuracy of 0.761, macro-F1 0.708 and kappa 0.671, so the
run-to-run difference caused by the order of floating-point additions and thread scheduling is
about 0.01; there is no estimate of the spread over seeds or splits. The final model scored
with `evaluate.py --bootstrap 1000` (95% percentile intervals; the resampling unit is a block
of 20 consecutive epochs, 10 minutes of one night, because epochs of a night are not
independent):

| Site (test subject) | Epochs | Accuracy (95% CI) | Macro-F1 (95% CI) | Cohen's kappa (95% CI) |
|---|---|---|---|---|
| fl-client-1 (46) | 983 | 0.712 (0.638-0.783) | 0.625 (0.570-0.670) | 0.609 (0.519-0.699) |
| fl-client-2 (23) | 904 | 0.752 (0.694-0.807) | 0.629 (0.587-0.657) | 0.645 (0.545-0.723) |
| fl-client-3 (6) | 843 | 0.861 (0.819-0.900) | 0.815 (0.757-0.851) | 0.808 (0.737-0.863) |
| fl-client-4 (41) | 1,078 | 0.711 (0.652-0.762) | 0.679 (0.635-0.714) | 0.619 (0.541-0.682) |
| pooled (4 subjects) | 3,808 | 0.754 (0.721-0.785) | 0.707 (0.669-0.742) | 0.665 (0.618-0.707) |

F1 per stage, pooled: W 0.90, N1 0.48, N2 0.80, N3 0.54, REM 0.81. N3 is rare in some
subjects (test epochs of N3: 9, 12, 136 and 46 at sites 1 to 4; 4 in the validation subject of
site 3), and the F1 for N3 is 0.11 at fl-client-1 and 0.96 at fl-client-3. The interval of a
single site covers only the variation within that subject's night, not the variation between
subjects, so it is too narrow as a statement about new subjects; the pooled interval has the
same limit with four subjects. The validation accuracy of the received global model at round 0
and round 9 was 0.10 and 0.74 (fl-client-1), 0.08 and 0.83 (fl-client-2), 0.04 and 0.86
(fl-client-3), 0.11 and 0.71 (fl-client-4).

The sandbox scripts were first run with a local emulation of the Hub's sandbox wrapper on the same
data and the simulator's results: `validation.py` 43 checks, 39 ok and 4 warnings (too few N3
epochs: 9 and 12 in the test splits of fl-client-1 and 2, 4 in the validation split of fl-client-3;
the fourth is the summary line, which takes the worst status of the others) in 3.7 s with 0.36 GB
peak memory, `visualization.py` 4 plots in 1.5 s (0.27 GB) and `results_visualization.py` 4 plots
in 2.3 s with 0.75 GB peak memory, inside the Hub's 1 GB and 5 minute limits. On 11 Nov 2026,
`validation.py` (the same 43 checks: 39 ok, 4 warnings) and `visualization.py` (4 plots) also ran
in a SwarmMedHub sandbox on the same data. On the SwarmMed run in [What was run](#what-was-run),
`results_visualization.py` and `explainability.py` also ran in the Hub's sandbox.
`explainability.py` is described below.

Not tested: GPU, more than four sites, sites on separate hosts, other subject splits or seeds,
`results_visualization.py` and `explainability.py` in the Hub's sandbox image (their emulation
used the host environment).

## Explainability

The paragraphs below describe the cases of the recorded run, with their outcomes. If you
want to try the blinded review on that run, do it before you read them.

`explainability.py` explains the final global model on the test split of the first site folder
(or the root data in a real deployment). It scores the 983 test epochs of that site and selects
the four most confident correct and the four most confident incorrect epochs, each of a
different scored stage (8 cases). For each case it draws four panels. The first, the input
panel, shows the 30-s epoch (shaded) with the epochs before and after it, for both channels; it
shows nothing from the model. The other three are for the predicted stage:

- **Integrated gradients:** the gradient of the predicted stage's logit integrated along a
  straight path from an all-zero input (the median of the scaled signal) to the epoch, 48 steps;
  the attributions add up to the logit difference from the zero input, and the remaining gap is
  stored with the case. Drawn over the 30-s signal of each channel, smoothed over 1 s.
- **Occlusion:** the drop of the predicted stage's logit when a 1-s window of one channel is set
  to zero (windows step 0.5 s; every sample gets the mean over the windows that cover it).
- **Probabilities and method agreement:** the class probabilities and the two attributions
  summed over channels and per second, with their Spearman correlation and the overlap of their
  six largest seconds (stored with the case).

Red marks time that supports the predicted stage, blue time that opposes it. Two run-level
figures follow: the confusion matrix of the site's test split and the agreement of the two
methods for each case.

Run it locally from the repository root, with the paths of [Run it](#run-it) (the same way
the sandbox runs it; the bundle can then be checked or imported):

```bash
python -m swarmmed_explain.run_local --script examples/eeg_sleep_staging/explainability.py \
  --data tmp/sleep_edf/local-test --model tmp/sleep_edf/FL_global_model.pt \
  --out tmp/sleep_edf/explain_bundle
```

On the simulator's final model (11 Nov 2026), the run took 4.0 s pinned to 2 CPUs with 0.78 GB
peak memory, inside the sandbox limits of 2 CPUs, 2 GB and 15 minutes, and wrote a valid bundle
of 8 cases, 32 panels and 2 summaries. The maps are not empty and the two methods partly agree:
over the 8 cases the Spearman correlation of the per-second attributions was 0.27 to 0.78
(median 0.65) and 3 to 5 of their six largest seconds were shared in 7 of 8 cases (2 of 6
in the REM case with the lowest correlation). In the correctly scored wake epoch both methods
mark the seconds with large deflections in Fpz-Cz that look like eye movements or blinks. In
the wrongly predicted epochs inspected the maps spread over most of the epoch with mixed signs.
These are properties of one small model and one subject; they are not
physiological findings.

Maps support review. They show what this model does with its input; they are not causal, they
depend on the zero reference and on the window size, neighbouring seconds and the two channels
share credit, and a plausible-looking map does not justify a prediction or show that the model
is correct. The model's probabilities are not calibrated. In SwarmMedHub a reviewer can agree,
disagree or note each case. In blinded mode, a case the reviewer has not read yet shows only its
case ID, its label and the input panel, and the Hub shows the cases in a random order, so a
case's position says nothing about its outcome.

## Limitations

- Few subjects: 9 per site and one test subject per site. Per-site metrics, especially for N1
  and N3, depend on that one night, and the 95% intervals above do not include the variation
  between subjects. The pooled value covers four test subjects.
- Healthy volunteers recorded at home with one device type and manual scoring by technicians
  according to Rechtschaffen and Kales; the model is not validated for patients, other
  devices or other scoring conventions. Subjects are dealt to the sites by age, so the sites have
  a similar age mix; they are not different hospitals, and differences between sites are mostly
  differences between nine people.
- N1 is hard (F1 about 0.5 here), N3 is rare in some subjects, and the model confuses N2
  with N3 and wake with N1; these are not tuned away.
- One epoch is scored on its own: no context from neighbouring epochs, which sleep-staging
  models usually use. The model is small and not tuned (one seed, two schedules compared).
- The scored hypnogram chooses the part of each night that is kept (30 minutes before the first
  to 30 minutes after the last scored sleep epoch), and the scaling of each recording is
  computed over that part. This is not leakage between subjects, but the test subjects' inputs
  also depend on their scoring, and an unscored recording cannot be prepared in the same way.
- The simulator results are one run per environment; they are not an estimate of run-to-run
  spread over seeds or splits.
- Explanation maps are not causal and are checked here only for non-triviality and agreement
  between methods, not for physiological validity.
- Not a federated-privacy claim: swarm learning keeps records at the sites, but the shared
  model weights can leak information about training data; no privacy analysis was done.
