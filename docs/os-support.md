---
title: Operating systems
description: Which components are genuinely OS-dependent, and which are merely untested.
---

# Operating system support

The short version: **sites that train run Linux; the coordinating site and the CLI
have so far only been run on Linux; people who only use the web interface can use
any operating system.** The hard Linux requirement for training sites comes from
two container features, not from the application, and it is about the container
runtime rather than about a particular distribution.

## By role

| Role | Requirement | Why |
|---|---|---|
| **Site running training** (FL client) | Linux | GPU passthrough and host networking (below) |
| **Coordinating site** (FL server, platform services) | Linux, the only OS used so far | The FL server container runs on a Docker bridge network with published ports and needs no GPU, so neither feature applies to it; other operating systems are untested rather than known not to work |
| **Clinician / analyst using the interface** | Any OS with a current browser | It is a web application |
| **Operator using the `swarmed` CLI** | A shell on the platform host (Linux so far) | The CLI runs inside the platform's application container; any computer that can open an SSH session to the host will do |

A network created in the web interface also makes the coordinating host a
training site, which brings both container features back; the CLI does the same
unless `--no-local-client` is given.

On the platform host, run the CLI inside the application container:
`docker exec -w /app swarmmedhub python3 -m swarmed_cli.main <command>`. Run on
the host as installed (`uv run swarmed …`), it fails: the generated `.env` names
the database host `postgres`, which resolves only inside the Docker network. The
host-side form needs at least `DB_HOST=127.0.0.1` and the published database
port; that is how it ran on the worked example's training hosts, where each call
stalled about 20 s retrying Redis. See [CLI](cli.md).

## What is genuinely OS-dependent

1. **GPU passthrough.** Training containers get GPUs through the NVIDIA Container
   Toolkit (`docker run --gpus all`), which runs only on Linux. The platform's GPU
   auto-detection only checks whether `docker info` lists a runtime named `nvidia`
   (registered, for example, by `nvidia-ctk runtime configure --runtime=docker`).
   Hosts on which `--gpus all` works without that runtime being registered — all
   three training hosts of the worked example — are not detected, and neither are
   other setups such as CDI-only ones. The client container then starts without a
   GPU, only a warning is logged, and a GPU-only model such as the breast-MRI
   example fails at its first task. Where `docker run --gpus all` works, set
   `SWARMMEDHUB_FORCE_GPU=true` (it adds that flag) or register the runtime.
2. **Host networking.** FL client containers are started with `--network host`,
   except in local-test networks. SwarmMed's startup kit pins each client's
   internal listener to that participant's IP address, so the address must exist
   on an interface the client container can bind. On Docker Desktop for macOS and
   Windows, containers run inside a Linux virtual machine and host networking
   refers to that machine's interfaces, which do not carry the computer's own
   addresses, so the client cannot bind the address written into its startup kit.
   This follows from the design; it has not been tried.

The Docker-in-Docker sandbox, in which site validation and visualisation scripts
run, is not a host-OS dependency in the same sense: it needs a privileged
container on a Linux kernel, which Docker Desktop also provides inside its virtual
machine. It has only been run on Linux. Scripts there are limited to 1 GB of
memory, one CPU and 300 s. The sandbox daemon is isolated from the platform's
internal database network, with access confined to MinIO (`sandbox_internal`)
and outbound internet access (`sandbox_egress`), preventing access to the host
Docker socket proxy or internal databases.

Everything else — the Django application, the web interface and the CLI
(portable Python), and the database, object store, queue and reverse proxy
(PostgreSQL, MinIO, Redis and nginx in Linux container images) — has no
OS-specific requirement of its own. The installation steps (`make`, shell scripts,
`uv`) assume a Unix-like shell.

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
  site do not need Linux. The web certificate the installation generates is issued
  for `localhost` and `127.0.0.1` only, so browsers on other computers show a
  certificate warning.
* **Running a training site: not supported.** Host networking does not work as
  required under Docker Desktop, and GPU passthrough is unavailable on macOS.
  Docker Desktop on Windows offers NVIDIA GPUs through its WSL2 backend, but
  neither that nor a Windows host with WSL2 has been tested; treat it as
  unvalidated.
* **Running the coordinating site: untested.** The FL server needs neither
  container feature, but nothing has been run under Docker Desktop or WSL2.
* **CLI use:** the CLI runs inside the platform's container on the platform host,
  so an operator on macOS or Windows uses it over SSH.

If your institution's only available hardware is macOS or Windows, the practical
arrangement is a Linux server — with an NVIDIA GPU if the model needs one, as the
breast-MRI example does — for the site's client and data, with clinicians using
the interface from their usual desktops.
