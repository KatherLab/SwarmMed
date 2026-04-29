# DUKE MST CLI Experiment

This code directory is intended for:

```bash
uv run swarmed project create \
  --user jeff \
  --title "DUKE MST IID 3-site CLI" \
  --description "MST DUKE swarm evaluation via swarmed CLI" \
  --code-dir ./experiments/duke_mst_cli
```

`training.py` uses NVFlare's PyTorch Lightning client API and imports the
MediSwarm MST/data stack from the runtime image. Run it with the
ODELIA/MediSwarm image, for example:

```bash
SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:<version>
SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true
```

Required client-local environment:

```bash
SITE_NAME=node_A
INSTITUTION=node_A
DATA_DIR=/mnt/dlhd0/DUKE_iid
SCRATCH_DIR=/mnt/dlhd0/deploy_test_duke_iid/swarmed_cli_mst
MODEL_NAME=MST
CONFIG=unilateral
EPOCHS_PER_ROUND=5
EPOCHS_MAX_CAP=10
```

The CLI runtime also accepts the older MediSwarm aliases `DATADIR` and
`SCRATCHDIR`; they are normalized to `DATA_DIR` and `SCRATCH_DIR`.

`SWARM_ROUNDS = 20` is the default full run. For a 2-round wiring smoke test,
set this on the server/admin host before `training start`:

```bash
SWARMMEDHUB_SWARM_ROUNDS=2 uv run swarmed training start --network <NETWORK_UUID>
```

Evaluate only the NVFlare global model artifact:

```bash
python experiments/duke_mst_cli/evaluate_global_model.py \
  --checkpoint /workspace/<JOB_ID>/app_<site>/FL_global_model.pt \
  --data-root /mnt/dlhd0/DUKE_iid \
  --institution test \
  --output-json ./results/duke_mst_cli/eval.json
```
