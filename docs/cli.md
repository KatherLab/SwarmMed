---
title: CLI
description: The swarmed command-line interface to SwarmMedHub, run inside the application container.
---

# CLI

`swarmed` is the command-line interface (CLI) to SwarmMedHub. It is part of the
same repository, calls the same Django services as the web interface, and uses
the same database records, object storage, Celery tasks and active
project/network context.

## Scope

- In SwarmMed 1.0.0 the CLI runs on the SwarmMedHub host, inside the application
  container. It is not a client for a remote Hub.
- It has so far been run only on Linux hosts
  ([Operating systems](os-support.md)). From macOS or Windows, use it over SSH on
  the platform host.
- It covers the executable workflow: projects, data, networks, training and
  results. User management and the log viewer are only in the web interface.
- It performs no authentication. It acts as the user it is given (below), so
  anyone who can run commands in the application container can act as any user
  ([Security](security.md#access-control-rbac)).

## Running the CLI

On a SwarmMedHub host, run the CLI inside the application container
(`swarmmedhub`), where the repository is mounted at `/app`:

```bash
docker exec -w /app swarmmedhub python3 -m swarmed_cli.main --help
```

From the repository directory, `docker compose exec app python3 -m swarmed_cli.main …`
is equivalent. Call the module with `python3 -m`: the `swarmed` command installed
in the image cannot import the package from the mounted repository.

The examples on this page write `swarmed` for that command. To type them as
shown, define a shell function in your session:

```bash
swarmed() { docker exec -w /app swarmmedhub python3 -m swarmed_cli.main "$@"; }
```

When the CLI runs in the container:

* **Paths** are resolved inside the container, whose working directory `/app` is
  the repository directory. `--code-dir`, the files given to `data import`,
  `--package` and `--out` must therefore lie inside the repository directory on
  the host, for example `./demo-code` or `tmp/startup.zip`.
* **Environment variables** for one command are passed with `docker exec -e`, for
  example `docker exec -e SWARMMEDHUB_SWARM_ROUNDS=2 -w /app swarmmedhub python3 -m swarmed_cli.main training start …`.
  `training start` builds and submits the job in the CLI process, so such
  variables apply to it. `network start` hands the start to the Celery worker,
  so variables that control how a network starts, such as client-only mode, must
  be set for the worker ([Networking](networking.md)).
* **Do not run `uv` inside the container.** `/app` is the host's repository
  directory, and uv treats the host's `.venv` as broken there: it deletes it and
  creates an empty one owned by root.

The acting user resolves in this order:

1. `--user USERNAME`
2. `SWARMED_USER` (pass it with `docker exec -e SWARMED_USER=<name> …`)
3. the operating-system user name, which inside the container is `root`

If no matching SwarmMedHub user exists, the command exits with a validation error.

### Running on the host

`make install` also installs a `swarmed` command into the host's `.venv`
(`uv run swarmed …`). On a SwarmMedHub host it fails as installed: the generated
`.env` addresses the database as `postgres` and the object store as `minio`, and
Redis is addressed as `redis` by default; these names resolve only inside the
Docker network. Run on the host, the CLI needs at least:

* `DB_HOST=127.0.0.1`, and `DB_PORT` set to the published database port
  (`POSTGRES_HOST_PORT`, 5432 by default);
* the internal CA certificate where the database connection looks for it,
  `/usr/local/share/ca-certificates/internal-ca.crt`, unless `DB_SSLROOTCERT` or
  `CA_CERT_PATH` names another path. With `DEBUG=False` the connection verifies
  the server's certificate (`sslmode=verify-full`).

Commands that use the object store, or that queue work for the Celery worker,
also need MinIO and Redis; no host-side configuration for them has been tested.
The worked example's training hosts ran the CLI this way, with
`DB_HOST=127.0.0.1`, on a reduced installation without the web application,
worker or object store, to import the startup package; each call stalled about
20 s retrying Redis ([Reproducing the worked example](reproduce.md)).

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

`network start --wait` returns when the network is running or has failed, and
exits with code 2 if it ends in the `ERROR` state.

## Workflow Quickstart

### 1. Create a project from a code directory

`training.py` is required at the root of `--code-dir`.

Optional root-level files are picked up automatically:

- `requirements.txt`
- `validation.py`
- `visualization.py`
- `results_visualization.py`

Any other files under the directory are imported under the existing training code layout.

```bash
swarmed project create \
  --user alice \
  --title "Demo Project" \
  --description "CLI workflow" \
  --code-dir ./demo-code
```

### 2. Select the current project

```bash
swarmed project use --user alice <PROJECT_UUID>
```

### 3. Import data

```bash
swarmed data import \
  --user alice \
  --project <PROJECT_UUID> \
  --dest incoming \
  ./data/patients.csv ./data/images
```

### 4. Run optional data checks

```bash
swarmed data validate --user alice --project <PROJECT_UUID>
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
```

### 5. Create or import a network

Local test network, with all participants on this host (`--clients N` sets the
number of clients, 2 by default):

```bash
swarmed network create \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Local Test" \
  --local-test
```

Provisioned startup-package network:

```bash
swarmed network create \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Hospital Swarm" \
  --participant site-a=100.64.0.10 \
  --participant site-b=100.64.0.11
```

Each `--participant NAME=IPv4` needs an IPv4 address that stays assigned to that
site's host for the study ([Networking](networking.md#addressing)). Without
`--server-ip`, the FL server's address is this host's Tailscale address. Unless
`--no-local-client` is given, this host is also added as a training client, named
after its `SWARMMEDHUB_HOSTNAME`.

For server/admin-only hosts, prevent the local host from being added as an
extra training client:

```bash
swarmed network create \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Hospital Swarm" \
  --server-ip 100.64.0.1 \
  --participant site-a=100.64.0.10 \
  --participant site-b=100.64.0.11 \
  --no-local-client
```

Export the startup package for the other sites:

```bash
swarmed network export-package --user alice <NETWORK_UUID> --out tmp/startup-kits.zip
```

!!! warning "The startup package is the study's master credential"
    The package contains every participant's private key, the FL server's
    private key, and for each site an admin identity with NVFlare's
    `project_admin` role, which may run code at every site. Write it to a path
    that git ignores, such as `tmp/`, move it only over an encrypted,
    authenticated channel, and delete copies once they are imported (see
    [Key custody](threat-model.md#key-custody-what-the-startup-package-contains)).
    Without `--out`, the package is written to the repository directory as
    `<network name>_startup_kits.zip`.

Import an existing startup package:

```bash
swarmed network import \
  --user alice \
  --project <PROJECT_UUID> \
  --name "Imported Network" \
  --package tmp/startup-kits.zip
```

### 6. Select and start the network

```bash
swarmed network use --user alice <NETWORK_UUID>
swarmed network start --user alice <NETWORK_UUID> --wait
```

At a participating site, set client-only mode for the worker before starting
the network; otherwise the start also launches a copy of the FL server from the
imported package. A coordinating host that does not train uses server-only mode.
Both are described in [Networking](networking.md).

### 7. Start and watch training

```bash
swarmed training start --user alice --network <NETWORK_UUID>
swarmed training watch --user alice <JOB_UUID>
```

The network must be running on this Hub. The job is not submitted if the
project's stored code has no `training.py`, for example while the object store is
unavailable. The number of rounds comes from `SWARMMEDHUB_SWARM_ROUNDS`, passed
with `docker exec -e`, or else from `SWARM_ROUNDS` in `training.py`, and is 10 if
neither is set ([Developer](developer.md#training-scripts-nvflare)).

### 8. Sync and export results

```bash
swarmed results sync --user alice --project <PROJECT_UUID>
swarmed results list --user alice --project <PROJECT_UUID>
swarmed results export \
  --user alice \
  --project <PROJECT_UUID> \
  --job <JOB_UUID> \
  --out ./results
```

### 9. Run results visualization

```bash
swarmed results visualize \
  --user alice \
  --project <PROJECT_UUID> \
  --job <JOB_UUID> \
  --wait
```

## Command Tree

```text
project create|list|show|use|update
data import|ls|export|mv|rm|validate|visualize
network create|import|list|show|use|export-package|start|stop|status
training start|list|status|watch|stop
results sync|list|export|visualize
```
