---
title: Installation
description: Installation instructions for MedSwarmHub.
---

# 🛠️ Installation

This page provides detailed instructions for installing the MedSwarmHub platform.

## 📋 System Requirements

Before you begin, ensure that your system meets the following requirements.

### 💻 Operating System

*   **Recommended:** Linux (Ubuntu 22.04 LTS or newer)
    *   The installation guide uses `apt` commands typical for Debian/Ubuntu environments.
*   **Supported:** macOS, Windows 10/11 (via Docker Desktop)
    *   *Note: Windows users are recommended to use WSL2 (Windows Subsystem for Linux) to ensure compatibility with helper scripts.*

### 🏗️ Hardware Resources

These specifications are for the **MedSwarmHub platform** services only.

*   **Minimum:**
    *   **CPU:** 2 Cores
    *   **RAM:** 4 GB
    *   **Storage:** 20 GB free space

*   **Recommended:**
    *   **CPU:** 4+ Cores
    *   **RAM:** 8 GB+
    *   **Storage:** 50 GB+ (SSD recommended)

!!! note "Machine Learning Workloads"
    If you intend to run actual **Machine Learning training** on this node (acting as a Training Client), you will need additional resources:
    
    *   **RAM:** 16 GB - 64 GB+ (dependent on model/batch size)
    *   **GPU:** NVIDIA GPU with CUDA support (strongly recommended)

## 🐳 1. Docker

Install Docker and Docker Compose by following the official documentation for your platform:

[https://docs.docker.com/engine/install/](https://docs.docker.com/engine/install/)

## 🌐 2. VPN Network (Tailscale)

For secure communication between the participants in the decentralized learning network, we recommend using a VPN like Tailscale.

### 🛠️ Installation

``` bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
```

``` bash
sudo apt update
sudo apt install tailscale
```

### 🔑 Login

Login with your credentials:

``` bash
sudo tailscale up
```

### 📡 Test Connectivity

Test your connectivity:

``` bash
tailscale ip -4
```

### 📂 Troubleshooting

If you have issues with DNS, you can try the following:

``` bash
tailscale set --accept-dns=false
systemctl restart tailscaled
```

## 🏥 3. MedSwarmHub

### 📥 Clone the Repository

``` bash
git clone https://github.com/pfeifferis/SwarmCloud.git
cd SwarmCloud
```

### ⚙️ Environment Variables

MedSwarmHub uses environment variables for configuration and sensitive information. The platform uses an automated setup script to manage these.

Run the following command to bootstrap your environment:

```bash
make setup
```

The `setup` target performs several key actions:
1.  **Dependency Sync:** Installs `uv` and synchronizes Python dependencies into a local virtual environment.
2.  **Interactive .env Generation:** Runs `scripts/setup_env.py` to create your `.env` file. It will prompt you for configuration values and generate secure random secrets for:
    *   **SECRET_KEY:** Django's cryptographic signing key.
    *   **Database & Redis:** Secure passwords for PostgreSQL and Redis.
    *   **MinIO / S3:** Root credentials and KMS encryption keys.
    *   **Encryption Keys:** `FERNET_KEYS` and `BACKUP_ENCRYPTION_KEY` for data-at-rest protection.
3.  **Privacy Configuration:** Prompts for `PRIVACY_CONTROLLER_*` and `PRIVACY_CONTACT_*` variables used to populate the platform's Privacy Policy and Terms of Service.
4.  **PgBouncer Helper:** Generates SCRAM credentials and configuration for the database proxy.
5.  **TLS Certificate Generation:** Creates a local internal Certificate Authority (CA) and issues leaf certificates for all backend services (Postgres, Redis, MinIO, Nginx, Sandbox).

!!! tip "Custom Hostname"
    During setup, you can provide a `MEDSWARMHUB_HOSTNAME`. This name identifies your node on the **Network** page.

### 🚀 Build and Run

After the setup is complete, start the platform with:

```bash
make start
```

`make start` builds the Docker services and brings them up in the background. If you need to stop the stack, run `make stop`. Tail the `medswarmhub` logs with `make logs`.

!!! tip "Manual Database Creation"
    If you see errors indicating that the `medswarmhub` database does not exist, you can create it manually while the containers are running:
    ```bash
    docker exec -it postgres psql -U medswarmhub -d postgres -c "CREATE DATABASE medswarmhub;"
    ```

## 👤 4. Create Superuser

``` bash
make superuser
```

Follow the prompts to create your superuser account.

!!! info "Superuser"
    The `superuser` has full access to all features and settings in the MedSwarmHub platform like an `admin`.

## 🔐 5. Trusting the Internal Root CA

When accessing MedSwarmHub via `https://localhost:5085`, your browser will show a security warning because the SSL certificate is issued by your local, internal Root CA.

### 🍎 macOS
```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain .secrets/certs/internal/ca.crt
```

### 🪟 Windows (PowerShell as Admin)
```powershell
Import-Certificate -FilePath ".secrets\certs\internal\ca.crt" -CertStoreLocation Cert:\LocalMachine\Root
```

### 🐧 Linux (Ubuntu/Debian)
```bash
sudo cp .secrets/certs/internal/ca.crt /usr/local/share/ca-certificates/internal-ca.crt
sudo update-ca-certificates
```

## 📂 Troubleshooting

### 📝 Postgres/Redis "Permission Denied" for SSL Key

If a database container fails due to key file permissions:

```bash
# 🚀 For Postgres (UID 999)
sudo chown 999:999 .secrets/certs/internal/postgres.key
sudo chmod 600 .secrets/certs/internal/postgres.key

# 🚀 For Redis
chmod 644 .secrets/certs/internal/redis.key
```

### 📝 Missing `.secrets` Directory

If you encounter errors about missing files in `.secrets/`:

1.  **Stop and Clean:**
    ```bash
    make stop
    sudo rm -rf .secrets
    ```
2.  **Regenerate:**
    ```bash
    make setup
    ```

## 🗑️ Deinstallation

If you wish to remove MedSwarmHub and its associated data from your system:

### 1. Stop and Cleanup Environment
This will stop the containers, remove the virtual environment, caches, and generated secrets/certificates:
```bash
make deinstall
```

### 2. Remove Docker Images (Optional)
To also remove the base Docker images to free up disk space:
```bash
docker rmi $(docker images -q 'medswarmhub*')
```

### 3. Remove Tailscale (Optional)
If you no longer need Tailscale:
```bash
sudo apt remove tailscale
```
