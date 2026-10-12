---
title: Reproducing the worked example
description: What was run for the three-site breast-MRI swarm on the Duke cohort, with the software, data layout, commands, evaluation and statistics, and what public artefacts cannot reproduce.
---

# Reproducing the worked example

The worked example trains a breast-MRI classifier as a swarm of three sites,
node_A, node_B and node_C, each holding a disjoint share of the Duke breast-MRI
cohort. The final model is scored on the Duke test split and on the test splits
of six ODELIA cohorts: CAM, MHA, RUMC, UKA and UMCU, pooled as the external test
set, and RSH, reported on its own.

This page records what was run, so that a reviewer can check it and repeat it.
User names, addresses and paths in the commands are examples.

## What was run

Every run below trained for 20 rounds, with the FL server on a coordinating host
that did not train, and one GPU at each training site.

| Run | Name in the statistics | Date | NVFlare job | `FL_global_model.pt` MD5 |
|---|---|---|---|---|
| Repetition 1 | `swarm_repetition_1` | 28 Apr 2026 | `1a581be8-069f-45d5-9e87-80b78c7e285a` | `7d24179145a3a8cb43e2edf0d065f7cc` |
| Repetition 2 | `swarm_repetition_2` | 28 Apr 2026 | `37bc58d0-9425-4180-9846-7273cd55d2bf` | `172f8bcf98f1fb6199261be8bbdfc9a3` |
| Web-interface run | `hub_poc_run1` | 4 May 2026 | `58179610-4c7f-4ce5-87fd-a6077333bdab` | `599f69ba046074ecf8fda56506679e65` |
| Repetition 3 | `swarm_repetition_3` | 15 Sep 2026 | `f6812656-cafa-4d59-a2a9-4584eef668f3` | `5f4b88e4be79e44833a71759f710c114` |
| Ablation, node_A and node_B only | `swarm_ablation_no_nodeC` | 22 Sep 2026 | `d67d860d-3e48-407a-adb5-0b154c6d1ed6` | `3f8295a4cd2f5de02722cdca72996742` |
| Negative control, permuted labels | `swarm_scrambled_label_control` | 8 Oct 2026 | `2b7fca67-62da-4696-9e7b-8ce9a8d88c69` | `817b31a4583214b59ed8f273fb46421c` |

* **Hosts.** Hosts 1 and 2 each have a Quadro RTX 6000 (24 GB), host 3 a Quadro
  RTX 8000 (48 GB); see [Hardware](hardware.md). Repetitions 1 and 2 and the
  web-interface run placed node_A, node_B and node_C on hosts 1, 2 and 3. The
  later runs swapped node_A and node_C (hosts 3, 2 and 1).
