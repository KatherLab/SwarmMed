# SwarmCloud

> [!WARNING]
> **Research Use Only:** This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmCloud is a decentralized medical data storage and collaborative training platform. It leverages **Swarm Learning** (via NVIDIA FLARE) to enable privacy-preserving machine learning across distributed medical institutions without the need to move raw data.

---

## 🚀 Installation & Setup

### 1. Prerequisites
Ensure you have [Docker](https://docs.docker.com/engine/install/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.

### 2. VPN Network (Tailscale)
MediSwarmCloud uses Tailscale for secure peer-to-peer networking.

```bash
# Install Tailscale (example for Ubuntu)
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/oracular.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt update && sudo apt install tailscale

# Login and activate
sudo tailscale up
```

### 3. Deploy Application
```bash
git clone https://github.com/pfeifferis/MediSwarmCloud.git
cd MediSwarmCloud

# Setup environment variables
cp .env.template .env
# Edit .env with your secrets

# Build and start containers
docker compose build
docker compose up -d
```

### 4. Initialize Superuser
```bash
docker exec -it swarmcloud python manage.py createsuperuser
```

---
### Documentation
```bash
mkdocs serve --dev-addr localhost:9999
```

---

## 🔒 Security

For more details on our security policies, see [SECURITY.md](SECURITY.md).

---

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for detailed coding standards, project architecture, and workflow instructions.

---

## 📂 Troubleshooting

### No Tailscale Connection
If you encounter connectivity issues with the VPN:
```bash
tailscale set --accept-dns=false
sudo systemctl restart tailscaled
```

### Docker Logs
```bash
docker compose logs -f swarmcloud
```