# Heart sound template: abnormal vs normal phonocardiogram recordings

A SwarmMed code bundle that trains a small convolutional network on heart sound
recordings (phonocardiograms, PCG). Each of four source databases of the PhysioNet/CinC
Challenge 2016 dataset is one site. The folder works as `--code-dir` unchanged.

The template is meant to show that training, data checks, result plots and explanations run on
SwarmMed with natural site splits. Its training has run to completion on a SwarmMed local-test
network (all sites on one host) and in NVIDIA FLARE's simulator, and its data checks, data plots,
result plots and explanations have run in a SwarmMedHub sandbox (see [What was
run](#what-was-run)). It is not a performance claim; read [Limitations](#limitations) before
interpreting any number.

## Task

Binary classification of a whole recording: abnormal (label 1) or normal (label 0). The
dataset's labels come from clinical diagnosis (abnormal: for example coronary artery
disease, valve disease; normal: healthy subjects). A recording is cut into 3-second
segments, the model scores every segment, and a recording's score is the mean of its
segments' probabilities.

## Data

- Dataset: Classification of Heart Sound Recordings: The PhysioNet/Computing in
  Cardiology Challenge 2016, version 1.0.0 (3,240 training recordings from six source
  databases, 2000 Hz after resampling by the dataset's authors).
- URL: <https://physionet.org/content/challenge-2016/1.0.0/>
- Licence: Open Data Commons Attribution License v1.0 (ODC-By 1.0). Anyone may use the
  files under the licence; attribute the dataset as below.
- Citations:
  - Liu C, Springer D, Li Q, et al. An open access database for the evaluation of heart
    sound algorithms. Physiol Meas. 2016;37(12):2181-2213. doi:10.1088/0967-3334/37/12/2181
  - Clifford GD, Liu C, Springer D, et al. Classification of Normal/Abnormal Heart Sound
    Recordings: the PhysioNet/Computing in Cardiology Challenge 2016. Computing in
    Cardiology 2016;43. doi:10.22489/CinC.2016.179-154
  - PhysioNet: Pollard T, Moody BE, Lehman L, Gow B, Fernandes C, Xie C, Johnson A, Mark RG,
    Heldt T. PhysioNet as a global platform for biomedical research. Nature Health.
    2026;1(8):792-795. https://doi.org/10.1038/s44360-026-00096-z
- How to obtain: `prepare_data.py --download` fetches `training.zip` (190 MB) and
  `annotations.zip` (11 MB) from PhysioNet, checks size and SHA-256 and extracts them, or
  download both files yourself and unpack them into one folder. Nothing in this folder
  contains dataset files. `splits.csv` holds only our own train/val/test assignment of the
  public record names.

### What `prepare_data.py` does

Uses only numpy, scipy and the standard library. For every record of a source it
(1) reads the WAV file, (2) applies a 20 Hz second-order Butterworth high-pass filter
(zero phase), (3) standardises the recording (subtract mean, divide by standard
deviation), (4) computes a 32-band log-mel spectrogram (25-800 Hz, 128-sample Hann window,
hop 40 samples = 20 ms), (5) cuts non-overlapping segments of 150 frames (3 s), at most
10 per recording, and (6) stores them as float16 with the label of the recording. The
assignment of each recording to train, val or test is read from `splits.csv`: 60/20/20
per source and class, grouped so that no group spans two splits (by subject for source b,
by raw recording for source e where the appendix identifies it, by recording otherwise).
Recordings of source e without an appendix row are excluded. After writing it reloads
every file and compares the counts with the expected ones. Normalisation statistics (per
mel band mean and standard deviation) are computed on the site's training split and
stored in `site_info.json`; `training.py`, `evaluate.py` and the Hub scripts apply them.

Options (the full commands are under [Run it](#run-it)):

```bash
python examples/audio_heart_sounds/prepare_data.py --raw-dir RAW --out OUT             # all four sites
python examples/audio_heart_sounds/prepare_data.py --raw-dir RAW --out OUT --download  # fetch the zips first
python examples/audio_heart_sounds/prepare_data.py --raw-dir RAW --out OUT --layout single --site-index 3  # source e
python examples/audio_heart_sounds/prepare_data.py --raw-dir RAW --out OUT --holdout c,d  # held-out sources
```

Counts of the default local-test layout (recordings, normal/abnormal):

| Site | Source | train | val | test |
|---|---|---|---|---|
| fl-client-1 | training-a (MIT, USA) | 71 / 176 | 23 / 58 | 23 / 58 |
| fl-client-2 | training-b (Aalborg, Denmark) | 232 / 64 | 79 / 24 | 75 / 16 |
| fl-client-3 | training-e (Dalian, China) | 1117 / 109 | 385 / 37 | 369 / 37 |
| fl-client-4 | training-f (Shiraz, Iran) | 48 / 20 | 16 / 7 | 16 / 7 |

Sources c and d (31 and 55 recordings) are too small for their own validation and test
splits; `--holdout c,d` writes them as test-only folders that `evaluate.py` scores
separately.

## Data layout

The registered data holds either one site's files at its root (a real deployment: every
site registers only its own data) or one folder per FLARE site name, in which case each
client reads only the folder named after it. The second layout lets a single-host
local-test network give each simulated site its own share.

```
fl-client-1/            (or the root of the registered data, for one real site)
  train.npz  val.npz  test.npz     x (segments, 32, 150) float16, y, rec, rec_ids, rec_y
  site_info.json                   source, counts, label map, preprocessing, normalisation
fl-client-2/ ...
```

`training.py` takes the site name from `nvflare.client.get_site_name()` (fallback: the
environment variable `SWARMMED_SITE_NAME`) and uses only keys that start with `<site>/`,
falling back to root-level files. It raises an error that lists the folders it found when
neither exists. `ALLOWED_EXTENSIONS` has to include `.npz` and `.json` (SwarmMed's default
does).

## Model

`PCGNet`: four 3x3 convolution blocks (16, 32, 64, 64 channels) with GroupNorm (no running
statistics to average across sites), ReLU and 2x2 max-pooling after the first three, then
global average pooling, dropout 0.3 and a linear layer; 60,641 weights. Training uses
random 128-frame crops of each segment, a binary cross-entropy loss weighted by the site's
class balance, Adam (learning rate 1e-3) and batches of 64. Each round every site scores
the received global model on its validation split, prints
one `SWARMMED_METRIC site=... round=... split=val ...` line, trains `LOCAL_EPOCHS = 1` epoch and sends the weights;
aggregation weights the sites by their number of training segments. `SWARM_ROUNDS = 12`.
The choice of 12 rounds of 1 epoch follows a check outside SwarmMed (a plain Python
averaging loop): 2 epochs per round gave no clear gain.

## Run it

Prepare the data once, on the machine that holds the files (see Data for what
`prepare_data.py` does). Run the commands from the repository root: the CLI runs in the
application container and reads only files inside the repository, whose `tmp/` folder git
ignores. `--download` fetches the two zips; leave it out if they are already unpacked in
`--raw-dir`.

```bash
python examples/audio_heart_sounds/prepare_data.py --download \
  --raw-dir tmp/physionet2016/raw --out tmp/physionet2016/local-test
# one site only, for a real deployment (site index 3 is source e):
python examples/audio_heart_sounds/prepare_data.py --raw-dir tmp/physionet2016/raw \
  --out tmp/physionet2016/site-3 --layout single --site-index 3
```

### Command line

Use your own user name and replace the UUIDs ([CLI](../../docs/cli.md)).

```bash
swarmed project create --user alice --title "Heart sounds" \
  --description "Audio template" --code-dir examples/audio_heart_sounds
swarmed data import --user alice --project <PROJECT_UUID> tmp/physionet2016/local-test/fl-client-*
swarmed data validate  --user alice --project <PROJECT_UUID> --wait
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
swarmed network create --user alice --project <PROJECT_UUID> --name "Heart sounds" --local-test --clients 4
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
site in a real deployment imports its files instead (`tmp/physionet2016/site-3/*`), which
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
   interface creates two clients, so only `fl-client-1` (source a) and `fl-client-2` (source b)
   train; for all four sources, create the network with the CLI.
4. On the **Results** page, select the job, click **Run Visualization**, and click **Run
   Explainability** in the **Explainability** section to see and review the explained cases.

`validation.py` checks files, shapes, counts, split overlap and class balance per site;
`visualization.py` plots class balance, recording durations, mean mel profiles and example
segments; `results_visualization.py` plots ROC curves, recording scores, AUROC with a bootstrap
interval and sensitivity/specificity on each site's test split.

### Score a final model at a site

Download `FL_global_model.pt` from the **Results** page or with `swarmed results export` and
save it as `tmp/physionet2016/FL_global_model.pt`. `evaluate.py` scores it on a site's test
split, or on every `fl-client-k` folder and pooled; run it from the repository root:

```bash
python examples/audio_heart_sounds/evaluate.py --checkpoint tmp/physionet2016/FL_global_model.pt \
  --data-dir tmp/physionet2016/local-test --out tmp/physionet2016/eval.json
```

## What was run

Each row states where, when, which sites and rounds, the result and the runtime. Results
are recording-level AUROC on each site's test split with a 95% bootstrap interval
(recordings resampled, 2000 resamples; too narrow for fl-client-2 and fl-client-3, see
Limitations), from `evaluate.py` on the final global model.

| Where | Date | Sites | Rounds | Result (test AUROC) | Runtime |
|---|---|---|---|---|---|
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 4 (training-a, b, e, f) | 12 x 1 local epoch | pooled 0.807 (0.758-0.856, 601 recordings, 118 abnormal); fl-client-1 0.710 (0.583-0.831); fl-client-2 0.396 (0.267-0.520); fl-client-3 1.000 (1.000-1.000); fl-client-4 0.491 (0.223-0.737) | 7 s job build + 157 s simulation (2 min 43 s wall) on a 24-core host, 4 threads per client; host Python 3.12 virtual environment of SwarmMed (torch 2.10.0, numpy 2.4.3, NVIDIA FLARE 2.7.1) |
| SwarmMed local-test network on one host, CPU | 12 Nov 2026 | 4 (training-a, b, e, f) | 12 x 1 local epoch | pooled 0.801 (0.752-0.851); fl-client-1 0.663 (0.538-0.787); fl-client-2 0.423 (0.301-0.548); fl-client-3 1.000 (1.000-1.000); fl-client-4 0.402 (0.119-0.686) | 11 s network start + 1 min 25 s job; fresh installation of SwarmMed following its README, in an isolated Ubuntu 24.04 container; runtime image built by the Hub for the network (python:3.12-slim, torch 2.10.0, NVIDIA FLARE 2.7.1); CPU |

SwarmMed run (12 Nov 2026, one run): the project was created from this folder with `swarmed project
create --code-dir`, the `fl-client-k` folders were imported with `swarmed data import`, and the job
was trained on a local-test network with 4 clients through the CLI steps in [Run it](#run-it). All
sites finished every round and hold identical `FL_global_model.pt` files. In the Hub's sandbox,
`validation.py` (31 checks, all ok), `visualization.py` (4 plots) and `results_visualization.py` (4
plots) completed, and on 12 Nov 2026 `explainability.py` stored 7 cases with 4 panels each and 2
run-level figures.

Details of the simulator run: 4 sites x 12 rounds gave 48 `SWARMMED_METRIC` lines, every
site finished all rounds, and the four sites hold byte-identical `FL_global_model.pt`
files. At the default threshold of 0.5, the per-recording sensitivity/specificity on the
test splits were 0.16/0.96 (fl-client-1), 0.00/0.87 (fl-client-2), 1.00/1.00
(fl-client-3), 0.00/0.94 (fl-client-4), and 0.39/0.98 pooled. On 11 Nov 2026,
`validation.py` (31 checks, all ok) and `visualization.py` (4 plots) ran in a SwarmMedHub
sandbox on the same data. On the simulator's model, the results visualisation ran in a
local emulation of the sandbox wrapper and the explanations with
`swarmmed_explain.run_local`; on the SwarmMed run's model, both ran in the Hub's sandbox.
The SwarmMed run's pooled AUROC (0.801) differs from the simulator run's (0.807), and so do
the per-site values; with one run of each, the size of the run-to-run variation is not
known.

How to read these numbers: the pooled AUROC of 0.807 is carried by fl-client-3 (source e).
In that source the healthy volunteers and the patients were recorded with different
devices in different places and differ in age (see Limitations), so its AUROC of 1.000
reflects the recording conditions and not disease detection. On the three other sites the
model is weak at source a (0.71, CI 0.58-0.83) and near chance at b (0.40) and f (0.49,
with a very wide interval).
Reaching these values needs nothing more than 12 rounds of 1 local epoch. Nothing was
tuned, and one run with one seed was made.

## Explainability

The paragraphs below describe the cases of the recorded run, with their outcomes. If you
want to try the blinded review on that run, do it before you read them.

`explainability.py` runs in SwarmMedHub's sandbox like the results visualisation (2 CPUs,
2 GB, 15 minutes), with the site's data and the job's result files, and uses the `explain`
helper and the `swarmmed_explain` package of this repository. It

1. loads the final global model and the test split of the first site folder it finds (or
   the root-level files) and scores every test recording;
2. selects up to 2 recordings per outcome (TP, FP, FN, TN) with `select_cases`, the most
   confident ones of each outcome (fewer when the model produces fewer cases of an outcome:
   in the run below there was only one FP);
3. for each, draws the input panel first: the log-mel spectrogram of the recording's middle
   3-second segment, chosen without the model, so it shows nothing from the model;
4. explains the 3-second segment that pushes the recording's score furthest towards the
   model's decision (often not the middle one), with
   - occlusion over time-frequency patches (8 mel bands x 15 frames, stride 4 x 8): the
     drop of the abnormal logit when a patch is replaced by the site's mean level;
   - integrated gradients of the abnormal logit from the site's mean level (32 steps,
     Gauss-Legendre);
   - the two maps summed over the mel bands, as curves over time;
5. adds two run-level figures: the integrated gradients summed over time, by frequency
   band and outcome, and the overlap of the two methods' top 10% cells per case.

Each case holds the recording ID (the dataset's record name, for example `a0242`), label,
prediction, score, outcome and details (source, number of segments, which segment was
explained, its probability, the integrated gradients completeness gap and the agreement
between the methods). The two map panels draw the explained segment in grey with the
attribution on top: red supports "abnormal", blue supports "normal" (time in seconds within
the segment); the input panel shows its segment in grey only, with time in seconds within
the recording. In these three panels the frequency axis has one row per mel band, labelled
with band frequencies in Hz, so its tick spacing in Hz is uneven.

Run on the simulator's final model (11 Nov 2026, fl-client-1 test split, 81 recordings): 7
cases (2 TP, 1 FP, 2 FN, 2 TN), 28 panels and 2 figures; 3.6 s wall pinned to 2 CPUs
(`taskset -c 0,1`) including the bundle validation, peak memory about 0.8 GB (the larger
part is importing torch). The two methods partly agree: the Spearman correlation between
the pixel maps is 0.09-0.29 (the occlusion map is blocky, the gradient map is fine
grained), 0.54-0.87 after both are averaged to the occlusion patch grid, and the
top-10% cells overlap by 14-36%. The explained model is the global model at fl-client-1,
where it is weak (AUROC 0.71): its maps show where this model looked, not where a physician
would look.

Maps support a reviewer's reading of a recording. They do not show why a heart sound is
abnormal, they depend on the baseline and window sizes chosen here, and they must not be
used to justify a prediction. In SwarmMedHub a reviewer can agree, disagree or note each
case. In blinded mode, a case the reviewer has not read yet shows only its case ID, its
label and the input panel, and the Hub shows the cases in a random order, so a case's
position says nothing about its outcome.

Locally, without the Hub, from the repository root with the paths of [Run it](#run-it):

```bash
python -m swarmmed_explain.run_local --script examples/audio_heart_sounds/explainability.py \
  --data tmp/physionet2016/local-test --model tmp/physionet2016/FL_global_model.pt \
  --out tmp/physionet2016/explain_bundle --threads 2
```

## Limitations

- Source e is confounded. Healthy volunteers were recorded in a laboratory with a
  microphone or piezoelectric sensor, patients in a hospital with an electronic
  stethoscope, and the groups differ in age (about 25 versus 60 years). Within-source
  AUROCs near 1.0 on e, and a pooled AUROC driven by e, say little about heart sounds.
  Source b is weak: the model is near chance on it (AUROC 0.40). The challenge paper's
  leave-one-database-out check of its baseline classifier gave AUROCs between 0.47 and
  0.86 across databases, so a large shift between sources is expected.
- Recording-level splits do not control all leakage. Subject identifiers exist only for
  source b. Recordings of one subject can fall into train and test in sources a, d and
  f, and the interpretation of the appendix column that groups source e's cuts of one raw
  recording is our inference (PhysioNet does not document it). Healthy volunteers of e can
  contribute several raw recordings.
- Small test splits: 23 recordings at fl-client-4 (7 abnormal), 81 at fl-client-1, 91 at
  fl-client-2 (16 abnormal). The intervals are wide; pooled intervals mix sources and
  are not an estimate for any population.
- The bootstrap intervals treat recordings as independent, but the split unit is the
  subject at source b and the raw recording at source e. The 91 test recordings of b come
  from 21 subjects (the 16 abnormal ones from 4), and the 406 of e from 81 raw recordings,
  so the intervals for fl-client-2 and fl-client-3 are too narrow. Resampling the subjects
  of b instead (a check run on 12 Nov 2026: 2,000 resamples, 1,978 of them with both
  classes) gives 0.21-0.58 for fl-client-2, against 0.27-0.52 in the table.
- The pooled AUROC mixes recordings of four sites scored by one global model. Sites use
  their own normalisation statistics, and the abnormal share of recordings differs
  between 9% (e) and 71% (a), so the pooled value depends on the mix of sites.
- Only one configuration and one seed were run. No hyperparameter search was made. The
  hand-corrected segmentation shipped with the dataset is not used. The 87 excluded
  recordings of source e are all normal.
- Not tested in the runs above: GPU, a real multi-site network (the SwarmMed run had
  all four sites on one host), and the held-out sources c and d on the model of the
  SwarmMed run.
- Explanation maps are a post hoc description of one model. They were not validated
  against clinical annotations, and one test split of one site was used.
