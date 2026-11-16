# 🏥 SwarmMed

> [!WARNING]
> **Research Use Only:** This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmMed is a platform for decentralized (swarm) learning on biomedical data, built on the client-controlled swarm workflow of [NVIDIA FLARE](https://github.com/NVIDIA/NVFlare). Each site keeps its data and trains on it locally; the sites exchange model parameters through a coordinating FL server, and in each round one site, drawn at random, aggregates them.

This repository contains the **SwarmMedHub** web interface and the **`swarmed`** command-line interface (CLI). Both call the same Django services and use the same database, object storage, task queue and NVFlare runtime, so a project's data, network, training and results can be handled through either. This README describes SwarmMed 1.0.0; the full documentation is in [`docs/`](docs/index.md).

The platform does not interpret a study's data. A study is a code bundle: a `training.py`, which reads the site's files through a small adapter (`flare_adapter`), and optional scripts for data checks, plots and per-case explanations. Between sites the platform sends model weights, training metrics and status, not the files. The templates in [`examples/`](examples/README.md) are such bundles for different kinds of data ([Templates](docs/templates.md)); the breast MRI study ([reproduction](docs/reproduce.md)) is the worked example and, so far, the only run across several hosts.

## 🧠 Frameworks and templates

The example training scripts exchange model weights with NVIDIA FLARE through a small adapter (`flare_adapter`) that converts them to NumPy arrays, which keeps the exchange independent of the machine-learning library. The repository includes example projects for:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **Hugging Face Transformers**
- **MONAI**

Templates for six kinds of data (tabular clinical data, intensive-care EHR, 12-lead ECG, ECG with patient metadata, heart sound recordings and sleep EEG) are complete project folders: a PyTorch training script, a script that prepares a public dataset, data checks, plots and per-case explanations. [Templates](docs/templates.md) lists them with what was run for each.

Only PyTorch has been run end to end with SwarmMed so far: the tabular example in `examples/pytorch`, in the [scalability benchmark](docs/scalability.md), and the breast-MRI worked example with PyTorch Lightning ([reproduction](docs/reproduce.md)). The six templates were each run to completion in NVIDIA FLARE's simulator with the job SwarmMed builds for them, on one host with simulated sites (11 November 2026). On 11 and 12 November 2026 each template also trained to completion on a SwarmMed local-test network (all sites as containers on one host, CPU) on a fresh installation of SwarmMed, through the CLI steps in its README, and its data checks, result plots and explanations ran in the Hub's sandbox. The other framework examples are provided but not validated. Jobs for TensorFlow and Keras are generated with NVFlare's PyTorch model persistor ([SwarmMed and NVIDIA FLARE](docs/nvflare-comparison.md)).

## 🛠️ Installation & Setup

The [installation guide](docs/installation.md) describes each step and what it changes on the host.

### 📋 1. Prerequisites

- **Linux** on every host that runs the FL server or trains ([operating systems](docs/os-support.md)); people who only use the web interface can use any operating system with a current browser.
- **Hardware** for the platform alone: at least 4 cores, 8 GB RAM and 100 GB disk; sites that train need what their model needs: the templates and the tabular example ran on CPUs, and the breast-MRI example needs an NVIDIA GPU ([hardware](docs/hardware.md)).
- [Docker Engine](https://docs.docker.com/engine/install/) with the [Compose plugin](https://docs.docker.com/compose/install/), usable without `sudo`, and `git`, `make`, `curl`, `openssl` and `python3` (3.8 or later). Sites that train on a GPU also need the NVIDIA driver and the NVIDIA Container Toolkit.
- **Internet access during installation, including `github.com`.** MinIO no longer publishes community images (`minio/minio` no longer exists on Docker Hub), so the stack builds MinIO `RELEASE.2025-09-07T16-13-09Z` from its pinned upstream source (`infrastructure/minio/`).

### 🌐 2. Overlay Network (NetBird or Tailscale)

Federated training does not need an overlay network, but two features of the web interface use this host's overlay address: the network form proposes it as the FL server's address, and a Hub recognises itself among a network's participants by it. Without an overlay, enter the server address in the form or use the CLI's `--server-ip`; [networking](docs/networking.md#the-overlay-helper-and-the-hub) explains `SWARMMEDHUB_OVERLAY=none` and `SWARMMEDHUB_LOCAL_ADDRESSES` for that case.

The recommended option is [NetBird](https://netbird.io/) with a management server that the consortium hosts itself ([self-hosting quickstart](https://docs.netbird.io/selfhosted/selfhosted-quickstart)), so that no third party mediates the connections between the sites; [networking](docs/networking.md#netbird-recommended) describes the setup keys, groups and access policy for a study. NetBird Cloud ([app.netbird.io](https://app.netbird.io/)) is a quick way to try it. Tailscale, which the worked example used, optionally with a self-hosted Headscale control server, and WireGuard, OpenVPN or IPSec are the alternatives ([options](docs/networking.md#options)). NetBird's client is licensed under BSD-3-Clause; its management, signal and relay servers have been licensed under AGPL-3.0 since v0.53.0. With SwarmMed, NetBird has so far been exercised only in a containers-only test on one host ([validation status](docs/networking.md#validation-status)).

```bash
# 🌐 Install NetBird (the script uses NetBird's apt, dnf or yum repository when it finds one)
curl -fsSL https://pkgs.netbird.io/install.sh | sh

# 🔑 Join the consortium's network with a setup key from its management server
sudo netbird up --management-url https://netbird.example.org --setup-key <SETUP_KEY>
netbird status    # NetBird IP: 100.64.0.10/16; Management and Signal: Connected
```

For a trial on NetBird Cloud, leave out `--management-url`. Other platforms and package repositories are described in the [NetBird documentation](https://docs.netbird.io/get-started/install/linux). To use Tailscale instead:

```bash
# 🌐 Install Tailscale (example for Ubuntu; $(lsb_release -cs) is the release codename)
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt update && sudo apt install tailscale

# 🔑 Login and activate
sudo tailscale up
```

An installation guide for other platforms can be found in the [Tailscale documentation](https://tailscale.com/docs/install).

> [!NOTE]
> SwarmMed does not install, configure or join the overlay. `make start` launches a helper on the host, `scripts/overlay_watcher.py`, that records this host's NetBird and Tailscale addresses and states every 30 seconds; `SWARMMEDHUB_OVERLAY` in `.env` (`auto`, `netbird`, `tailscale` or `none`) selects whose address the Hub uses. After a reboot, start the helper again with `make overlay`.

> [!WARNING]
> On a Linux host that runs both NetBird and Tailscale, NetBird's traffic is likely to be dropped: according to Tailscale's documentation and code, its default firewall rule drops packets from 100.64.0.0/10 that do not arrive on Tailscale's interface, and NetBird's default addresses lie in that range. Neither this nor the remedies (a NetBird address range outside 100.64.0.0/10, Tailscale's `disable-linux-cgnat-drop-rule` node attribute, or `--netfilter-mode=nodivert`) have been tested with SwarmMed ([hosts that also run Tailscale](docs/networking.md#hosts-that-also-run-tailscale)).

### 🚢 3. Deploy Application
Clone the repository and navigate to the project directory:
```bash
git clone https://github.com/KatherLab/SwarmMed.git
cd SwarmMed
```

Then rely on the Makefile to bootstrap the environment and start the services:

```bash
make install    # Install uv, Python 3.12 and the host environment .venv
make env        # Create .env: generates secrets, asks for the other values
make start      # Generate certificates, build the images and start the stack
```

- `make env` writes `.env` readable only by its owner. Replace the example addresses it offers for `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` and `PUBLIC_URL` with this host's own names and addresses.
- `make start` creates the platform's internal certificate authority and asks for a passphrase to protect its private key. For an unattended installation, provide it in the environment instead of at the prompt: `CA_PASSPHRASE='…' make start`. Every later `make start` replaces the CA and the service certificates and asks again.
- Every service has the restart policy `always`, so the stack comes back when Docker starts after a reboot.

### 👤 4. Initialize Superuser
Create an admin account to access the web interface:
```bash
make superuser
```

Then open `https://<host>:5085` (port 443 serves the same site). Browsers warn about the certificate, which is issued by the internal CA for `localhost` and `127.0.0.1` only (see [Troubleshooting](#-troubleshooting)).

## 📚 Documentation
Serve the documentation locally at `http://localhost:9999` (it listens on all of the host's addresses while it runs):
```bash
make docs-serve
```

## 🗂️ Repository layout

| Path | Contents |
|---|---|
| `apps/` | SwarmMedHub's Django apps (projects, data, networks, training, results and explanations, users and others) |
| `core/` | Django settings, URLs and the Celery application |
| `swarmed_cli/` | The `swarmed` CLI |
| `swarmmed_explain/` | Explainability methods, plotting helpers and the bundle format that `explainability.py` writes ([README](swarmmed_explain/README.md)) |
| `examples/` | Templates for six kinds of data, framework examples on synthetic data and the breast MRI site scripts ([catalogue](examples/README.md)) |
| `experiments/duke_mst_cli/` | Code of the breast MRI worked example |
| `templates/`, `static/` | Pages and assets of the web interface |
| `docs/` | The documentation (MkDocs) |
| `scripts/`, `infrastructure/` | Host-side helpers (environment, certificates, overlay status) and the configuration of nginx, PostgreSQL, PgBouncer and MinIO |

## 💻 Local CLI

In SwarmMed 1.0.0, `swarmed` runs on the SwarmMedHub host, inside the application container, and uses the same database, object storage, Celery workers and current-project/current-network context as the web interface. It has so far been run only on Linux hosts. The host-side `uv run swarmed …` fails on a standard installation, because the generated `.env` names the database host `postgres`, which resolves only inside the Docker network; see the [CLI guide](docs/cli.md) for the overrides it needs.

```bash
swarmed() { docker exec -w /app swarmmedhub python3 -m swarmed_cli.main "$@"; }

swarmed project create --user alice --title "Demo" --code-dir ./demo-code
swarmed project use --user alice <PROJECT_UUID>
swarmed data import --user alice --project <PROJECT_UUID> --dest incoming ./data.csv
swarmed network create --user alice --project <PROJECT_UUID> --name "Local Test" --local-test
swarmed training start --user alice --network <NETWORK_UUID>
swarmed results sync --user alice --project <PROJECT_UUID>
```

The repository is mounted at `/app` in the container, so paths such as `--code-dir` must lie inside the repository directory.

Use `--json` on any command to get a stable envelope:

```json
{
  "ok": true,
  "command": "project list",
  "result": {},
  "warnings": [],
  "errors": []
}
```

## 🔐 Security Architecture

SwarmMed has not undergone an external security audit or penetration test. In brief:

- **Federated-learning traffic** between each site and the FL server uses mutual TLS, with certificates from a CA generated for each study. TLS protects each hop only: **the FL server relays every model update between sites and, in the default configuration, can read it.**
- **The startup package** that every site imports contains every participant's private key, the FL server's private key and, for each site, an admin identity that may run code at every site. SwarmMed is designed for consortia whose institutions trust each other, not for open participation.
- **Platform services** (PostgreSQL, Redis, MinIO and the web server) use TLS with certificates from the installation's own internal CA. The web interfaces of different sites exchange status over HTTPS without certificate verification by default.
- **An overlay network is optional.** The compose files ship no VPN, and the FL server's ports are published on all of the host's addresses.
- **Validation, visualization and explainability scripts** run in short-lived containers on a separate Docker daemon, limited to 1 CPU, 1 GB of memory and 5 minutes (explainability scripts by default to 2 CPUs, 2 GB and 15 minutes), but their network is only partly isolated: they can reach the internet, MinIO, the application and worker containers and any service the host publishes on a non-loopback address, but not the Docker socket proxy, PostgreSQL or Redis. Treat them as trusted code.
- **Raw data** is not part of any message the platform sends between sites, but the training code runs at every site with access to its data.

Read the [threat model](docs/threat-model.md), [security controls](docs/security.md) and [networking](docs/networking.md) pages before deploying, and [SECURITY.md](SECURITY.md) for how to report a vulnerability.

## 📂 Troubleshooting

### 🔒 "Your connection is not private" (SSL Warning)
The web interface's certificate is issued by the platform's internal certificate authority for `localhost` and `127.0.0.1` only. On the host itself, a browser that trusts the internal CA shows no warning for `https://localhost:5085`:

```bash
sudo cp .secrets/certs/internal/ca.crt /usr/local/share/ca-certificates/internal-ca.crt
sudo update-ca-certificates
```

Browsers on other computers, which reach the host by its name or address, show the warning even with the CA trusted, because the certificate does not name the host. Every `make start` creates a new CA, so trust the new `ca.crt` after each one. Details are in the [installation guide](docs/installation.md).

### No Overlay Connection
The **Network** page shows the address and state that the overlay helper last recorded, and marks them as stale when the helper has stopped; start it again with `make overlay`, and see `tmp/overlay_watcher.log` for the changes it recorded. For NetBird, `netbird status -d` shows whether the management and signal servers are reachable, and every peer that an access policy connects with this host. For Tailscale, if DNS stops working after joining:
```bash
tailscale set --accept-dns=false
sudo systemctl restart tailscaled
```

### 🐳 Docker Logs
To follow the logs of the application and the worker:
```bash
make logs
```

For every service, run `docker compose logs -f` in the repository directory.

## 🗑️ Deinstallation

If you wish to remove SwarmMed and its associated local data from your system:

### 1. Stop and Cleanup Environment

> [!CAUTION]
> `make deinstall` asks for no confirmation. It deletes the database and object-store volumes (every project, dataset, result and the audit log), `workspaces/` (every network's startup kits, private keys and training results), the host environment, the generated certificates, and the stack's images. It also runs `docker builder prune -af` and `docker image prune -af`, which delete all build caches and every image on the host that no container uses, including images of other projects. It keeps `.env`. Export what you need first; the [installation guide](docs/installation.md) lists exactly what it deletes and what it leaves.

```bash
make deinstall
```

### 2. Remove the Source Directory (Optional)
To also remove the code, `.env` and anything else left in the directory, run from its parent directory:
```bash
sudo rm -r SwarmMed
```

### 3. Remove the Overlay Client (Optional)
If you no longer need NetBird or Tailscale, remove the client (Debian and Ubuntu packages):
```bash
sudo apt remove netbird      # NetBird
sudo apt remove tailscale    # Tailscale
```
NetBird keeps the host's state, including its private key, in `/var/lib/netbird`; delete that directory too. Then remove the host in the NetBird dashboard or the Tailscale admin console.

## 📄 License

SwarmMed, including SwarmMedHub and the `swarmed` CLI, is open source software, licensed under the [Apache License 2.0](LICENSE).

```
Copyright 2026 KatherLab (https://kather.ai/)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0
```

See [LICENSE](LICENSE) for the full text and [NOTICE](NOTICE) for attribution requirements.

> **DISCLAIMER: RESEARCH USE ONLY.** This software is for research purposes only and is not a
> medical device. The developers take no responsibility or liability for clinical use.
