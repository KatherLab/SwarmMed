# ECG template: normal or abnormal 12-lead ECG from PTB-XL

A signal template for SwarmMed. Four recording sites each hold their own 12-lead ECGs from the
public PTB-XL dataset, and a small 1D convolutional network learns to tell normal from abnormal
records. The data are about 320 MB in all, so a swarm run of five rounds finishes in minutes on a
CPU. The template is meant to show that training, data checks, result plots and per-case
explanations run on SwarmMed with a natural site split (PTB-XL's recording-site field). Its training
has run to completion on a SwarmMed local-test network (all sites on one host), in NVIDIA FLARE's
simulator and, with a development version of SwarmMed (changes now in 1.0.3), across three
hospitals, each training on its own server; its data checks, data plots, result plots and
explanations have run in a SwarmMedHub sandbox (see [What was run](#what-was-run)). It does not make
a performance claim. A second template, [`multimodal_ptbxl`](../multimodal_ptbxl/README.md), adds
age, sex, height and weight to the same task.

## Task

Binary classification of a 10-second, 12-lead ECG at 100 Hz: **normal** (label 0: the record's
only diagnostic superclass is NORM) or **abnormal** (label 1: any of MI myocardial infarction,
STTC ST/T change, CD conduction disturbance, HYP hypertrophy). The five superclasses are kept in
the data files (`y_super`) for the explanation and result plots; training uses the binary label.
Records without any diagnostic superclass are dropped (411 of 21,799).

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
  `LICENSE.txt`, `SHA256SUMS.txt` and the `records100/` folder (about 414 MB, using HTTP range
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
4. writes `train.npz`, `val.npz`, `test.npz` and `site_info.json` per site, reads them back, and
   compares the counts and SHA-256 fingerprints of the arrays with the values of the reference
   preparation (`--skip-verify` turns this off).

No filtering or resampling is applied to the signals. `training.py` converts each record to
millivolts and subtracts each lead's median when it loads the data; nothing is computed across
records or sites.

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
ID in explanations). `site_info.json` holds the source, licence, counts, label map and
preprocessing.

- **Local-test network on one host:** import the `fl-client-k` folders themselves (see
  [Run it](#run-it)), so that every key starts with `fl-client-k/`. Each client reads only the
  folder named after its FLARE site name.
- **Real deployment:** each site runs `prepare_data.py --layout single --site-index K` and
  registers only its own files, at the root of its data. The client then finds `train.npz` and
  `val.npz` at the root. The prepared folder of the multimodal template also works here (it holds
  the same four arrays plus metadata).

## Model

`ECGNet`: four blocks of Conv1d (kernels 7, 7, 5, 5; 32, 64, 64, 128 channels; no bias),
BatchNorm, ReLU and max pooling by 2, then global average pooling over time and a linear layer
with one output logit (the log-odds of "abnormal"); 79,169 parameters. Each round a site scores
the received global model on its validation split, trains it for 1 local epoch (Adam, learning
rate 1e-3, batch size 128, a new optimiser each round) and returns the weights with its number of
training records as the aggregation weight. 5 swarm rounds (`SWARM_ROUNDS` in `training.py`). The
schedule comes from a plain-PyTorch check before the platform runs: 5 rounds of 1 local epoch
gave a pooled test AUROC of 0.921, the same as 3 epochs of central training on the pooled data
(0.922). The binary task is learned within one epoch, so more rounds add little.

The test split is used only by `evaluate.py`, `results_visualization.py` and `explainability.py`,
never during training. Every site prints one line per round:

```text
SWARMMED_METRIC site=fl-client-1 round=4 split=val val_auroc=0.9221 val_acc=0.8370 val_loss=0.3516 n=497 model=global
```

Round 0 scores the untrained, seeded initial weights (an AUROC below 0.5 there is noise from the
random weights, not a result); later rounds score the aggregated model of the previous round.

## Files

| File | Role |
|---|---|
| `training.py` | Client-API training loop (needs only numpy, scikit-learn, torch) |
| `requirements.txt` | Pinned runtime packages |
| `validation.py` | Data checks in the Hub sandbox (files, shapes, labels, `y` against `y_super`, unique record numbers, no record in two splits, flat leads, class counts, `site_info.json` against the arrays) |
| `visualization.py` | 4 data plots: records per site and split by label, superclass shares per site, one normal and one abnormal record, signal level per lead and site |
| `results_visualization.py` | 4 result plots: validation AUROC per round, ROC curves, test AUROC with bootstrap 95% CI per site and pooled, share of records called abnormal per superclass |
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
python examples/ecg_ptbxl/prepare_data.py --download \
  --raw-dir tmp/ptbxl_ecg/raw --out tmp/ptbxl_ecg/local-test
# one site only, for a real deployment:
python examples/ecg_ptbxl/prepare_data.py --raw-dir tmp/ptbxl_ecg/raw \
  --out tmp/ptbxl_ecg/site-2 --layout single --site-index 2
```

### Command line

Use your own user name and replace the UUIDs ([CLI](../../docs/cli.md)).

```bash
swarmed project create --user alice --title "PTB-XL ECG" \
  --description "ECG template" --code-dir examples/ecg_ptbxl
swarmed data import --user alice --project <PROJECT_UUID> tmp/ptbxl_ecg/local-test/fl-client-*
swarmed data validate  --user alice --project <PROJECT_UUID> --wait
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
swarmed network create --user alice --project <PROJECT_UUID> --name "ECG local" --local-test --clients 4
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
site in a real deployment imports its files instead (`tmp/ptbxl_ecg/site-2/*`), which stores
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
   interface creates two clients, so only `fl-client-1` and `fl-client-2` (recording sites 0 and
   1) train; for all four sites, create the network with the CLI.
4. On the **Results** page, select the job, click **Run Visualization**, and click **Run
   Explainability** in the **Explainability** section to see and review the explained cases.

### Score a final model at a site

Download `FL_global_model.pt` from the **Results** page or with `swarmed results export`, save
it as `tmp/ptbxl_ecg/FL_global_model.pt`, then, from the repository root:

```bash
python examples/ecg_ptbxl/evaluate.py --checkpoint tmp/ptbxl_ecg/FL_global_model.pt \
  --data tmp/ptbxl_ecg/local-test --out tmp/ptbxl_ecg/result.json   # all sites + pooled
python examples/ecg_ptbxl/evaluate.py --checkpoint tmp/ptbxl_ecg/FL_global_model.pt \
  --data tmp/ptbxl_ecg/site-2                                        # one site
```

Use the final `FL_global_model.pt`; `best_FL_global_model.pt` is selected by the validation
accuracy the sites report, not by AUROC. `evaluate.py` imports the model and the normalisation
from `training.py` next to it.

## What was run

| Where | Date | Sites | Rounds | Result (test split of each site) | Run time | Environment |
|---|---|---|---|---|---|---|
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 4 (PTB-XL recording sites 0, 1, 2, other) | 5 x 1 local epoch | see below | 15 s job build + 94 s simulator on 4 clients | Host virtual environment: Python 3.12.13, torch 2.10.0, numpy 2.4.3, scikit-learn 1.8.0, pandas 2.3.3, NVFlare 2.7.1; CPU |
| NVIDIA FLARE simulator, job built by SwarmMed | 11 Nov 2026 | 4 (PTB-XL recording sites 0, 1, 2, other) | 5 x 1 local epoch | pooled AUROC 0.922 (0.910-0.932); per site 0.874, 0.936, 0.912, 0.947 | 9 s job build + 66 s simulator on 4 clients | Runtime image `swarmcloud-app:latest` (torch 2.10, numpy 2.4, scikit-learn 1.8, NVFlare 2.7.1), container without network, CPU |
| SwarmMed local-test network on one host, CPU | 11 Nov 2026 | 4 (as above) | 5 x 1 local epoch | pooled AUROC 0.922 (0.910-0.932); per site 0.874, 0.936, 0.912, 0.947, as in the runtime-image simulator run | 11 s network start + 39 s job | Fresh installation of SwarmMed following its README, in an isolated Ubuntu 24.04 container with its own Docker Engine; runtime image built by the Hub for the network (python:3.12-slim with the platform's packages and this template's `requirements.txt`, torch 2.10.0, NVFlare 2.7.1); CPU |
| SwarmMed at three hospitals and a coordinating host, each on its own institutional network, joined by a VPN | 16 Nov 2026, three runs | 3 (PTB-XL recording sites 0, 1, 2, one per hospital; `fl-client-4` not trained on) | 20 x 1 local epoch; seeds 0, 1, 2 | pooled AUROC 0.928 (SD 0.005; runs 0.922, 0.930, 0.932) over the 3 training shares; per site 0.892, 0.949, 0.920 (means); 0.954 on `fl-client-4` (mean) | 3 min 15 s, 3 min 4 s and 3 min 4 s per job in the Hub's record; 2 min 49 s, 2 min 50 s and 2 min 46 s from the start to the end of the swarm workflow in the FL server's log | SwarmMed 1.0.2 plus changes that 1.0.3 contains: this template and the explanations, a standby FL server with a fix for client sites, NetBird support, the network-stop fix and a kombu pin (1.0.3 also has a later fix to exporting startup packages, which these runs did not include); installed with its README's steps, two edits to `.env` at the hospitals and a Compose override in one added Ubuntu 24.04 container with its own Docker Engine on each host; client-only Hubs at the hospitals, a server-only Hub at the coordinating host; runtime image: the Hub's application image, built on each host (`swarmmed-app:latest`; the image name follows the checkout folder's name; torch 2.10.0, NVFlare 2.7.1, numpy 2.4.3, scikit-learn 1.8.0); CPU only, 4 threads per site |

SwarmMed run (11 Nov 2026, one run): the project was created from this folder with `swarmed project
create --code-dir`, the `fl-client-k` folders were imported with `swarmed data import`, and the job
was trained on a local-test network with 4 clients through the CLI steps in [Run it](#run-it). All
sites finished every round and hold identical `FL_global_model.pt` files. In the Hub's sandbox,
`validation.py` (50 checks: 49 ok, 1 warning), `visualization.py` (4 plots) and
`results_visualization.py` (4 plots) completed, and on 12 Nov 2026 `explainability.py` stored 8
cases with 4 panels each and 3 run-level figures.

Simulator run on the host (one run, one data split, one seed; no repetition, so there is no estimate of
run-to-run spread). All four sites logged a `SWARMMED_METRIC` line in each of the 5 rounds, and
the `FL_global_model.pt` files of the four sites are identical. The final model scored with
`evaluate.py` (AUROC with a 95% percentile bootstrap interval, 2,000 resamples of records, seed 0;
accuracy, sensitivity and specificity at probability 0.5; the bootstrap treats records as
independent, see Limitations):

| Site | Test records (abnormal) | AUROC (95% CI) | Accuracy | Sensitivity | Specificity |
|---|---|---|---|---|---|
| fl-client-1 | 483 (173) | 0.874 (0.837-0.908) | 0.801 | 0.474 | 0.984 |
| fl-client-2 | 598 (337) | 0.937 (0.917-0.954) | 0.816 | 0.703 | 0.962 |
| fl-client-3 | 867 (584) | 0.913 (0.894-0.930) | 0.795 | 0.718 | 0.954 |
| fl-client-4 | 210 (152) | 0.947 (0.915-0.974) | 0.814 | 0.757 | 0.966 |
| pooled | 2,158 (1,246) | 0.922 (0.911-0.932) | 0.804 | 0.685 | 0.967 |

The 0.5 threshold is not tuned; the model calls too few records abnormal (the pooled sensitivity
is 0.68, 74% to 77% of the records of each abnormal superclass), so accuracy at 0.5 understates
what the AUROC shows. The pooled AUROC mixes sites with different prevalences (36% to 72%);
compare the per-site values. The validation AUROC of the received global model went from 0.28 to
0.37 at round 0 (untrained weights) to 0.91 to 0.96 at round 4 at the four sites. `evaluate.py`
on this final model gives the same AUROC to three decimals as the plain-PyTorch check that chose the
schedule (pooled 0.921; per site 0.873, 0.939, 0.911, 0.944).

A second simulator run in the runtime image (the second row; the same job, data and seeds, the
network disabled) completed on its first attempt with identical global models at the four sites. Its
results differ from the host run in the third decimal at most (the model files are not
byte-identical across the two environments), which is the size of difference to expect from
floating-point differences; it is not an independent repetition.

The sandbox scripts were first run with a local emulation of the Hub's sandbox wrapper on the same
data and the simulator's results (it runs the host virtual environment, so package versions and
memory accounting can differ from the Hub's image). `validation.py`: 50 checks, 49 ok and 1 warning
(one record at fl-client-4 has a flat lead), 4.7 s, 0.44 GB peak memory. `visualization.py`: 4
plots, 3.3 s, 0.29 GB. `results_visualization.py`: 4 plots, 7.9 s, 0.75 GB peak memory, which is
close to the Hub's 1 GB limit because importing torch alone takes about 0.5 GB. On 11 Nov 2026,
`validation.py` (the same 50 checks: 49 ok, 1 warning) and `visualization.py` (4 plots) also ran in
a SwarmMedHub sandbox on the same data. On the local-test SwarmMed run (11 and 12 Nov 2026),
`results_visualization.py` and `explainability.py` also ran in the Hub's sandbox.
`explainability.py` is described below.

`prepare_data.py --download` was run on 11 Nov 2026 into an empty folder: the download (380.6 MB of
the official zip by range requests, 43,601 files all matching PhysioNet's `SHA256SUMS.txt`) took about
65 s, the whole run 87 s with 0.73 GB peak memory, and the output matched the reference counts and
fingerprints. Without the download the conversion takes about 20 s.

Multi-site runs (16 Nov 2026, three runs with seeds 0, 1 and 2; the last row of the table). Three
hospitals each trained on one PTB-XL share on a server of their own: hospital A on `fl-client-1`
(recording site 0), hospital B on `fl-client-2` (recording site 1) and hospital C on `fl-client-3`
(recording site 2). The shares were the ones prepared for
[`multimodal_ptbxl`](../multimodal_ptbxl/README.md), whose `prepare_data.py` writes the same ECG
arrays as this template's (all 12 site and split files match this template's fingerprints) plus age,
sex, height and weight, which this template does not read. They were copied to the hospitals, so the
records are public PTB-XL data, not data of the hospitals' own patients; each hospital imported only
its own share, at the root of its data, and no hospital held `fl-client-4`. The hospitals and a
separate coordinating host were each on their own institutional network, joined by a VPN between the
institutions. At each hospital, a development version of SwarmMed (1.0.2 plus changes now in 1.0.3)
was copied to an existing server and installed with its README's steps, two edits to `.env` (the
project's path on the host and the Hub's host name) and a Compose override (client-only mode, GPU
off, the runtime image, non-default FL ports and a Celery worker running as root; see the notes
below), in one added Ubuntu 24.04 container (privileged, with its own Docker Engine, limited to 16
CPUs and 28 GiB of memory). Apart from that container, its base image and scratch folders, which
were removed after the runs except for the image, the servers were left as they were. The
installations were set up and operated remotely over SSH by the SwarmMed team, not by the hospitals'
staff. The hospitals' Hubs ran in client-only mode (`SWARMMEDHUB_CLIENT_ONLY_MODE`,
`SWARMMEDHUB_LOCAL_PARTICIPANT`); the coordinating host ran a server-only Hub
(`SWARMMEDHUB_SERVER_ONLY_MODE`), also in a container with its own Docker Engine, with the FL
server's ports published on the host's VPN address. The network was created with the CLI at the
coordinating Hub (`--server-ip`, one `--participant` per hospital, `--no-local-client`), and each
hospital imported the startup bundle into its own Hub. The FL clients dialled out to the FL server's
fed-learn port and the hospitals' Hubs to its admin port; training needed no inbound connections at
the hospitals (only the Hubs' optional status exchange on port 5085 would have; see the notes
below). Messages between the hospitals, model weights included, were relayed through the FL server,
as SwarmMed configures it by default (see
[Threat model](../../docs/threat-model.md#3-the-coordinating-host-sees-model-updates-default-configuration)),
so the coordinating host could observe the updates and the runs depended on it throughout.

The first job ran this template's code (seed 0) for 20 rounds of 1 local epoch
(`SWARMMEDHUB_SWARM_ROUNDS=20` in the environment of the `training start` command; the job's
`training.py` was identical at the three hospitals and differs from the template only in the project
ID that the Hub writes into it), in NVIDIA FLARE's client-controlled swarm workflow, on CPUs only
(`SWARMMEDHUB_ENABLE_GPU=false`, 4 threads per site). Every round aggregated the updates of all
three hospitals. The job completed at its first attempt (the deployment had been brought up on 13
Nov 2026, see below), at about 8 s per round. Afterwards the three hospitals held byte-identical
final models (`FL_global_model.pt`) and byte-identical best models (`best_FL_global_model.pt`,
selected by the highest mean of the three hospitals' validation accuracies, 0.858 at round 15). Each
hospital scored the final model on its own test split with `evaluate.py` (settings as above). The
coordinating host scored the same model on the public shares, with the same values to four decimals,
and also computed the pooled row, which needs all three hospitals' shares, and the `fl-client-4`
row, from a share that no hospital held:

| Hospital | Share (PTB-XL recording site) | Test records (abnormal) | AUROC (95% CI) | Accuracy | Sensitivity | Specificity |
|---|---|---|---|---|---|---|
| A | `fl-client-1` (0) | 483 (173) | 0.880 (0.844-0.913) | 0.816 | 0.509 | 0.987 |
| B | `fl-client-2` (1) | 598 (337) | 0.943 (0.925-0.960) | 0.824 | 0.727 | 0.950 |
| C | `fl-client-3` (2) | 867 (584) | 0.917 (0.901-0.935) | 0.796 | 0.716 | 0.961 |
| pooled, A to C | `fl-client-1` to `fl-client-3` | 1,948 (1,094) | 0.922 (0.911-0.934) | 0.810 | 0.686 | 0.967 |
| none, not trained on | `fl-client-4` (every other site) | 210 (152) | 0.946 (0.912-0.973) | 0.843 | 0.803 | 0.948 |

`fl-client-4` holds the test records of PTB-XL's other recording sites and of records without a
site. No hospital trained on it, but it comes from the same dataset, labelling and device
manufacturer as the training shares, so it is not an external validation. The pooled row covers
three shares (1,948 records) and is not comparable with the four-share pooled values above. The
untuned 0.5 threshold again gives a low sensitivity, lowest at hospital A (`fl-client-1`; see
[Limitations](#limitations)).

Two more jobs ran later on 16 Nov 2026 on the same network and data, with the template's `SEED` set
to 1 and 2: the only change, made in the coordinating Hub's project with
`swarmed project update --code-dir`. For these jobs the hospitals' installations, removed after the
first job, were set up again in the same way, with the subnet and worker settings of the notes below
applied from the start; the coordinating host's container, also removed, was recreated from its kept
data, with the same network and project. Both jobs completed at their first attempt; every round
again aggregated the updates of all three hospitals, and the three hospitals again held
byte-identical final models. The final models' test AUROCs, computed as above:

| Test set | Seed 0 | Seed 1 | Seed 2 | Mean (SD) |
|---|---|---|---|---|
| hospital A, `fl-client-1` | 0.880 | 0.893 | 0.903 | 0.892 (0.012) |
| hospital B, `fl-client-2` | 0.943 | 0.949 | 0.954 | 0.949 (0.005) |
| hospital C, `fl-client-3` | 0.917 | 0.921 | 0.921 | 0.920 (0.002) |
| pooled, A to C | 0.922 | 0.930 | 0.932 | 0.928 (0.005) |
| `fl-client-4`, not trained on | 0.946 | 0.956 | 0.961 | 0.954 (0.008) |

Sensitivity at the 0.5 threshold was 0.547 (SD 0.038) at hospital A, 0.762 (0.034) at B, 0.756
(0.035) at C and 0.725 (0.035) pooled; mean specificity was 0.94 to 0.98 (0.93 to 0.99 in single
runs). Three runs give a rough idea of the spread between seeds; they are not a test of any
difference.

Compared with the runs on one host, the sites were servers at three hospitals plus a coordinating
host, not containers or simulated clients on one host; three shares trained instead of four; the job
ran 20 rounds instead of 5; and the runtime image was the Hub's application image, as in the
runtime-image simulator run, not one built for the network. The first run on this deployment, on 13
Nov 2026, brought it up: it completed after four failed jobs, most of the issues in the notes below
were found while bringing it up, and it ran 5 rounds with the same hospitals and training code. Its
final model gave per-site AUROCs of 0.874, 0.936 and 0.912, pooled 0.918 (0.906-0.929), and 0.946
(0.913-0.973) on `fl-client-4`. The per-site AUROCs of the first 20-round run (seed 0, as on 13 Nov)
are slightly higher (by less than 0.01), inside the bootstrap intervals; with one 5-round run, this
is no evidence of a difference.

Training on this deployment did not reproduce bit for bit. The 13 Nov 2026 run and the first 16 Nov
run (seed 0) had the same data, seed and aggregating hospitals in their first five rounds; their
validation metrics agree to four decimals in rounds 0 and 1 and then differ slightly (AUROC by up to
0.0004, accuracy by up to two records at a hospital). In round 0 the updates reached the aggregating
hospital in a different order in these two runs, and the aggregator adds them in the order they
arrive, which is consistent with differences from floating-point summation.

Explanations: the hospitals' Hubs did not list the job (see the notes below), so `explainability.py`
ran at each hospital with `python -m swarmmed_explain.run_local` (as in
[Explainability](#explainability)), outside the Hub's sandbox, on the hospital's own test split and
the final model of the first 20-round run (seed 0, 16 Nov 2026). Each bundle held 8 cases, 32 panels
and 3 run-level figures. The three bundles were imported into the coordinating Hub with
`swarmed results explain-import`, which checked them like sandbox output and stored them as three
explanation runs of that job, for review there. These records are public; with patients' data,
sharing explanations with another site needs its own legal basis
([Explainability](../../docs/explainability.md#privacy)).

Deployment notes from these runs:

- **Docker's subnets and the VPN:** SwarmMed creates a Docker bridge network for each network
  without naming a subnet, so Docker takes one from its default address pools. At hospital A, and at
  the coordinating host for the second network created there, that subnet overlapped the VPN's
  address range, and traffic to VPN addresses was routed into the bridge; hospitals B and C had
  their pools moved before their clients first started. Here every Hub's Docker Engine ran inside
  the added container; an engine on the host itself was not tried. Before the first network starts,
  set Docker's `default-address-pools` to a range that no site uses, or create the bridge
  (`swarm_<first 12 characters of the network ID>_net`) beforehand with a free subnet; SwarmMed uses
  an existing bridge of that name.
- **The coordinating host's own address:** the FL server's address was the coordinating host's VPN
  address, which the Hub does not detect by itself. It was listed in `SWARMMEDHUB_LOCAL_ADDRESSES`
  (for `app` and `celery_worker`) so that the Hub recognises itself as the server. This setting, and
  the CLI's warning about this case when a network is created, come with the NetBird support in
  1.0.3.
- **Results with server-only and client-only Hubs:** the coordinating Hub had the job's record but
  no model, and the hospitals' Hubs had the model but did not list the job, so
  `swarmed results sync` had nothing to collect on either side. Score and explain the model at each
  site, as above, and import the explanations into the coordinating Hub where sharing them is
  allowed.
- **Connectivity:** judge it by the FL server's log (`Total clients`), not by the participant status
  that `swarmed network show` reports, which, at the coordinating Hub, listed every participant, the
  FL server included, as offline, with no last-seen time. That status comes from status messages
  between the Hubs (HTTPS on port 5085), which need inbound connections that this deployment did not
  allow: the coordinating Hub tried to send them to each hospital every 30 s, and none reached a
  Hub. The hospitals' Hubs did not find their own addresses in the participant list and sent none.
- **Restarts:** restart FL containers with `swarmed network stop` and then `swarmed network start`,
  which removes NVFlare's stale process-ID files. After a bare `docker restart`, NVFlare finds its
  old `daemon_pid.fl`, whose process ID now belongs to the container's new first process, and
  refuses to start. In 1.0.2, and in any version without the network-stop fix,
  `swarmed network stop` fails with `'function' object has no attribute 'delay'`; 1.0.3 includes the
  fix.
- **Workspace permissions:** at hospital A, starting the FL client failed with permission errors on
  the FL workspace until the Hub's Celery worker ran as root; the same change was made at hospitals
  B and C before their clients first started. Whether a standard installation is affected was not
  checked.
- **Admin identity:** all four Hubs logged in to the FL server's admin port with one identity,
  hospital A's `project_admin`, so the server's log names the same user for every Hub; only the
  connection's source address tells them apart. The startup package gives every site every identity
  in any case (see
  [Threat model](../../docs/threat-model.md#key-custody-what-the-startup-package-contains)).
- **Standby FL server:** a defect in this feature (released in 1.0.3), which was part of the code
  run here, kept the clients from training at first: each hospital's FL workspace also held the
  server's configuration, and the FL client read it instead of its own. The 13 Nov 2026 run worked
  around it by editing the configuration files by hand; the 16 Nov runs used the fix that 1.0.3
  contains.

Sites on separate hosts were tested on 13 and 16 Nov 2026 (above). Not tested: GPU; more than four
sites on one host or more than three on separate hosts; more than three runs, or other data splits;
ECGs of the hospitals' own patients; SwarmMed installed directly on a hospital's server rather than
in an added container; installation and operation by a hospital's own staff; failover to the standby
FL server; NetBird as the overlay; the PTB-XL 500 Hz records.

## Explainability

The paragraphs below describe the cases of the final model of the simulator run on the host (11 Nov
2026, the first row of the table in [What was run](#what-was-run)) on `fl-client-1`, with their
outcomes. If you want to try the blinded review on that run, do it before you read them.

`explainability.py` explains the final global model on the test split of the first site folder
(or the root data in a real deployment). It selects the two most confident cases of each outcome
(true positive, false positive, false negative, true negative; "abnormal" is the positive class;
8 cases) and draws four panels per case. The first, the input panel, is the record itself (the
12 leads in millivolts, each minus its median, over the 10 seconds); it shows nothing from the
model. The other three are for the abnormal logit:

- **Integrated gradients** over the 12 leads: the gradient of the output integrated along a
  straight path from the all-zero record (every lead at its median) to the case, 32 steps; the
  contributions add up to the output difference from that reference, and the completeness gap is
  stored with the case. The map is drawn as colour behind each lead and smoothed over 0.25 s for
  display.
- **Occlusion** over the 12 leads: the drop of the logit when a 0.5-s window of one lead is set
  to the lead's median, stride 0.25 s.
- **Method agreement:** both maps summed over the leads per half second, scaled and overlaid, with
  the rank correlation and the overlap of the five largest half seconds, and the share of the
  attribution each lead receives.

Red marks signal that supports "abnormal", blue signal that opposes it. Three run-level figures
follow: the distribution of the model's scores on the test split by label, the agreement of the two
methods per case, and the attribution per lead averaged over the explained records.

Run it locally from the repository root, with the paths of [Run it](#run-it) (the same way
the sandbox runs it; the bundle can then be checked or imported):

```bash
python -m swarmmed_explain.run_local --script examples/ecg_ptbxl/explainability.py \
  --data tmp/ptbxl_ecg/local-test --model tmp/ptbxl_ecg/FL_global_model.pt \
  --out tmp/ptbxl_ecg/explain_bundle
```

On the final model of the simulator run on the host (11 Nov 2026), the run took 10.3 s pinned to 2
CPUs (`taskset -c 0,1`) with 0.84 GB peak memory, inside the sandbox limits of 2 CPUs, 2 GB and 15
minutes, and wrote a valid bundle of 8 cases, 32 panels and 3 summaries. On the fl-client-1 test
split the maps are not zero or uniform: they concentrate on individual beats and, in the first true
positive, on one wide beat of different shape at about 2.8 s that both methods mark in the same
leads. The rank correlation between the two methods over the 8 cases was between 0.24 and 0.89
(median 0.68), and the overlap of their five largest half seconds between 1 of 5 and 4 of 5. The
methods therefore agree on the main regions but not on the fine ranking. These are properties of one
small model on eight records; they are not clinical findings, and the maps were not compared with a
clinician's reading.

Maps support review. They show what this model does with the signal; they are not causal, they
depend on the reference (the median-lead record), occlusion windows of different leads overlap in
their effect, and a plausible-looking map does not justify a prediction or show that the model is
correct. In SwarmMedHub a reviewer can agree, disagree or note each case. In blinded mode, a
case the reviewer has not read yet shows only its case ID, its label and the input panel, and the
Hub shows the cases in a random order, so a case's position says nothing about its outcome.

## Limitations

- One data split. One seed and one training run per environment, except the three 20-round runs at
  the hospitals (seeds 0 to 2; a 5-round bring-up run on 13 Nov 2026 came before them), which are
  too few to estimate the spread between runs reliably; no external validation. The PTB-XL test fold
  comes from the same hospitals and devices as the training folds.
- **Label provenance differs at fl-client-1.** Only 28.6% of its training records were validated by
  a human; the other 71.4% were not (their label is the initial automatic report), while its
  validation and test records are 100% human-validated. Abnormal prevalence there drops from 58% in
  training to 35% to 36% in validation and test, and its test AUROC (0.874 in the simulator run;
  0.892 on average over the three runs at the hospitals) and sensitivity (0.47 and 0.55 at threshold
  0.5) are the lowest. At the other sites at least 99.8% of the records in every split are
  human-validated. Part of the difference between sites reflects this, not only the model.
- Sites differ in recording devices and population (fl-client-1 is mostly CS100 and CS-12
  devices, fl-client-2 the AT-6 family only), so part of the site differences reflects data
  collection. `fl-client-4` pools every other recording site and is the smallest (1,037 training
  records).
- The label is a coarse summary (any abnormal superclass), taken from the dataset's SCP
  statements without their likelihoods; a record can carry several superclasses. 411 records
  without a diagnostic superclass are excluded.
- 100 Hz records only; no filtering, no augmentation, no hyperparameter search. This is a small
  network chosen to run on CPUs, not a model for clinical use. Scores are not calibrated, and the
  0.5 threshold gives a low sensitivity.
- The bootstrap intervals resample records, not patients. The four test splits hold 483, 598,
  867 and 210 records from 417, 563, 704 and 198 patients, so records of one patient are treated
  as independent and the intervals are somewhat too narrow.
- Explanation maps are not causal and are checked here only for consistency across two methods,
  not for clinical validity.
- Case selection ranks records, not patients, and the prepared files hold no patient number, so
  two explained cases can be records of the same patient.
- Not a federated-privacy claim: swarm learning keeps records at the sites, but the shared model
  weights can leak information about training data; no privacy analysis was done.