* **Runtime image.** Repetition 3 and the negative control were started with the
  image under [Software](#software), pinned by digest. For repetitions 1 and 2
  the image was not recorded at the time. It was identified afterwards, from
  build dates, as the same build (`1.4.3-dev.260427.ab6397b`, pushed to Docker
  Hub on 27 April 2026); the build before it, `1.4.3-dev.260424.66c7b48`
  (`sha256:f1b49a0c6e2c913ee5180e7a9d5af3e981865f0ec413695c727925ead9ddc9ab`),
  cannot be ruled out. The image of the web-interface run and of the ablation
  was not recorded.
* **Code.** Repetition 3, the web-interface run, the ablation and the negative
  control ran this repository's `experiments/duke_mst_cli/training.py`; only the
  project id, which the Hub writes into the script, differs. The copy kept for
  repetition 2 differs in one respect: it always wrote per-round prediction CSVs.
  The coordinating host's copy for repetition 1 was overwritten. Repetitions 2
  and 3, the web-interface run and the negative control generated a
  byte-identical client job configuration; the ablation's differs only in
  waiting for two sites instead of three.
* **SwarmMed.** The coordinating host ran commit `5b0b8173` for repetition 3.
  The training hosts ran an April 2026 copy of SwarmMed that is not a git
  checkout.
* **Checks.** In repetition 3, the ablation and the negative control the final
  model had the same MD5 on every site. Repetition 3 took 3 h 01 min for its 20
  rounds, according to the FL server log.

## Software

| Component | Version |
|---|---|
| Runtime image (FL server and clients) | `jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6`, tag `1.4.3-dev.260427.ab6397b`, linux/amd64; tagged locally as `odelia:paper-apr2026` |
| NVFlare in the runtime image | `nvflare_mediswarm 2.7.2.dev260427`, MediSwarm's fork of NVFlare: branch `MediSwarm-2.7.2` at commit `17f28b28eb9a80381251c3a325f7dc51f95d52c6` (15 April 2026), built into the image on 27 April 2026. `nvflare.__version__` reports `0+unknown`. |
| Training stack in the runtime image | Python 3.10.14, PyTorch 2.2.2 with CUDA 12.1, PyTorch Lightning 2.4.0; MediSwarm's model and data code in `/MediSwarm/application/jobs/_shared/custom` |
| SwarmMed (Hub and `swarmed` CLI) | this repository; it pins `nvflare==2.7.1`, which the Hub uses to provision the startup kits, build the job and submit it |
| Scoring image | `jefftud/odelia@sha256:a58e982e4f5abeb7731816a96a6c553eb97097e4fa626db5175a4a72b81df21c`, tag `1.0.2`; recorded for the three repetitions, the ablation and the negative control |

Both images were listed on Docker Hub under these digests on 9 October 2026.

The MST model loads its backbone, DINOv2 ViT-S/14, through `torch.hub` when it is
built. The client logs of the negative control show each client container
downloading the code from the `main` branch of
`github.com/facebookresearch/dinov2` and the pretrained weights from
`dl.fbaipublicfiles.com` at the start of training. Training hosts therefore need
outbound internet access, and this code is not pinned by the image digest.

## Data at each site

SwarmMed mounts `DATA_DIR` read-only into the client container at `/data`, and
MediSwarm's loader reads the site's folder there:

```text
<DATA_DIR>/                      e.g. /mnt/data/DUKE_iid
└── node_A/                      the site name (INSTITUTION, or SITE_NAME)
    ├── data_unilateral/
    │   ├── Breast_MRI_001_left/Sub_1.nii.gz
    │   ├── Breast_MRI_001_right/Sub_1.nii.gz
    │   └── ...
    └── metadata_unilateral/
        ├── annotation.csv       UID, PatientID, Lesion, ...
        └── split.csv            UID, Split, Fold
```

* Each sample is one breast. The UID is `<patient>_left` or `<patient>_right`,
  the loader reads only `Sub_1.nii.gz` from its folder, and the model makes one
  prediction per breast. The statistics scripts call these samples exams.
* `Lesion` is 0 (no lesion), 1 (benign) or 2 (malignant). The model has three
  outputs; scoring takes class 2 as positive and classes 0 and 1 as negative. The
  Duke folders contain only classes 0 and 2.
* Only rows with `Fold` 0 are used. A site's `split.csv` has `train` and `val`
  rows. The Duke test split is a fourth folder, `test`, with `test` rows, used
  only for scoring. The ODELIA cohorts have the same layout, one folder per
  cohort.

The partition is by patient and balanced by label. No patient appears in two of
the four folders, or in both the training and the validation split of a site,
and the malignant fraction lies between 0.519 and 0.538 in every split:

| Folder | Patients (train / val) | Breasts (train / val) | Malignant fraction (train / val) |
|---|---|---|---|
| `node_A` | 260 (208 / 52) | 520 (416 / 104) | 0.522 / 0.519 |
| `node_B` | 195 (156 / 39) | 390 (312 / 78) | 0.519 / 0.526 |
| `node_C` | 65 (52 / 13) | 130 (104 / 26) | 0.519 / 0.538 |
| `test` | 131 | 262 | 0.523 |
| Total | 651 | 1,302 | |

## Training code and settings

`experiments/duke_mst_cli/` is the project's code directory. `training.py` is a
thin NVFlare client: it uses NVFlare's PyTorch Lightning client API and takes
the data pipeline, model and trainer from MediSwarm's `threedcnn_ptl` module,
which comes with the runtime image; the script cannot run without it. In every
round it validates the global model it receives and then trains it locally.
`requirements.txt` lists the job's packages; with the configured image forced
(below), nothing is installed from it.

The model and training, as defined by MediSwarm's code in the image: MST, a
DINOv2 ViT-S/14 encoder applied to each slice followed by a transformer across
slices, with three outputs; cross-entropy weighted by the inverse class
frequencies of the site's training split; AdamW with a learning rate of 1e-6;
batch size 1 with gradients accumulated over 8 batches; 16-bit mixed precision.
Each volume is cropped or padded to 224 × 224 × 32 voxels and
intensity-normalised; training adds a random flip or rotation and random noise.

Settings at each training host:

| Variable | Value | Effect |
|---|---|---|
| `SITE_NAME`, `INSTITUTION` | `node_A`, `node_B` or `node_C` | the site folder read under `DATA_DIR`; required |
| `DATA_DIR` | host directory that holds the site folder | mounted read-only at `/data` in the client container; required |
| `SCRATCH_DIR` | writable host directory | the site's Lightning logs and local checkpoints; required |
| `MODEL_NAME` | `MST` | default in `training.py` |
| `CONFIG` | `unilateral` | one sample per breast; default |
| `EPOCHS_PER_ROUND` | `5` | base of the epoch rule below; default |
| `EPOCHS_MAX_CAP` | `10` | cap of the epoch rule; default |
| `SWARMMEDHUB_MST_EXPORT_PREDICTIONS` | unset (off) | per-round prediction CSVs, much slower than training on these volumes |

`SWARM_ROUNDS = 20` is a constant in `training.py`. The Hub reads it when it
builds the job; `SWARMMEDHUB_SWARM_ROUNDS` in the environment of the process
that submits the job overrides it.

MediSwarm sets each site's local epochs per round from the size of its training
split: round(`EPOCHS_PER_ROUND` × 500 / n), clamped to between 1 and
`EPOCHS_MAX_CAP`, where n is the number of training samples and 500 is the
default of `EPOCHS_REFERENCE_DATASET_SIZE`. For node_A, node_B and node_C (416,
312 and 104 training samples) this gives 6.0, 8.0 and 24.0, so 6, 8 and 10
epochs per round, as the clients log. Over 20 rounds that is 120, 160 and 200
local epochs.

Platform settings:

| Variable | Set on | Value | Effect |
|---|---|---|---|
| `SWARMMEDHUB_FLARE_IMAGE` | every host | `odelia:paper-apr2026` | image of the server and client containers |
| `SWARMMEDHUB_FORCE_CONFIGURED_IMAGE` | every host | `true` | use that image; without it SwarmMed ignores the image whenever the job's runtime requirements list packages beyond its minimal set, as here, and builds its own |
| `SWARMMEDHUB_SERVER_ONLY_MODE` | coordinating host | `true` | start only the FL server from the startup package |
| `SWARMMEDHUB_CLIENT_ONLY_MODE` | training hosts | `true` | start only a client, never a copy of the server |
| `SWARMMEDHUB_LOCAL_PARTICIPANT` | training hosts | `node_A`, `node_B` or `node_C` | which site's client to start |
| `SWARMMEDHUB_FORCE_GPU` | training hosts | `true` | give the client container the GPUs even though `docker info` lists no runtime named `nvidia`, as on the recorded hosts; otherwise the client starts on the CPU and MediSwarm's model stops with "This example requires a GPU" |

The FL server listens on 8002 (fed-learn) and 8003 (admin) by default, as in
repetitions 1 and 2 and the web-interface run. Repetition 3 used 8012/8013, and
the ablation and the negative control 8014/8015, because by then another FLARE
deployment held the defaults on the coordinating host. The ports are written into
the kits, so `SWARMMEDHUB_FED_LEARN_PORT` and `SWARMMEDHUB_ADMIN_PORT` must be
set before the network is created; see [Networking](networking.md).

## Running it

Four hosts on a Tailscale overlay: the coordinating host runs SwarmMedHub and the
FL server; each training host holds one site's data and runs that site's client,
which connects out to the server's fed-learn port (see
[Networking](networking.md)). On a host that runs SwarmMedHub, run the `swarmed`
CLI inside the app container, as below; the host-side `uv run swarmed` in the
experiment's README cannot reach the database of a standard installation.

