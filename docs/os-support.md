---
title: Operating systems
description: Which components are genuinely OS-dependent, and which are merely untested.
---

# Operating system support

The short version: **SwarmMed supports Linux (Ubuntu, Debian, Fedora, RHEL/Rocky), macOS (Docker Desktop), and Windows (WSL2 / Docker Desktop) across the web interface, the `swarmed` CLI, the coordinating site (FL server), and training sites (FL clients).** NVIDIA GPU acceleration is supported on Linux and Windows (WSL2 with NVIDIA Container Toolkit), while macOS and CPU-only hosts run FL client containers on CPU via Docker bridge networking.

## By role

| Role | Supported OS | How it runs |
|---|---|---|
| **Site running training** (FL client) | Linux (GPU or CPU), Windows with WSL2 / Docker Desktop (NVIDIA GPU or CPU), macOS with Docker Desktop (CPU) | Uses host networking on native Linux by default (`SWARMMEDHUB_CLIENT_HOST_NETWORK=true`) and automatically switches to Docker bridge networking with `--add-host` routing on Docker Desktop (macOS/Windows) |
| **Coordinating site** (FL server, platform services) | Linux, macOS (Docker Desktop), Windows (WSL2 / Docker Desktop) | The FL server and platform services run on Docker bridge networks with published ports and require no GPU |
| **Clinician / analyst using the interface** | Any OS with a current browser | Web application over HTTPS |
| **Operator using the `swarmed` CLI** | Linux, macOS, Windows (WSL2) | Runs either inside the application container (`docker exec -w /app swarmmedhub python3 -m swarmed_cli.main …`) or directly on the host (`uv run swarmed …`), with automatic host endpoint and CA certificate resolution |

A network created in the web interface also makes the coordinating host a
training site; the CLI does the same unless `--no-local-client` is given.

On the platform host, you can run the CLI either inside the application container
(`docker exec -w /app swarmmedhub python3 -m swarmed_cli.main <command>`) or
directly on the host (`uv run swarmed <command>`). When invoked on the host
outside a container, `swarmed_cli.bootstrap` automatically translates the
container-internal hostnames in `.env` (`postgres`, `redis`, `minio`) and the
internal CA path to the published `127.0.0.1` ports (auto-discovering
`POSTGRES_HOST_PORT` via `docker port` if needed) and `.secrets/certs/internal/ca.crt`.
See [CLI](cli.md).

## Container runtime features across operating systems

1. **GPU passthrough.** Training containers receive NVIDIA GPUs via
   `docker run --gpus all` on Linux (NVIDIA Container Toolkit) and Windows
   (Docker Desktop with WSL2 backend). SwarmMed's GPU auto-detection checks both
   whether `docker info` lists a legacy `nvidia` runtime and, if not, executes a
   lightweight `docker run --rm --gpus all` capability probe against the runtime
   image. This automatically detects modern CDI-only setups, Ubuntu hosts where
   `--gpus all` works without a legacy runtime entry in `/etc/docker/daemon.json`,
   and Windows WSL2 Docker Desktop. On macOS or CPU-only hosts, the probe
   gracefully falls back to CPU execution. You can also explicitly override
   detection with `SWARMMEDHUB_FORCE_GPU=true` or disable GPUs with
   `SWARMMEDHUB_ENABLE_GPU=false`.
2. **Container networking (Host vs. Bridge mode).** SwarmMed stores participant IP
   metadata in `client_ip` / `.client_ip_map.json` rather than NVFlare's
   relay-oriented `listening_host` property, and automatically normalizes any
   legacy `local/comm_config.json` internal listener host to `localhost`. Because
   each client's internal parent-to-subprocess listener binds to container
   loopback (`localhost`) and all client-to-server FLARE traffic uses outbound
   gRPC connections, FL client containers work in both `--network host` mode
   (default on native Linux) and standard Docker bridge mode (auto-selected on
   macOS and Windows Docker Desktop, or selectable on Linux via
   `SWARMMEDHUB_CLIENT_HOST_NETWORK=false`).

The Docker-in-Docker sandbox, in which site validation and visualisation scripts
run, needs a privileged container on a Linux kernel, which both native Linux
Docker Engine and Docker Desktop (inside its Linux VM on macOS and Windows)
provide. Scripts there are limited to 1 GB of memory, one CPU and 300 s. The
sandbox daemon is isolated from the platform's internal database network, with
access confined to MinIO (`sandbox_internal`) and outbound internet access
(`sandbox_egress`), preventing access to the host Docker socket proxy or internal
databases.

Everything else — the Django application, the web interface and the CLI
(portable Python), and the database, object store, queue and reverse proxy
(PostgreSQL, MinIO, Redis and nginx in Linux container images) — runs in standard
containers. The PostgreSQL entrypoint copies the TLS private key onto the
container's native filesystem with `0600 postgres:postgres` permissions at
startup so bind mounts on macOS (VirtioFS/APFS) and Windows (WSL2/NTFS) start
cleanly without host UID-mapping errors, and `scripts/generate_internal_certs.sh`
uses explicit X.509 extension configs compatible with both Linux OpenSSL and
macOS LibreSSL.

## Prerequisites (any Linux distribution)

Rather than naming distributions, check these. Nothing in the code checks them.
Apart from the driver floor, which follows from the runtime image, the values are
what has been run; older versions are untested rather than known not to work.

