# 🏥 SwarmMed

> [!WARNING]
> **Research Use Only:** This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmMed is a secure and scalable platform for decentralized learning on medical data. This repository contains the **SwarmMedHub** web interface and the local **`swarmed`** companion CLI. Both interfaces share the same Django backend workflow, storage, task queue, and NVFlare runtime so teams can run the same core project, data, network, training, and results workflow through either interface.

## 🧠 Supported Frameworks

SwarmMedHub is framework-agnostic and provides a built-in adapter for all major machine learning libraries:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **HuggingFace Transformers**
- **MONAI** 

## 🛠️ Installation & Setup

### 📋 1. Prerequisites
Ensure you have [Docker](https://docs.docker.com/engine/install/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.

### 🌐 2. VPN Network (Tailscale)
SwarmMed uses Tailscale for secure peer-to-peer networking.

```bash
# 🌐 Install Tailscale (example for Ubuntu)
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt update && sudo apt install tailscale

# 🔑 Login and activate
sudo tailscale up
```

An installation guide for other platforms can be found in the [Tailscale documentation](https://tailscale.com/docs/install).

> [!WARNING]
> Please make sure Tailscale CLI is accessible in your terminal, as the setup scripts rely on it to configure the VPN network. Please verify the installation by running `tailscale status` before proceeding. To run it on macOS see [Tailscale CLI documentation](https://tailscale.com/docs/reference/tailscale-cli?tab=macos).

### 🚢 3. Deploy Application
Clone the repository and navigate to the project directory:
```bash
git clone https://github.com/KatherLab/SwarmMed.git
cd SwarmMed
```

Then rely on the Makefile to bootstrap the environment and start the services:

```bash
make install    # Install dependencies and initialize .venv
make env        # Create .env file with custom values
make start      # Build Docker services and bring the stack online
```

`make install` also installs the local `swarmed` CLI from this same repository. The CLI runs in the same Django environment as SwarmMedHub and is intended for Linux hosts in the current v1 release.

### 👤 4. Initialize Superuser
Create an admin account to access the web interface:
```bash
make superuser
```

## 📚 Documentation
Host the documentation locally for the best experience:
```bash
make docs-serve
```

## 💻 Local CLI

`swarmed` is installed from this same repository as a local companion CLI for SwarmMedHub. It uses the same Django environment, database, object storage, Celery workers, and current-project/current-network context as the web UI.

The CLI is intended for Linux hosts in v1. After `make install`, run it through the project environment:

```bash
uv run swarmed project create --user alice --title "Demo" --code-dir ./demo-code
uv run swarmed project use --user alice <PROJECT_UUID>
uv run swarmed data import --user alice --project <PROJECT_UUID> --dest incoming ./data.csv
uv run swarmed network create --user alice --project <PROJECT_UUID> --name "Local Test" --local-test
uv run swarmed training start --user alice --network <NETWORK_UUID>
uv run swarmed results sync --user alice --project <PROJECT_UUID>
```

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

## 🔐 Security Architecture Highlights

SwarmMedHub is designed from the ground up for maximum security in decentralized medical environments:

- **🔒 End-to-End TLS Encryption:** All internal and external traffic is secured via TLS, with an internal Certificate Authority (CA) managing service-to-service mutual TLS (mTLS).
- **🧠 NVFLARE & Swarm Learning:** Leverages NVIDIA FLARE for federated learning, ensuring raw medical data never leaves the local institution's premises.
- **🌐 Secure VPN Mesh:** Built-in integration with **Tailscale** creates a private, encrypted wireguard-based mesh network between all participants.
- **🏗️ Isolated Code Sandboxing:** User-submitted validation and visualization scripts run in highly restricted, ephemeral Docker containers with CPU/RAM limits to prevent resource exhaustion and data leakage.
- **🛡️ Secure Data Handling:** Uses locked-down **PgBouncer** for database proxying with SCRAM credentials and provides safe, encrypted backup restoration with path traversal protection.

These protections are enabled automatically when using the provided compose files. Review [SECURITY.md](SECURITY.md) for further details.

## 📂 Troubleshooting

### 🔒 "Your connection is not private" (SSL Warning)
Since the platform uses an internal Certificate Authority (CA) for `localhost`, your browser will show a warning. To resolve this:

**macOS:**
```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain .secrets/certs/internal/ca.crt
```

**Windows (PowerShell as Admin):**
```powershell
Import-Certificate -FilePath ".secrets\certs\internal\ca.crt" -CertStoreLocation Cert:\LocalMachine\Root
```

**Chrome/Edge Bypass:**
Type `thisisunsafe` anywhere on the warning page to bypass it without installing the certificate.

### No Tailscale Connection
If you encounter connectivity issues with the VPN:
```bash
tailscale set --accept-dns=false
sudo systemctl restart tailscaled
```

### 🐳 Docker Logs
To view real-time logs from all services:
```bash
make logs
```

## 🗑️ Deinstallation

If you wish to remove SwarmMed and its associated local data from your system:

### 1. Stop and Cleanup Environment
This will stop the containers, remove the virtual environment, caches, and generated secrets/certificates:
```bash
make deinstall
```

### 2. Remove Docker Images (Optional)
To also remove code to free up disk space:
```bash
sudo rm -r SwarmMed
```

### 3. Remove Tailscale (Optional)
If you no longer need Tailscale:
```bash
sudo apt remove tailscale
```

## 📄 License

SwarmMedHub is open source software, licensed under the [Apache License 2.0](LICENSE).

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