### 1. Prepare every host

```bash
docker pull jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6
docker tag jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6 \
  odelia:paper-apr2026
```

The coordinating host runs a SwarmMedHub installation
([Installation](installation.md)). Each training host needs Docker with NVIDIA
GPU support, its site folder under `DATA_DIR`, and SwarmMed to import the startup
package and start its client.

### 2. Create the project and the network (coordinating host)

```bash
SWARMED="docker exec -w /app swarmmedhub python3 -m swarmed_cli.main"
$SWARMED project create --user <hub-user> --title "DUKE MST IID 3-site" \
  --code-dir ./experiments/duke_mst_cli --json
$SWARMED network create --user <hub-user> --project <PROJECT> --name "DUKE IID 3-site" \
  --server-ip 100.64.0.1 \
  --participant node_A=100.64.0.11 --participant node_B=100.64.0.12 \
  --participant node_C=100.64.0.13 \
  --no-local-client --json
$SWARMED network export-package --user <hub-user> <NETWORK> --out tmp/startup.zip
```

The app container mounts the repository at `/app`, so `--code-dir` and `--out`
are paths in the repository. `--no-local-client` keeps the coordinating host
from being added as a fourth training site. NVFlare rejects underscores in
participant names, so `node_A` becomes `node-A` in the kits and the job;
`SITE_NAME` keeps the underscore. The package holds every site's kit, and each
kit also contains the FL server's private key and an admin key, so send it to
the sites over a secure channel (see [Threat model](threat-model.md)).