| Component | Requirement |
|---|---|
| Kernel | cgroup v2; run on the 5.14, 6.17 and 7.0 series |
| Docker Engine | run on 28.4–29.8, with the Compose plugin (`docker compose`, 5.1.4 on the platform host) |
| NVIDIA driver | 530.30 or newer for the worked example's CUDA 12.1 runtime (NVIDIA's minimum for CUDA 12.1); the training hosts ran 580.173 and 580.178 |
| NVIDIA Container Toolkit | only at sites that train; run with 1.13.5, 1.17.8 and 1.20.0 |
| Python 3.12 | on every host with a SwarmMed installation: `make install` fetches `uv` if it is missing, then Python 3.12 and the project environment (9.6 GiB on the platform host), which the installation scripts use |

Any distribution that meets these should work — Debian, RHEL and derivatives such
as Rocky and AlmaLinux, Fedora, SLES — but SwarmMed has so far been run only on
Ubuntu 22.04 and 24.04 (below). The installation guide's commands are written
with `apt`; on RPM-based systems substitute `dnf` and the corresponding Docker and
NVIDIA repositories.
Nothing in the application depends on the package manager.

## Where this has actually been run

| Host role | Distribution | Kernel | Docker | Toolkit | NVIDIA driver |
|---|---|---|---|---|---|
| Platform + FL server | Ubuntu 24.04 LTS | 6.17 | 29.5 | — | — |
| Training site | Ubuntu 22.04 LTS | 5.14 | 29.8 | 1.20 | 580.178 |
| Training site | Ubuntu 22.04 LTS | 5.14 | 29.8 | 1.13 | 580.178 |
| Training site | Ubuntu 24.04 LTS | 7.0 | 28.4 | 1.17 | 580.173 |

Versions as recorded on 22 September 2026, during the two-site ablation run, and
unchanged on 8 October. None of the training hosts had rebooted since 11
September, so kernel and driver are those of the worked-example runs. All four
hosts use cgroup v2. The training hosts ran an April–May 2026 copy of SwarmMed in
the reduced form described under [Hardware](hardware.md#platform-overhead-measured).

Two Ubuntu releases, three kernel series (5.14, 6.17 and 7.0), three Docker
versions (28.4–29.8) and three toolkit versions (1.13–1.20); the deployment was
not sensitive to these.

Separately, automated fresh-installation tests were executed against clean
container environments following the README instructions literally (`make install`,
`make env`, `make start`, web UI verification, superuser initialization,
in-container CLI verification, project creation, data import, local-test network
provisioning and startup, 2-round tabular training, and results synchronization):

| Distribution | Docker Engine | Docker Compose | Result |
|---|---|---|---|
| **Ubuntu 24.04 LTS** | 29.1.3 | v2.40.3 | **Passed all stages** end to end, through training and results sync (97 s execution) |
| **Debian GNU/Linux 13 (trixie)** | 29.9.0 | v5.6.0 | **Passed deployment and provisioning**: `make install`, `make env`, `make start`, Web UI, superuser, CLI, project creation, data import, network provisioning, and runtime image build |
| **Fedora Linux 44** | 29.9.0 | v5.6.0 | **Passed deployment and provisioning**: `make install`, `make env`, `make start`, Web UI, superuser, CLI, project creation, data import, network provisioning, and runtime image build |
| **Rocky Linux 9.8 (Blue Onyx)** | 29.9.0 | v5.6.0 | **Passed deployment and provisioning**: `make install`, `make env`, `make start`, Web UI, superuser, CLI, project creation, data import, network provisioning, and runtime image build |

Across all four distributions, dependency installation (`uv`), platform container builds,
service orchestration, database migrations, HTTPS endpoints, superuser creation,
and network provisioning completed without distribution-specific defects. On
Debian, Fedora, and Rocky, network startup during concurrent parallel test runs
reached Celery's default 25-minute soft task time limit (`CELERY_TASK_SOFT_TIME_LIMIT`)
during un-cached pip downloads of PyTorch and CUDA libraries under bandwidth
contention; the underlying image builds completed successfully in ~26 minutes.
SwarmMed now exposes configurable Celery task limits with a 60-minute default to
support slower network connections during initial runtime image builds.

## macOS and Windows

* **Using the interface: supported.** SwarmMedHub is a web application and
  nothing is installed on the user's computer, so clinicians at a participating
  site can connect from any browser on macOS, Windows, or Linux.
* **Running the coordinating site or a training site: supported via Docker Desktop / WSL2.**
  - **Networking:** SwarmMed automatically detects Docker Desktop (`OperatingSystem: Docker Desktop` / `linuxkit` kernel / `darwin` / `nt`) and launches FL client containers on the Docker bridge network with `--add-host` routing (`swarmmedhub:host-gateway`, `host.docker.internal:host-gateway`, and `server:<coordinating-ip>`), while binding NVFlare's internal parent-to-subprocess listener to `localhost`.
  - **Compute (GPU vs. CPU):** On Windows with WSL2 and NVIDIA Container Toolkit, SwarmMed's `--gpus all` probe detects and passes through the NVIDIA GPU automatically. On macOS (Intel or Apple Silicon) or Windows hosts without an NVIDIA GPU, client containers fall back cleanly to CPU execution (suitable for tabular, clinical, and CPU-compatible models; GPU-mandatory 3D imaging workloads such as the breast-MRI MST example require an NVIDIA GPU on Linux or Windows WSL2).
  - **Filesystem & TLS:** Internal certificate generation (`scripts/generate_internal_certs.sh`) is compatible with both OpenSSL and macOS LibreSSL, includes SANs for `localhost`, `127.0.0.1`, `172.17.0.1`, `swarmmedhub`, and `host.docker.internal`, and the PostgreSQL container copies its TLS key to container-local storage at startup to avoid VirtioFS/NTFS bind-mount ownership errors.
* **CLI use (`swarmed`):** Supported both inside the application container (`docker exec -w /app swarmmedhub python3 -m swarmed_cli.main …`) and directly on the host (`uv run swarmed …` on Linux, macOS, or Windows WSL2).
