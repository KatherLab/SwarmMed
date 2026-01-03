---
title: Installation
description: Installation instructions for SwarmCloud.
---

# Installation

This page provides detailed instructions for installing the SwarmCloud platform.

## Hardware Requirements

Before you begin, ensure that your hardware meets the following requirements:

*   **CPU:** 4 cores or more
*   **RAM:** 16 GB or more
*   **Storage:** 100 GB of free disk space
*   **GPU:** NVIDIA GPU with CUDA support (optional, for training jobs)
*   **Network:** Stable internet connection
*   **Operating System:** Ubuntu 20.04 LTS or later

## 1. Docker

Install Docker and Docker Compose by following the official documentation for your platform:

[https://docs.docker.com/engine/install/](https://docs.docker.com/engine/install/)

## 2. VPN Network (Tailscale)

For secure communication between the participants in the decentralized learning network, we recommend using a VPN like Tailscale.

### Installation

``` bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
```

``` bash
sudo apt update
sudo apt install tailscale
```

### Login

Login with your credentials:

``` bash
sudo tailscale up
```

### Test Connectivity

Test your connectivity:

``` bash
tailscale ip -4
```

### Troubleshooting

If you have issues with DNS, you can try the following:

``` bash
tailscale set --accept-dns=false
systemctl restart tailscaled
```

## 3. SwarmCloud

### Clone the Repository

``` bash
git clone https://github.com/pfeifferis/SwarmCloud.git
cd SwarmCloud
```

### Environment Variables

SwarmCloud uses environment variables for configuration and sensitive information. Copy the provided `.env.template` file to create your local `.env` file:

``` bash
cp .env.template .env
```

Open the `.env` file and fill in the required values. Key sections include:

*   **SECRET_KEY:** A unique random string for cryptographic signing.
*   **Database Settings:** Credentials for PostgreSQL and Redis.
*   **MinIO / S3 Settings:** Credentials and endpoint for object storage.
*   **Encryption Keys:** `FERNET_KEYS` and `BACKUP_ENCRYPTION_KEY` used for data-at-rest protection.

!!! tip "Generate Secure Keys"
    You can generate secure Fernet keys using Python:
    ```bash
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ```

Please ensure that you **never** commit your `.env` file to version control.

### Build and Run

``` bash
docker compose build
docker compose up -d
```

## 4. Create Superuser

``` bash
docker exec -it swarmcloud python manage.py createsuperuser
```

Follow the prompts to create your superuser account.

!!! info "Superuser"
    The `superuser` has full access to all features and settings in the SwarmCloud platform like an `admin`.

