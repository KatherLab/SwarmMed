# MedSwarmHub

> [!WARNING]
> **Research Use Only:** This software is intended for research purposes only and is **not** a medical device. It has not been cleared or approved by any regulatory authority (e.g., FDA, EMA) for clinical use. The developers and contributors take no responsibility or liability for any clinical decisions made based on results obtained from this software.

MedSwarmHub is a decentralized medical data storage and collaborative training platform. It leverages **Swarm Learning** (via NVIDIA FLARE) to enable privacy-preserving machine learning across distributed medical institutions without the need to move raw data.

## 🧠 Supported Frameworks

MedSwarmHub is framework-agnostic and provides a built-in adapter for all major machine learning libraries:

- **PyTorch** & **PyTorch Lightning**
- **TensorFlow** & **Keras**
- **Scikit-learn**
- **HuggingFace Transformers**
- **MONAI** 

## 🚀 Installation & Setup

### 1. Prerequisites
Ensure you have [Docker](https://docs.docker.com/engine/install/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.

### 2. VPN Network (Tailscale)
MedSwarmHub uses Tailscale for secure peer-to-peer networking.

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
git clone https://github.com/pfeifferis/MedSwarmHub.git
cd MedSwarmHub   
```

Then rely on the Makefile so you no longer run the manual prep scripts directly:

```bash
make install        # install uv and sync Python dependencies
make env            # generate .env file with secure random secrets
make start          # build the Docker services and bring them up
```

### 4. Initialize Superuser
```bash
make superuser
```

## Documentation
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

### Mac error
`Error response from daemon: ports are not available: exposing port TCP 172.17.0.1:9001 -> 127.0.0.1:0: listen tcp4 172.17.0.1:9001: bind: can't assign requested address make: *** [compose-up] Error 1`

```bash
sudo ifconfig lo0 alias 172.17.0.1
```
make 
remove the alias after stopping the application:

```bash
sudo ifconfig lo0 172.17.0.1 -alias
```


### Docker Logs
```bash
make logs
```