### 3. Import the startup package (each training host)

```bash
# SWARMED defined as in step 2
$SWARMED network import --user <site-user> --project <SITE_PROJECT> \
  --name "DUKE IID 3-site" --package tmp/startup.zip --json
```

The imported network keeps the coordinator's network ID. The recorded training
hosts ran SwarmMed outside containers against their own database
(`.venv/bin/swarmed ...` with `DB_HOST=127.0.0.1`) and imported into an existing
project.

### 4. Start the FL server (coordinating host)

`swarmed network start` and the web interface hand the start to the Hub's Celery
worker, which does not see variables given on the command line. The recorded
runs called the start task directly instead, in a process that had the
variables:

```bash
docker exec -w /app \
  -e SWARMMEDHUB_SERVER_ONLY_MODE=true \
  -e SWARMMEDHUB_FLARE_IMAGE=odelia:paper-apr2026 \
  -e SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true \
  swarmmedhub python3 manage.py shell -c \
  "from network.tasks import start_swarm_network_task; start_swarm_network_task('<NETWORK>', <USER_ID>)"
```

`<USER_ID>` is the numeric id of the Hub user. Putting the variables into the
Celery worker's environment instead, and starting from the CLI or the web
interface, is described in [Networking](networking.md); the recorded runs did
not use that route.

### 5. Start the clients (each training host)

The recorded form, run from the SwarmMed directory, with the values for node_A:

```bash
export SWARMMEDHUB_CLIENT_ONLY_MODE=true SWARMMEDHUB_LOCAL_PARTICIPANT=node_A
export SWARMMEDHUB_FLARE_IMAGE=odelia:paper-apr2026 SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true
export SWARMMEDHUB_FORCE_GPU=true
export SITE_NAME=node_A INSTITUTION=node_A
export DATA_DIR=/mnt/data/DUKE_iid SCRATCH_DIR=/mnt/scratch/duke_mst/node_A
export MODEL_NAME=MST CONFIG=unilateral EPOCHS_PER_ROUND=5 EPOCHS_MAX_CAP=10
# Specific to the recorded hosts, which ran SwarmMed outside containers:
export DB_HOST=127.0.0.1 DB_PORT=5432 HOST_PROJECT_PATH=/path/to/SwarmMed
.venv/bin/python manage.py shell -c \
  "from network.tasks import start_swarm_network_task; start_swarm_network_task('<NETWORK>', <USER_ID>)"
```

