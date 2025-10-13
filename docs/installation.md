---
title: Installation
description: Installation instructions for MediSwarmCloud.
---

# Installation

This page provides detailed instructions for installing the MediSwarmCloud platform.

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

## 3. MediSwarmCloud

### Clone the Repository

``` bash
git clone https://github.com/pfeifferis/MediSwarmCloud.git
cd MediSwarmCloud
```

### Environment Variables

Create a `.env` file in the root of the project and add the following variables:

``` bash
SECRET_KEY=your-secret-key
DEBUG=True

POSTGRES_DB=mediswarm
POSTGRES_USER=mediswarm
POSTGRES_PASSWORD=mediswarm
POSTGRES_HOST=db
POSTGRES_PORT=5432

AWS_ACCESS_KEY_ID=your-access-key-id
AWS_SECRET_ACCESS_KEY=your-secret-access-key
AWS_STORAGE_BUCKET_NAME=your-bucket-name
AWS_S3_REGION_NAME=your-region
AWS_S3_ENDPOINT_URL=your-s3-endpoint-url
```

Please replace the placeholder values with your actual configuration.

### Build and Run

``` bash
docker compose build
docker compose up -d
```

## 4. Create Superuser

``` bash
docker exec -it mediswarmcloud python manage.py createsuperuser
```

Follow the prompts to create your superuser account.

!!! info "Superuser"
    The superuser has full access to all features and settings in the MediSwarmCloud platform like an admin.

