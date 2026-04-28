---
title: CLI
description: Local companion command-line workflow for SwarmMedHub.
---

# CLI

`swarmed` is a local companion CLI for SwarmMedHub. It is installed from the same repository, runs inside the same Django environment as the web UI, and uses the same database records, object storage, Celery tasks, and active project/network context.

## Scope

- v1 is a local companion tool, not a remote API client.
- v1 is Linux-first and validated on Linux hosts.
- The CLI covers the executable workflow only: projects, data, networks, training, and results.

## Running the CLI

Install the project environment first. This is the same repository and Python environment used by SwarmMedHub:

```bash
make install
```

If the local stack is not configured yet, generate the environment file as well:

```bash
make env
```

Then run commands through the managed environment:

```bash
uv run swarmed --help
```

The acting user resolves in this order:

1. `--user USERNAME`
2. `SWARMED_USER`
3. the local OS username

If no matching SwarmMedHub user exists, the command exits with a validation error.

## Output and Exit Codes

Every command supports `--json`.

```json
{
  "ok": true,
  "command": "project list",
  "result": {},
  "warnings": [],
  "errors": []
}
```

Exit codes:

- `0`: success
- `2`: usage or validation error
- `1`: unexpected failure

## Workflow Quickstart

### 1. Create a project from a code directory

`training.py` is required at the root of `--code-dir`.

Optional root-level files are picked up automatically:

- `requirements.txt`
- `validation.py`
- `visualization.py`
- `results_visualization.py`

Any other files under the directory are uploaded under the existing training code layout.

```bash
uv run swarmed project create \
  --user alice \
  --title "Demo Project" \
  --description "CLI workflow" \
  --code-dir ./demo-code
```

### 2. Select the current project

```bash
uv run swarmed project use --user alice <PROJECT_UUID>
```

### 3. Upload data

```bash
uv run swarmed data upload \
  --user alice \
  --project <PROJECT_UUID> \
  --dest incoming \
  ./data/patients.csv ./data/images
```

### 4. Run optional data checks

```bash
uv run swarmed data validate --user alice --project <PROJECT_UUID>
uv run swarmed data visualize --user alice --project <PROJECT_UUID> --wait
```

### 5. Create or import a network

Local test network:

```bash
uv run swarmed network create \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Local Test" \
  --local-test
```

Provisioned startup-package network:

```bash
uv run swarmed network create \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Hospital Swarm" \
  --participant site-a=100.64.0.10 \
  --participant site-b=100.64.0.11
```

For server/admin-only hosts, prevent the local host from being added as an
extra training client:

```bash
uv run swarmed network create \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Hospital Swarm" \
  --server-ip 100.64.0.1 \
  --participant site-a=100.64.0.10 \
  --participant site-b=100.64.0.11 \
  --no-local-client
```

Import an existing startup package:

```bash
uv run swarmed network import \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Imported Network" \
  --package ./startup-kits.zip
```

### 6. Select and start the network

```bash
uv run swarmed network use --user alice <NETWORK_UUID>
uv run swarmed network start --user alice <NETWORK_UUID> --wait
```

### 7. Start and watch training

```bash
uv run swarmed training start --user alice --network <NETWORK_UUID>
uv run swarmed training watch --user alice <JOB_UUID>
```

### 8. Sync and download results

```bash
uv run swarmed results sync --user alice --project <PROJECT_UUID>
uv run swarmed results list --user alice --project <PROJECT_UUID>
uv run swarmed results download \
  --user alice \
  --project <PROJECT_UUID> \
  --job <JOB_UUID> \
  --out ./results
```

### 9. Run results visualization

```bash
uv run swarmed results visualize \
  --user alice \
  --project <PROJECT_UUID> \
  --job <JOB_UUID> \
  --wait
```

## Command Tree

```text
project create|list|show|use|update
data upload|ls|download|mv|rm|validate|visualize
network create|import|list|show|use|export-package|start|stop|status
training start|list|status|watch|stop
results sync|list|download|visualize
```