The client container mounts `DATA_DIR` read-only and `SCRATCH_DIR` read-write;
the site's data is not imported into SwarmMed.

### 6. Train (coordinating host)

```bash
docker exec -w /app -e SWARMMEDHUB_SWARM_ROUNDS=2 swarmmedhub python3 -m swarmed_cli.main \
  training start --user <hub-user> --network <NETWORK> --json        # 2-round smoke test
$SWARMED training start --user <hub-user> --network <NETWORK> --json     # 20 rounds
$SWARMED training watch --user <hub-user> <JOB> --network <NETWORK> --interval 60
```

`training start` builds and submits the job in the CLI process, so
`SWARMMEDHUB_SWARM_ROUNDS` applies; the smoke test before repetition 3 took 42
minutes. The Hub submits only while its own record of the network is running,
which is why the server is started from the Hub that submits, and it refuses
when the project's stored code has no `training.py`, for example while object
storage is down.

The job uses NVFlare's client-controlled swarm workflow (`SwarmServerController`,
`SwarmClientController`). All three sites train and aggregate, each round waits
for all three (`min_responses_required: 3`), and `private_p2p` is off. Updates
are averaged with `InTimeAccumulateWeightedAggregator` over full weights, and
models are stored by `PTFileModelPersistor`, chosen because `training.py`
imports `torch`. The job also configures `SimpleIntimeModelSelector` for a metric
named `accuracy`, which never reaches it (the client logs show
`global best metric is None`), so no best model is selected.

### 7. Collect the model

Each training host ends with the global model after the last round, about 94 MB:

```text
workspaces/<project>/<network>/workspace/<project_name>/prod_00/node-A/<NVFlare job>/app_node-A/FL_global_model.pt
```

`<project>` is the id of the project on that host and `<project_name>` its title
as a slug with underscores (`DUKE MST IID 3-site` becomes
`duke_mst_iid_3_site`); the other hosts have `node-B` and `node-C`. The file is
NVFlare's PyTorch persistor output, a dictionary whose `model` entry is the state
dict. Compare the MD5 across the hosts and copy one file for scoring.

## Evaluation

Each checkpoint was scored with
`experiments/duke_mst_cli/export_global_predictions.py` on the test split
(`Split` test, `Fold` 0) of each cohort only. For the three repetitions, the
ablation and the negative control the script ran in the scoring image on a GPU
host. The recorded call, condensed (paths are examples):

```bash
SCORER=jefftud/odelia@sha256:a58e982e4f5abeb7731816a96a6c553eb97097e4fa626db5175a4a72b81df21c
WORK=/mnt/data/scoring   # scripts/export_global_predictions.py, models/<name>/FL_global_model.pt
DUKE=/mnt/data/DUKE_iid ODELIA=/mnt/data/odelia
score() {  # <name> <cohort> <data root> <folder>
  docker run --rm --gpus all --entrypoint python \
    -e PYTHONPATH=/MediSwarm/application/jobs/ODELIA_ternary_classification/app/custom \
    -v "$WORK":/work -v "$WORK/torch_cache":/root/.cache/torch \
    -v "$DUKE":"$DUKE":ro -v "$ODELIA":"$ODELIA":ro \
    "$SCORER" /work/scripts/export_global_predictions.py \
      --checkpoint "/work/models/$1/FL_global_model.pt" --model-id "$1" --model-family swarm \
      --dataset "$2" --data-root "$3" --institution "$4" --split test \
      --scratch-dir /work/scratch --batch-size 1 --num-workers 2 \
      --output-csv "/work/predictions/swarm/$1/$2.csv" \
      --output-json "/work/predictions/swarm/$1/$2.json"
}
score swarm_repetition_3 DUKE_test "$DUKE" test
for c in CAM MHA RUMC UKA UMCU RSH; do score swarm_repetition_3 "$c" "$ODELIA" "$c"; done
```

`torch_cache` keeps the DINOv2 code that `torch.hub` downloads on first use. For
each cohort the script writes `<cohort>.csv`, one row per breast, and
`<cohort>.json`:

