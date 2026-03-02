# SwarmCloud

> [!WARNING]
> **Research Use Only:** This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

SwarmCloud is a decentralized medical data storage and collaborative training platform. It leverages **Swarm Learning** (via NVIDIA FLARE) to enable privacy-preserving machine learning across distributed medical institutions without the need to move raw data.

---

## 🚀 Installation & Setup

### 1. Prerequisites
Ensure you have [Docker](https://docs.docker.com/engine/install/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.

### 2. VPN Network (Tailscale)
SwarmCloud uses Tailscale for secure peer-to-peer networking.

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
git clone https://github.com/pfeifferis/SwarmCloud.git
cd SwarmCloud


# Prepare secret directories for TLS, PgBouncer, and Docker client certs
mkdir -p .secrets/certs .secrets/docker .secrets/pgbouncer
# Setup environment variables
cp .env.template .env
# Edit .env with your secrets

# (Optional) Pre-render PgBouncer config before containers start
python scripts/setup_pgbouncer.py || true

# Generate internal TLS material (stored in .secrets/ and not committed)
./scripts/generate_internal_certs.sh

# Build and start containers
docker compose build
docker compose up -d
```

### Tooling (uv)

SwarmCloud relies on uv for Python dependency management. The root Makefile installs uv when needed and synchronizes `requirements.txt` into `.venv`, so running the install target is all you need to provision the local Python tooling:

```bash
make install
```

Use the Makefile to run documentation helpers (`make docs-serve`, `make docs-build`) or docker shortcuts (`make compose-up`, `make compose-down`).

### 4. Initialize Superuser
```bash
docker exec -it swarmcloud python manage.py createsuperuser
```

---
### Documentation
```bash
make docs-serve
```

If you prefer to manage the MkDocs dependencies manually, install them with uv and run the server directly:

```bash
uv pip install mkdocs mkdocs-material
uv run mkdocs serve --dev-addr localhost:9999
```

---

## 🔐 Security Architecture Highlights

- **TLS-isolated sandboxing:** User code runs against the `sandbox-dind` service rather than the host Docker socket. Client certificates generated in `.secrets/docker/client` are mounted read-only and validated automatically by the entrypoint before any workloads run.
- **Read-only Tailscale telemetry:** The application now queries a dedicated `tailscale-status` sidecar over HTTP instead of mounting `/var/run/tailscale` directly, eliminating write access to the host VPN daemon.
- **Locked-down PgBouncer configuration:** `scripts/setup_pgbouncer.py` now writes SCRAM credentials and config files into `.secrets/pgbouncer/` with `0600` permissions and refuses to run with default passwords. Mounts in the compose files are read-only by default.
- **Safe backup restoration:** Encrypted backup archives are inspected for symlinks and path traversal before extraction, preventing crafted tarballs from overwriting files outside the restore directory.

These protections are enabled automatically when using the provided compose files, but you can review [SECURITY.md](SECURITY.md) for operational guidance.

---

## 🔒 Security

For more details on our security policies, see [SECURITY.md](SECURITY.md).

---

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for detailed coding standards, project architecture, and workflow instructions.

---

## 📂 Troubleshooting

### "Your connection is not private" (SSL Warning)
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

### Docker Logs
```bash
docker compose logs -f swarmcloud
```