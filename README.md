# 🏥 SwarmMed

> [!WARNING]
> **Research Use Only:** This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmMed is a platform for decentralized (swarm) learning on medical data, built on the client-controlled swarm workflow of [NVIDIA FLARE](https://github.com/NVIDIA/NVFlare). Each site keeps its data and trains on it locally; the sites exchange model parameters through a coordinating FL server, and in each round one site, drawn at random, aggregates them.

This repository contains the **SwarmMedHub** web interface and the **`swarmed`** command-line interface (CLI). Both call the same Django services and use the same database, object storage, task queue and NVFlare runtime, so a project's data, network, training and results can be handled through either. This README describes SwarmMed 1.0.0; the full documentation is in [`docs/`](docs/index.md).

## 🧠 Frameworks

The example training scripts exchange model weights with NVIDIA FLARE through a small adapter (`flare_adapter`) that converts them to NumPy arrays, which keeps the exchange independent of the machine-learning library. The repository includes example projects for:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **Hugging Face Transformers**
- **MONAI**

Only PyTorch has been run end to end with SwarmMed so far: the tabular example in `examples/pytorch`, in the [scalability benchmark](docs/scalability.md), and the breast-MRI worked example with PyTorch Lightning ([reproduction](docs/reproduce.md)). The other examples are provided but not validated. Jobs for TensorFlow and Keras are generated with NVFlare's PyTorch model persistor ([SwarmMed and NVIDIA FLARE](docs/nvflare-comparison.md)).

## 🛠️ Installation & Setup

The [installation guide](docs/installation.md) describes each step and what it changes on the host.

### 📋 1. Prerequisites

- **Linux** on every host that runs the FL server or trains ([operating systems](docs/os-support.md)); people who only use the web interface can use any operating system with a current browser.
- **Hardware** for the platform alone: at least 4 cores, 8 GB RAM and 100 GB disk; sites that train need what their model needs, for the breast-MRI example an NVIDIA GPU ([hardware](docs/hardware.md)).
- [Docker Engine](https://docs.docker.com/engine/install/) with the [Compose plugin](https://docs.docker.com/compose/install/), usable without `sudo`, and `git`, `make`, `curl`, `openssl` and `jq`. Sites that train on a GPU also need the NVIDIA driver and the NVIDIA Container Toolkit.
- **Internet access during installation, including `github.com`.** MinIO no longer publishes community images (`minio/minio` no longer exists on Docker Hub), so the stack builds MinIO `RELEASE.2025-09-07T16-13-09Z` from its pinned upstream source (`infrastructure/minio/`).

### 🌐 2. VPN Network (Tailscale)

Federated training does not need an overlay network, but two features of the web interface use Tailscale: a network created in the web interface takes this host's Tailscale address as the FL server's address, and a Hub recognises itself among a network's participants by it. Without Tailscale, create networks with the CLI and `--server-ip` ([networking](docs/networking.md)).

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
> SwarmMed does not install, configure or join Tailscale. `make start` launches a helper on the host that reads the address with `tailscale ip -4` every 30 seconds, so check that this command prints it. After a reboot, start the helper again with `make tailscale`.

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
- **Validation and visualization scripts** run in short-lived containers on a separate Docker daemon, limited to 1 CPU, 1 GB of memory and 5 minutes, but their network is not isolated: they can reach the internet and the platform's internal services, including the unauthenticated proxy to the host's Docker API. Treat them as trusted code.
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

### No Tailscale Connection
If you encounter connectivity issues with the VPN:
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

### 3. Remove Tailscale (Optional)
If you no longer need Tailscale:
```bash
sudo apt remove tailscale
```

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