* the CSV has the `uid`, `y_true` (1 if `Lesion` is 2), `y_score` (the model's
  probability for class 2), `y_pred` (1 if the predicted class is 2), the model
  name and the checkpoint's MD5;
* the JSON has AUROC, accuracy, F1, sensitivity, specificity and the confusion
  matrix, and lists the checkpoint keys that were ignored, missing or unexpected.
  For every recorded checkpoint only the two class-weight buffers
  (`_class_weight`, `loss.weight`) were ignored, and no key was missing or
  unexpected.

`evaluate_global_model.py` in the same directory computes the same metrics
without the per-case file; the statistics use the per-case files.

| Test split | Breasts | Patients | Malignant |
|---|---|---|---|
| Duke | 262 | 131 | 137 |
| CAM | 102 | 51 | 20 |
| MHA | 40 | 20 | 11 |
| RUMC | 14 | 7 | 6 |
| UKA | 40 | 20 | 4 |
| UMCU | 64 | 32 | 5 |
| ODELIA external, the five above pooled | 260 | 130 | 46 |
| RSH | 40 | 20 | 13 |

The ODELIA test splits other than RUMC's also contain benign lesions (class 1),
which count as negative.

## Statistics

Copy each scored model's directory to
`results/duke_mst_cli/stat_tests/predictions/swarm/<name>/` in the repository,
then run the scripts from the repository root with its Python environment. They
read and write paths under `results/`, which git ignores:

```bash
.venv/bin/python experiments/duke_mst_cli/build_delong_report.py
.venv/bin/python experiments/duke_mst_cli/build_results_summary.py
.venv/bin/python experiments/duke_mst_cli/build_nodec_analysis.py
.venv/bin/python experiments/duke_mst_cli/build_scalability_report.py
```

Paths below are relative to `results/duke_mst_cli/stat_tests/` unless shown in
full.

* **`build_delong_report.py`** reads `predictions/swarm/swarm_repetition_{1,2,3}/`,
  the baseline exports in `raw/local_iid/`, and each ODELIA cohort's
  `annotation.csv` and `split.csv` in `metadata/odelia/<cohort>/`; it stops if the
  baseline exports are missing. It writes:
    * `all_predictions.csv`: every model, cohort and breast, with the external
      cohorts restricted to their test split;
    * `pairwise_delong_results.csv`: per cohort, paired DeLong tests of each
      swarm run against each baseline run and against the other swarm runs;
    * `validation_checks.json`: each swarm AUROC recomputed from its CSV,
      matching labels in every pair, and identical cases for every model per
      cohort;
    * `duke_mst_delong_comparison.xlsx`: per-model metrics, the pairwise tests,
      the model lists, the prediction sources and the checks as a workbook;
    * the normalised baseline predictions in `predictions/local/`.
