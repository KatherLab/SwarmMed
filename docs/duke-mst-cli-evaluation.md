---
title: DUKE MST CLI Evaluation
description: Running the DUKE MST 3-site experiment through the swarmed CLI.
---

# DUKE MST CLI Evaluation

This run validates the `swarmed` CLI against the existing MediSwarm DUKE IID
deployment design.

## Topology

| Role | Host | Site | Data |
| --- | --- | --- | --- |
| Server/admin | Cosmos | server only | no training data |
| Client | dl0 | `node_A` | `/mnt/dlhd0/DUKE_iid` |
| Client | dl2 | `node_B` | `/mnt/sda1/DUKE_iid` |
| Client | dl3 | `node_C` | `/mnt/swarm_alpha/DUKE_iid` |
| Eval host | dl0 | `test` | `/mnt/dlhd0/DUKE_iid/test` |

Use `experiments/duke_mst_cli` as the project code directory.

## Create Project And Network

```bash
uv run swarmed project create \
  --user jeff \
  --title "DUKE MST IID 3-site CLI" \
  --description "MST DUKE swarm evaluation via swarmed CLI" \
  --code-dir ./experiments/duke_mst_cli \
  --json

uv run swarmed network create \
  --user jeff \
  --project <PROJECT_UUID> \
  --name "DUKE IID 3-site" \
  --server-ip <COSMOS_TAILSCALE_OR_HOST> \
  --participant node_A=100.64.4.100 \
  --participant node_B=100.64.4.102 \
  --participant node_C=100.64.4.103 \
  --no-local-client \
  --json
```

`--no-local-client` is required because Cosmos is server/admin only.

If the client hosts do not share the same SwarmCloud database/workspace, export
the startup package on Cosmos and import the same ZIP on each client host before
starting its runtime:

```bash
uv run swarmed network export-package <NETWORK_UUID> \
  --out /tmp/duke_iid_3site_startup.zip

# copy /tmp/duke_iid_3site_startup.zip to dl0, dl2, and dl3

uv run swarmed network import \
  --user jeff \
  --project <PROJECT_UUID> \
  --name "DUKE IID 3-site" \
  --package /tmp/duke_iid_3site_startup.zip \
  --json
```

## Start Runtime

Start the server on Cosmos:

```bash
SWARMMEDHUB_SERVER_ONLY_MODE=true \
SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:<version> \
SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true \
uv run swarmed network start <NETWORK_UUID> --wait --json
```

Start the clients on their own hosts with the same startup package/network.

dl0 / `node_A`:

```bash
SWARMMEDHUB_CLIENT_ONLY_MODE=true \
SWARMMEDHUB_LOCAL_PARTICIPANT=node_A \
SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:<version> \
SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true \
SITE_NAME=node_A INSTITUTION=node_A \
DATA_DIR=/mnt/dlhd0/DUKE_iid \
SCRATCH_DIR=/mnt/dlhd0/deploy_test_duke_iid/swarmed_cli_mst \
MODEL_NAME=MST CONFIG=unilateral \
uv run swarmed network start <NETWORK_UUID> --wait --json
```

dl2 / `node_B`:

```bash
SWARMMEDHUB_CLIENT_ONLY_MODE=true \
SWARMMEDHUB_LOCAL_PARTICIPANT=node_B \
SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:<version> \
SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true \
SITE_NAME=node_B INSTITUTION=node_B \
DATA_DIR=/mnt/sda1/DUKE_iid \
SCRATCH_DIR=/mnt/sda1/deploy_test_duke_iid/swarmed_cli_mst \
MODEL_NAME=MST CONFIG=unilateral \
uv run swarmed network start <NETWORK_UUID> --wait --json
```

dl3 / `node_C`:

```bash
SWARMMEDHUB_CLIENT_ONLY_MODE=true \
SWARMMEDHUB_LOCAL_PARTICIPANT=node_C \
SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:<version> \
SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true \
SITE_NAME=node_C INSTITUTION=node_C \
DATA_DIR=/mnt/swarm_alpha/DUKE_iid \
SCRATCH_DIR=/mnt/scratch/deploy_test_duke_iid/swarmed_cli_mst \
MODEL_NAME=MST CONFIG=unilateral \
uv run swarmed network start <NETWORK_UUID> --wait --json
```

## Smoke And Full Training

Two-round smoke run:

```bash
SWARMMEDHUB_SWARM_ROUNDS=2 \
uv run swarmed training start --user jeff --network <NETWORK_UUID> --json
uv run swarmed training watch --user jeff <JOB_UUID> --network <NETWORK_UUID> --interval 30
```

Full 20-round run:

```bash
uv run swarmed training start --user jeff --network <NETWORK_UUID> --json
uv run swarmed training watch --user jeff <JOB_UUID> --network <NETWORK_UUID> --interval 30
```

## Results

```bash
uv run swarmed results sync --user jeff --project <PROJECT_UUID>
uv run swarmed results export \
  --user jeff \
  --project <PROJECT_UUID> \
  --job <JOB_UUID> \
  --out ./results/duke_mst_cli
```

For acceptance, collect `FL_global_model.pt` from each client, verify matching
`md5sum`, then evaluate one global checkpoint on the held-out DUKE test set on
dl0. The reference MediSwarm MST swarm AUROC is approximately `0.895`.

The CLI result sync/export path is scoped to `FL_global_model.pt` for this
global-model evaluation. `last_global_model.ckpt` is a local Lightning
checkpoint and must not be used for the swarm global AUROC.

```bash
python experiments/duke_mst_cli/evaluate_global_model.py \
  --checkpoint ./results/duke_mst_cli/<JOB_UUID>/<site>/FL_global_model.pt \
  --data-root /mnt/dlhd0/DUKE_iid \
  --institution test \
  --output-json ./results/duke_mst_cli/eval.json
```
