---
title: Operating systems
description: Which components are genuinely OS-dependent, and which are merely untested.
---

# Operating system support

The short version: **a participating site runs Linux; everyone else can use any
operating system.** The Linux requirement comes from three specific container
features, not from the application, and it is about the container runtime rather
than about a particular distribution.

## By role

| Role | Requirement | Why |
|---|---|---|
| **Site running training** (FL client) | Linux | GPU passthrough and host networking (below) |
| **Coordinating site** (FL server, platform services) | Linux | Same container features; no GPU needed |
| **Clinician / analyst using the interface** | Any OS with a current browser | It is a web application |
| **Operator using the `swarmed` CLI** | Linux | Runs inside the platform's own environment |

## What is genuinely OS-dependent

1. **GPU passthrough.** Training containers get GPUs through the NVIDIA Container
   Toolkit, which is Linux-only. Note that the platform's GPU auto-detection looks
   for a Docker daemon advertising a runtime named `nvidia`; installations that
   expose GPUs a different way (for example CDI-only setups) are not detected and
   fall back to CPU. Set `SWARMMEDHUB_FORCE_GPU=true` in that case.
2. **Host networking.** FL client containers are started with `--network host`,
   which behaves as intended only on Linux. On Docker Desktop for macOS and
   Windows the containers run inside a virtual machine, so host networking does
   not give the container the host's address, and a client cannot be reached at
   the address written into its startup kit.
3. **Docker-in-Docker sandbox.** Site validation and visualisation scripts run in
   a nested Docker daemon with resource limits and a restricted network. This
   relies on Linux cgroups.

Everything else — the Django application, the database, object store, queue, the
web interface, the CLI logic — is ordinary portable Python.

## Prerequisites (any Linux distribution)

Rather than naming distributions, check these:

| Component | Requirement |
|---|---|
| Kernel | 5.4 or newer, cgroup v2 |
| Docker Engine | 24.0 or newer, with the Compose plugin v2 |
| NVIDIA driver | 535 or newer for the worked example's CUDA 12 runtime |
| NVIDIA Container Toolkit | 1.13 or newer, only at sites that train |
| Python | 3.12, only if you run the CLI or the helper scripts outside containers |

Any distribution that meets these works: Ubuntu, Debian, RHEL and derivatives such
as Rocky and AlmaLinux, Fedora, SLES. The installation guide's commands are
written with `apt`; on RPM-based systems substitute `dnf` and the corresponding
Docker and NVIDIA repositories. Nothing in the application depends on the package
manager.

## Where this has actually been run

| Host role | Distribution | Kernel | Docker | Toolkit |
|---|---|---|---|---|
| Platform + FL server | Ubuntu 24.04 LTS | 6.17 | 29.5 | — |
| Training site | Ubuntu 22.04 LTS | 5.14 | 29.8 | 1.20 |
| Training site | Ubuntu 22.04 LTS | 5.14 | 29.8 | 1.13 |
| Training site | Ubuntu 24.04 LTS | 7.0 | 28.4 | 1.17 |

Two Ubuntu releases, four kernel series from 5.14 to 7.0, and three Docker and
toolkit versions — the deployment is not sensitive to these. RPM-based
distributions have not yet been exercised end to end; we expect no difference
given the prerequisites above, and would welcome a report.

## macOS and Windows

* **Using the interface: fully supported.** SwarmMedHub is a web application;
  any current browser on any operating system works. Clinicians at a participating
  site do not need Linux.
* **Running a site: not supported.** Host networking and GPU passthrough do not
  work as required under Docker Desktop. A Windows host with WSL2 and GPU
  passthrough is closer to a Linux host and may work, but has not been tested;
  treat it as unvalidated.
* **Development and CLI use:** possible under WSL2; the CLI is Linux-first.

If your institution's only available hardware is macOS or Windows, the practical
arrangement is a small Linux virtual machine or server for the site's client and
data, with clinicians using the interface from their usual desktops.