* **`build_results_summary.py`** reads `all_predictions.csv` and, if present,
  `predictions/swarm/hub_poc_run1/` and
  `predictions/swarm/swarm_scrambled_label_control/`. It writes:
    * `auroc_repetition_summary.csv`: per arm and cohort, the AUROC of each run
      with mean, SD and a 95% t-interval;
    * `noninferiority_tests.csv`: for the Duke and the pooled external test
      sets, swarm minus centralised and swarm minus ensemble, as paired DeLong
      tests per run and a t-interval across runs, judged at margins of 0.03 and
      0.05;
    * `ensemble_baseline.csv`: the arms side by side, including the
      no-collaboration ensemble, the per-case mean of the three single-site
      models' scores;
    * `gui_vs_cli.csv`: the web-interface run against the spread of the three
      repetitions;
    * `scrambled_label_control.csv` (see [Negative control](#negative-control)),
      `cohort_sizes.csv`, and `results_summary.md` with these tables in readable
      form.
* **`build_nodec_analysis.py`** reads `all_predictions.csv` and
  `predictions/swarm/swarm_ablation_no_nodeC/`. It writes `nodec_analysis.md`
  and `nodec_contribution.png` and `.svg`, which compare the three-site swarm
  with the swarm without node_C, and each site's single-site model with the
  swarm model. The PNG is also in `docs/assets/results/`.
* **`build_scalability_report.py`** reads `results/scalability/scalability_raw.csv`
  and writes `scalability_summary.md`, `scalability.png` and `scalability.svg`
  next to it. It belongs to the scalability benchmark, not to the breast-MRI
  runs; the benchmark script that writes its input is not in the repository (see
  [Scalability](scalability.md)).

Re-running the four scripts on the recorded inputs on 9 October 2026 reproduced
the recorded CSV and JSON outputs and both PNG figures byte for byte.

## Negative control

The negative control repeats the swarm with labels that carry no information
about the images. For each site, a copy of its folder was made with the images
hard-linked and `split.csv` copied unchanged, and the `Lesion` values in
`annotation.csv` were randomly permuted within the training split and,
separately, within the validation split (seeds 20261007, 20261008 and 20261009
for node_A, node_B and node_C). Every split keeps its patients and its class
balance; the Duke test folder was not touched. Sites, image, `training.py` and
settings were those of repetition 3. The copies were made with
`experiments/duke_mst_cli/make_scrambled_labels.py`; run on the same site folders
with the same seeds, it reproduces the control's `annotation.csv` files byte for
byte:

```bash
python3 experiments/duke_mst_cli/make_scrambled_labels.py \
  /mnt/data/DUKE_iid/node_A /mnt/data/DUKE_iid_scrambled/node_A 20261007
```

The output folder must be on the same file system as the site folder, because
the images are hard-linked.

The control's model is scored like the swarm runs, against the true labels of
the test splits. `build_results_summary.py` then reports, per cohort and for the
pooled sets, its AUROC with a DeLong 95% interval, a test against 0.5, and
paired DeLong tests against each swarm repetition (`scrambled_label_control.csv`).

## What cannot be reproduced from public artefacts

* **The data.** The images are not distributed with SwarmMed and must be
  obtained from their providers: the Duke breast-MRI dataset from The Cancer
  Imaging Archive (TCIA), under its terms of use, and the ODELIA cohorts on
  request from the ODELIA consortium. The prepared files used here are not public
  either: the per-breast volumes, and each folder's `annotation.csv` and
  `split.csv`, which define the three-site partition and the Duke test set.
  Neither this repository nor the runtime image contains them, or the script
  that made the partition. The image does contain MediSwarm's preprocessing
  scripts for the TCIA download
  (`/MediSwarm/application/jobs/ODELIA_ternary_classification/app/scripts/preprocessing/`);
  whether the files used here were made with them is not recorded.
* **The single-site and centralised baselines.** The published node_A, node_B,
  node_C and pooled baselines, three runs each, were trained with a different
  codebase (the `odelia_breast_mri` repository) and a different model
  configuration from the swarm: a DINOv2 ViT-S backbone with registers, loaded
  from Hugging Face, instead of DINOv2 ViT-S/14 without registers; one output
  with unweighted binary cross-entropy instead of three classes with
  class-weighted cross-entropy; AdamW at a learning rate of 1e-5 instead of 1e-6;
  and their own training and scoring scripts. Only their exported per-case
  predictions enter the statistics, and those exports are not public. The
  checkpoints of runs 2 and 3 of every baseline arm are no longer available.
  Comparisons of the swarm with these baselines, or with the ensemble built from
  them, therefore also compare two model configurations; comparisons among swarm
  runs (repetitions, ablation, web interface against CLI, negative control) do
  not.
* **The run records and helper scripts.** `results/` and `workspaces/` are
  ignored by git, so the checkpoints, per-case predictions, logs and provenance
  files referred to on this page are not in the repository. Neither are the
  scripts that started the clients and called the scorer; what they did is
  described above.
* **The backbone code.** DINOv2 is fetched at run time from a branch that can
  change (see [Software](#software)).
* **Bit-identical models.** Neither `training.py` nor MediSwarm's training code
  sets a global random seed; MediSwarm's data module only fixes its data
  loaders' generator, at seed 0. Training also runs in 16-bit mixed precision on
  GPUs, and the three repetitions ended with three different models. Judge a new
  run against the spread of the repetitions, not against any one of them.
