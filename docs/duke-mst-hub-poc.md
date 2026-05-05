---
title: DUKE MST Hub POC
description: Running and validating the real 3-site DUKE MST swarm through SwarmMedHub.
---

# DUKE MST Hub POC

This runbook documents the real 3-client DUKE breast MRI MST proof of concept
that was validated through SwarmMedHub on May 4, 2026. The Hub orchestrates and
monitors the swarm; the ODELIA/MediSwarm image provides the MST model and DUKE
runtime stack.

Do not commit Hub passwords, Tailscale keys, or node-specific private tokens.
Ask the maintainer for Tailscale access and a Hub user account before testing.

## Validated Topology

| Role | Host | Tailscale IP | Site | Data |
| --- | --- | --- | --- | --- |
| Hub/server | `hd-cosmos` | `100.100.101.100` | server | none |
| Client | `dd-dl0` | `100.64.4.100` | `node_A` / `node-A` | `/mnt/dlhd0/DUKE_iid` |
| Client | `dd-dl2` | `100.64.4.102` | `node_B` / `node-B` | `/mnt/sda1/DUKE_iid` |
| Client | `dd-dl3` | `100.64.4.103` | `node_C` / `node-C` | `/mnt/swarm_alpha/DUKE_iid` |

The Hub repository path on `hd-cosmos` was:

```bash
/home/jeff/Projects/SwarmCloud-1
```

The client repository path was:

```bash
/home/swarm/Projects/SwarmCloud
```

The validated runtime image was:

```bash
jefftud/odelia:1.4.4-dev.260430.64ef7b1
```

## Preflight

Confirm the Hub stack is healthy:

```bash
ssh hd-cosmos
cd /home/jeff/Projects/SwarmCloud-1
docker compose ps
docker compose exec -T -e PYTHONPATH=/app app \
  swarmed project list --user admin --json
```

Confirm the FLARE server ports are free before starting the real server:

```bash
docker rm -f odelia_swarm_server_flserver_64ef7b1 2>/dev/null || true
ss -ltnp | grep -E ':8002|:8003' || true
```

Check each client host:

```bash
for h in dd-dl0 dd-dl2 dd-dl3; do
  ssh "$h" 'hostname; docker --version; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader'
done
```

Pull the same runtime image on all hosts:

```bash
for h in hd-cosmos dd-dl0 dd-dl2 dd-dl3; do
  ssh "$h" 'docker pull jefftud/odelia:1.4.4-dev.260430.64ef7b1'
done
```

If a client cannot pull from the registry, copy the image from `hd-cosmos`:

```bash
ssh hd-cosmos 'docker save jefftud/odelia:1.4.4-dev.260430.64ef7b1 | gzip' \
  | ssh <client> 'gunzip | docker load'
```

## Create Project And Network In The Hub

Run these commands inside the Hub app container so the CLI uses the same
database and storage as the GUI:

```bash
cd /home/jeff/Projects/SwarmCloud-1

PROJECT_JSON=$(docker compose exec -T -e PYTHONPATH=/app app \
  swarmed project create \
    --user admin \
    --title "DUKE MST IID 3-site Hub POC 2026-05-04" \
    --description "Real Duke breast MRI MST 3-site POC monitored through SwarmCloud Hub" \
    --code-dir ./experiments/duke_mst_cli \
    --json)

PROJECT_UUID=<extract-from-PROJECT_JSON>

NETWORK_JSON=$(docker compose exec -T -e PYTHONPATH=/app app \
  swarmed network create \
    --user admin \
    --project "$PROJECT_UUID" \
    --name "DUKE MST IID 3-site Real Swarm" \
    --server-ip 100.100.101.100 \
    --participant node_A=100.64.4.100 \
    --participant node_B=100.64.4.102 \
    --participant node_C=100.64.4.103 \
    --no-local-client \
    --json)

NETWORK_UUID=<extract-from-NETWORK_JSON>
```

Export and copy the startup package:

```bash
docker compose exec -T -e PYTHONPATH=/app app \
  swarmed network export-package "$NETWORK_UUID" \
    --out /tmp/duke_mst_iid_3site_startup.zip

for h in dd-dl0 dd-dl2 dd-dl3; do
  scp /tmp/duke_mst_iid_3site_startup.zip "$h:/tmp/"
done
```

## Start Client Runtimes

Client-side commands should run tasks inline because there is no local Celery
worker on the DL nodes. The runtime also needs a higher file descriptor limit
for the 20-round MST run, and `SWARMMEDHUB_FORCE_GPU=true` on these hosts because
Docker GPU support works even when the runtime probe cannot infer it from the
daemon runtime list.

Common environment:

```bash
cd /home/swarm/Projects/SwarmCloud
export HOST_PROJECT_PATH=/home/swarm/Projects/SwarmCloud
export SWARMMEDHUB_RUN_TASKS_INLINE=true
export SWARMMEDHUB_CLIENT_ONLY_MODE=true
export SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:1.4.4-dev.260430.64ef7b1
export SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true
export SWARMMEDHUB_FORCE_GPU=true
export SWARMMEDHUB_RUNTIME_NOFILE_LIMIT=65536
export SWARMMEDHUB_DISABLE_ASYNC_LOGGING=true
export DB_HOST=127.0.0.1
export REDIS_HOST=127.0.0.1
export AWS_S3_ENDPOINT_URL=https://127.0.0.1:9000
export SWARMMEDHUB_LOCAL_S3_ENDPOINT=https://127.0.0.1:9000
```

Set the site-specific values before importing and starting.

`dd-dl0`:

```bash
export DB_PORT=55432
export SWARMMEDHUB_LOCAL_PARTICIPANT=node_A
export SITE_NAME=node_A INSTITUTION=node_A
export DATA_DIR=/mnt/dlhd0/DUKE_iid
export SCRATCH_DIR=/mnt/dlhd0/deploy_test_duke_iid/swarmed_hub_poc
```

`dd-dl2`:

```bash
export DB_PORT=5432
export SWARMMEDHUB_LOCAL_PARTICIPANT=node_B
export SITE_NAME=node_B INSTITUTION=node_B
export DATA_DIR=/mnt/sda1/DUKE_iid
export SCRATCH_DIR=/mnt/sda1/deploy_test_duke_iid/swarmed_hub_poc
```

`dd-dl3`:

```bash
export DB_PORT=5432
export SWARMMEDHUB_LOCAL_PARTICIPANT=node_C
export SITE_NAME=node_C INSTITUTION=node_C
export DATA_DIR=/mnt/swarm_alpha/DUKE_iid
export SCRATCH_DIR=/mnt/scratch/deploy_test_duke_iid/swarmed_hub_poc
```

Import the startup package and start the local participant:

```bash
.venv/bin/python -m swarmed_cli.main network import \
  --user jeff \
  --project <LOCAL_PROJECT_UUID> \
  --name "DUKE MST IID 3-site Real Swarm" \
  --package /tmp/duke_mst_iid_3site_startup.zip \
  --json

CLIENT_NETWORK_UUID=<imported-network-uuid>

MODEL_NAME=MST CONFIG=unilateral \
.venv/bin/python -m swarmed_cli.main network start \
  --user jeff \
  "$CLIENT_NETWORK_UUID" \
  --wait --timeout 900 --json
```

Validate GPU and file descriptor access inside a started client container:

```bash
docker exec swarm-<network-prefix>-node-A /bin/bash -lc \
  'ulimit -n; nvidia-smi --query-gpu=memory.used --format=csv,noheader'
```

The validated run used `nofile=65536:65536` at container creation time. Docker
may report a larger effective value inside the container on some hosts.

## Start Server Runtime

Start the server from the Hub app container with inline execution so the runtime
environment is visible to the task:

```bash
cd /home/jeff/Projects/SwarmCloud-1

docker compose exec -T \
  -e PYTHONPATH=/app \
  -e SWARMMEDHUB_RUN_TASKS_INLINE=true \
  -e HOST_PROJECT_PATH=/home/jeff/Projects/SwarmCloud-1 \
  -e SWARMMEDHUB_SERVER_ONLY_MODE=true \
  -e SWARMMEDHUB_FLARE_IMAGE=jefftud/odelia:1.4.4-dev.260430.64ef7b1 \
  -e SWARMMEDHUB_FORCE_CONFIGURED_IMAGE=true \
  -e SWARMMEDHUB_RUNTIME_NOFILE_LIMIT=65536 \
  app swarmed network start \
    --user admin \
    "$NETWORK_UUID" \
    --wait --timeout 900 --json
```

Open the Hub at:

```text
https://hd-cosmos:5085/
```

Confirm the Network page shows the Duke network as running.

## Smoke And Full Training

Run a two-round smoke first:

```bash
SWARMMEDHUB_SWARM_ROUNDS=2 \
docker compose exec -T -e PYTHONPATH=/app app \
  swarmed training start \
    --user admin \
    --network "$NETWORK_UUID" \
    --json

docker compose exec -T -e PYTHONPATH=/app app \
  swarmed training watch \
    --user admin \
    --network "$NETWORK_UUID" \
    <SMOKE_HUB_JOB_UUID> \
    --interval 30 --timeout 7200
```

Then run the full 20-round job without `SWARMMEDHUB_SWARM_ROUNDS`:

```bash
docker compose exec -T -e PYTHONPATH=/app app \
  swarmed training start \
    --user admin \
    --network "$NETWORK_UUID" \
    --json

docker compose exec -T -e PYTHONPATH=/app app \
  swarmed training watch \
    --user admin \
    --network "$NETWORK_UUID" \
    <FULL_HUB_JOB_UUID> \
    --interval 60 --timeout 14400
```

Useful monitoring commands:

```bash
docker logs --tail 2200 swarm-<network-prefix>-server 2>&1 \
  | grep -E '<FLARE_JOB_UUID>.*(updated status of client|FATAL|ERROR|TASK_ABORTED|Workflow controller|Server runner finished)'
```

```bash
for spec in dd-dl0:swarm-<network-prefix>-node-A dd-dl2:swarm-<network-prefix>-node-B dd-dl3:swarm-<network-prefix>-node-C; do
  h=${spec%:*}
  c=${spec#*:}
  ssh "$h" "docker logs --tail 300 \"$c\" 2>&1 | grep -E '<FLARE_JOB_UUID>|Epoch [0-9]+ - val|finished training round|ERROR|Traceback|Too many open|TASK_ABORTED'"
done
```

The 20-round validation run completed in 2h 34m 20s with Hub status
`Completed 100%`.

## Results And Evaluation

Current multi-host result collection requires staging each client artifact into
the Hub workspace before `results sync` can index it. Each client produced:

```text
<client-workspace>/prod_00/node-*/<FLARE_JOB_UUID>/app_node-*/FL_global_model.pt
```

Copy those files into the central Hub workspace layout:

```text
/home/jeff/Projects/SwarmCloud-1/workspaces/<PROJECT_UUID>/<NETWORK_UUID>/workspace/<safe-project-name>/prod_00/node-A/<FLARE_JOB_UUID>/app_node-A/FL_global_model.pt
/home/jeff/Projects/SwarmCloud-1/workspaces/<PROJECT_UUID>/<NETWORK_UUID>/workspace/<safe-project-name>/prod_00/node-B/<FLARE_JOB_UUID>/app_node-B/FL_global_model.pt
/home/jeff/Projects/SwarmCloud-1/workspaces/<PROJECT_UUID>/<NETWORK_UUID>/workspace/<safe-project-name>/prod_00/node-C/<FLARE_JOB_UUID>/app_node-C/FL_global_model.pt
```

Then sync and download from the Hub:

```bash
docker compose exec -T -e PYTHONPATH=/app app \
  swarmed results sync \
    --user admin \
    --project "$PROJECT_UUID" \
    --json

docker compose exec -T -e PYTHONPATH=/app app \
  swarmed results download \
    --user admin \
    --project "$PROJECT_UUID" \
    --job "$FULL_HUB_JOB_UUID" \
    --out ./results/duke_mst_hub_poc \
    --json
```

Evaluate one downloaded `FL_global_model.pt` on `dd-dl0` using the ODELIA image:

```bash
docker run --rm --gpus all --ulimit nofile=65536:65536 \
  -e PYTHONPATH=/MediSwarm/application/jobs/_shared/custom:/MediSwarm/application/jobs/_shared/custom/models \
  -e DATA_DIR=/mnt/dlhd0/DUKE_iid \
  -e SCRATCH_DIR=/tmp/duke_mst_eval \
  -e SITE_NAME=test \
  -e INSTITUTION=test \
  -e CONFIG=unilateral \
  -e MODEL_NAME=MST \
  -v /home/swarm/Projects/SwarmCloud:/workspace/SwarmCloud \
  -v /mnt/dlhd0/DUKE_iid:/mnt/dlhd0/DUKE_iid:ro \
  -w /workspace/SwarmCloud \
  jefftud/odelia:1.4.4-dev.260430.64ef7b1 \
  python experiments/duke_mst_cli/evaluate_global_model.py \
    --checkpoint results/duke_mst_hub_poc/<download-dir>/<FLARE_JOB_UUID>/node-A/FL_global_model.pt \
    --data-root /mnt/dlhd0/DUKE_iid \
    --institution test \
    --split test \
    --config unilateral \
    --model-name MST \
    --output-json results/duke_mst_hub_poc/eval_nodeA_test.json
```

The validated full run produced:

```text
Hub job: 46428e94-8cf6-4df3-9cc8-004c1220d841
FLARE job: 58179610-4c7f-4ce5-87fd-a6077333bdab
Global model MD5: 599f69ba046074ecf8fda56506679e65
AUROC: 0.8972
Accuracy: 0.8588
F1: 0.8614
Sensitivity: 0.8394
Specificity: 0.8800
```

## CLI And GUI Acceptance

Run lightweight CLI checks before and after the training run:

```bash
docker compose exec -T -e PYTHONPATH=/app app \
  swarmed project list --user admin --json

docker compose exec -T -e PYTHONPATH=/app app \
  swarmed network list --user admin --json
```

On each client, verify the imported runtime record:

```bash
SWARMMEDHUB_DISABLE_ASYNC_LOGGING=true \
SWARMMEDHUB_CLIENT_ONLY_MODE=true \
SWARMMEDHUB_RUN_TASKS_INLINE=true \
DB_HOST=127.0.0.1 \
DB_PORT=<5432-or-55432> \
REDIS_HOST=127.0.0.1 \
AWS_S3_ENDPOINT_URL=https://127.0.0.1:9000 \
SWARMMEDHUB_LOCAL_S3_ENDPOINT=https://127.0.0.1:9000 \
.venv/bin/python -m swarmed_cli.main network status \
  --user jeff \
  <CLIENT_NETWORK_UUID> \
  --json
```

The client status command may emit a local MinIO certificate warning; it is
accepted for this check if the JSON payload returns `ok: true`.

In the GUI:

1. Open `https://hd-cosmos:5085/`.
2. Set the Duke project and network as current.
3. Confirm the Network page shows the Duke network.
4. Confirm the Training page/status API shows `Completed` and `100%`.
5. Confirm the Results page lists `FL_global_model.pt` for the full job.

## Known Issues

- Multi-host client artifacts are not collected automatically into the central
  Hub workspace. Stage the three `FL_global_model.pt` files manually before
  running `results sync`.
- Hub audit logging can emit non-blocking warnings similar to
  `RequestContextMiddleware has no attribute get_context`.
- On the validated DL hosts, force GPU runtime selection with
  `SWARMMEDHUB_FORCE_GPU=true`.
- Host-side evaluation may fail with missing ODELIA modules. Run the evaluator
  in the ODELIA image and set the `PYTHONPATH` shown above.
