# DUKE MST CLI Experiment

The three-site breast-MRI example of the paper: MediSwarm's MST model trained as a
swarm on an IID three-site split of the Duke cohort and evaluated on the Duke test
split and the ODELIA test splits. The full recipe, with the exact image, settings,
commands and what cannot be reproduced from public artefacts, is in
[`docs/reproduce.md`](../../docs/reproduce.md). This page is the short version.

Create the project from this directory on the coordinating Hub. On a Hub host the
`swarmed` CLI runs inside the app container, where the repository is mounted at
`/app`:

```bash
docker exec -w /app swarmmedhub python3 -m swarmed_cli.main project create \
  --user <hub-user> \
  --title "DUKE MST IID 3-site CLI" \
  --description "MST DUKE swarm evaluation via swarmed CLI" \
  --code-dir ./experiments/duke_mst_cli
```

`training.py` uses NVFlare's PyTorch Lightning client API and imports the
MediSwarm MST/data stack from the runtime image, so the clients must run the
ODELIA/MediSwarm image. The paper's runs used
`jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6`:

```bash
SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia@sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6
SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true
```

Client-local environment at each training site (values for node_A):

```bash
SITE_NAME=node_A
INSTITUTION=node_A
DATA_DIR=/mnt/data/DUKE_iid
SCRATCH_DIR=/mnt/scratch/duke_mst/node_A
MODEL_NAME=MST
CONFIG=unilateral
EPOCHS_PER_ROUND=5
EPOCHS_MAX_CAP=10
SWARMMEDHUB_MST_EXPORT_PREDICTIONS=false
```

These variables must reach the process that starts the client. `swarmed network
start` and the web interface hand the start to the Hub's Celery worker, which does
not see variables given on the command line; `docs/reproduce.md` shows how the
recorded runs started the server and the clients. The CLI runtime also accepts the
older MediSwarm aliases `DATADIR` and `SCRATCHDIR`; they are normalized to
`DATA_DIR` and `SCRATCH_DIR`.

`SWARM_ROUNDS = 20` is the default full run. For a 2-round wiring smoke test, set
the variable in the process that submits the job:

```bash
docker exec -w /app -e SWARMMEDHUB_SWARM_ROUNDS=2 swarmmedhub \
  python3 -m swarmed_cli.main training start --user <hub-user> --network <NETWORK_UUID>
```

The POC evaluates the NVFlare `FL_global_model.pt` checkpoint. Per-round
prediction CSV export is disabled by default because it is much slower than the
training loop on Duke MRI volumes. Set
`SWARMMEDHUB_MST_EXPORT_PREDICTIONS=true` if those intermediate CSVs are needed.

Evaluate only the NVFlare global model artifact (inside the runtime image, which
provides the MediSwarm data stack):

```bash
python experiments/duke_mst_cli/evaluate_global_model.py \
  --checkpoint /workspace/<JOB_ID>/app_<site>/FL_global_model.pt \
  --data-root /mnt/data/DUKE_iid \
  --institution test \
  --output-json ./results/duke_mst_cli/eval.json
```

Per-case predictions for the statistics come from `export_global_predictions.py`,
the statistics from `build_delong_report.py`, `build_results_summary.py` and
`build_nodec_analysis.py`, and the negative control's site copies from
`make_scrambled_labels.py`; see `docs/reproduce.md`.
