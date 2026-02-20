---
title: Installation
description: Installation instructions for SwarmCloud.
---

# Installation

This page provides detailed instructions for installing the SwarmCloud platform.

## System Requirements

Before you begin, ensure that your system meets the following requirements.

### Operating System

*   **Recommended:** Linux (Ubuntu 22.04 LTS or newer)
    *   The installation guide uses `apt` commands typical for Debian/Ubuntu environments.
*   **Supported:** macOS, Windows 10/11 (via Docker Desktop)
    *   *Note: Windows users are recommended to use WSL2 (Windows Subsystem for Linux) to ensure compatibility with helper scripts.*

### Hardware Resources

These specifications are for the **SwarmCloud platform** services only.

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

### Custom Hostname

By default, SwarmCloud will generate a random, human-friendly hostname (e.g., `brave-lion`) for your node when it first starts. You can customize this hostname in two ways:

1.  **Environment Variable:** Add `SWARMCLOUD_HOSTNAME=your-custom-name` to your `.env` file.
2.  **Persistent File:** Create a file named `.swarmcloud_hostname` in the project root containing your desired name:
    ```bash
    echo "my-custom-node-name" > .swarmcloud_hostname
    ```

The hostname is displayed on the **Network** page and helps other participants identify your node in the decentralized network.

Please ensure that you **never** commit your `.env` file to version control.

### Prepare Secret Directories

The compose stack expects a few host directories where generated secrets will be written. Create them before launching the containers:

```bash
mkdir -p .secrets/certs .secrets/docker .secrets/pgbouncer
```

!!! warning "Directory vs Files"
    Only create the top-level directories listed above. **Do not** create subdirectories named after individual certificates (like `ca.crt`), as this will prevent the generation scripts from writing the actual certificate files.

#### Generate Internal TLS Certificates

Run the provided script to generate the internal Certificate Authority and service-specific certificates:

```bash
# You can set a passphrase for the CA or leave it empty for the prompt
export CA_PASSPHRASE=yoursecurepassphrase
./scripts/generate_internal_certs.sh
```

- `.secrets/certs` will store the internal CA and leaf certificates.
- `.secrets/docker` is populated automatically with TLS material for the sandbox daemon.
- `.secrets/pgbouncer` receives PgBouncer credentials.

#### Pre-render PgBouncer Configuration

You can pre-render the PgBouncer artifacts to catch configuration mistakes early:

```bash
python scripts/setup_pgbouncer.py
```

!!! info "PgBouncer Setup Script"
    The script reads your `DB_USER`, `DB_PASS`, and `DB_NAME` from the `.env` file to generate secure SCRAM-hashed credentials. 
    
    If the containers are not already running, you will see an error message at the end: `Error response from daemon: cannot kill container: pgbouncer: No such container`. **This is normal and safe to ignore**; it simply means the script couldn't signal a running container to reload its configuration. The files themselves are generated correctly.

### Build and Run

``` bash
docker compose build
docker compose up -d
```

!!! tip "Manual Database Creation"
    If you see errors indicating that the `swarmcloud` database does not exist, you can create it manually while the containers are running:
    ```bash
    docker exec -it postgres psql -U swarmcloud -d postgres -c "CREATE DATABASE swarmcloud;"
    ```

The first startup may take a little longer because:

1. `sandbox-dind` generates a private CA and client certificates under `.secrets/docker/`.
2. `tailscale-status` boots alongside your host Tailscale daemon to serve read-only status metrics.
3. The main `app` container now waits for the sandbox daemon before applying migrations, ensuring all background jobs have a secure Docker endpoint.

## 4. Create Superuser

``` bash
docker exec -it swarmcloud python manage.py createsuperuser
```

Follow the prompts to create your superuser account.

!!! info "Superuser"
    The `superuser` has full access to all features and settings in the SwarmCloud platform like an `admin`.

## 5. Trusting the Internal Root CA

When accessing SwarmCloud via `https://localhost:5085`, your browser will show a security warning ("Your connection is not private") because the SSL certificate is issued by an internal, untrusted Certificate Authority (CA).

To resolve this and see the "green lock," you must trust the Root CA on your system.

### macOS
Run the following command in your terminal:
```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain .secrets/certs/internal/ca.crt
```
Alternatively, open `.secrets/certs/internal/ca.crt` in **Keychain Access**, double-click the **InternalCA** certificate, and set **Trust** to **Always Trust**.

### Windows (PowerShell)
Run as Administrator:
```powershell
Import-Certificate -FilePath "infrastructure\certs\internal\ca.crt" -CertStoreLocation Cert:\LocalMachine\Root
```

### Linux (Ubuntu/Debian)
```bash
sudo cp .secrets/certs/internal/ca.crt /usr/local/share/ca-certificates/internal-ca.crt
sudo update-ca-certificates
```
*Note: You may also need to import the certificate manually into your browser settings (e.g., Firefox Settings -> Privacy & Security -> Certificates -> View Certificates -> Authorities -> Import).*

### Chrome/Edge "Secret" Bypass
If you want to skip the installation and just bypass the error screen:
1. Click anywhere on the error page.
2. Type `thisisunsafe` on your keyboard.
3. The page will reload and grant access.

## Troubleshooting

### Postgres "Permission Denied" for SSL Key

If the `postgres` container fails to start with logs indicating `FATAL:  private key file ".../server.key" has group or world access` or `Permission denied`, it means the file permissions on the host are too open or owned by the wrong user.

1.  **Stop containers:**
    ```bash
    docker compose down
    ```
2.  **Fix permissions on the host:**
    The private key must be owned by the user ID Postgres uses inside the container (UID 999) and have strict permissions (`0600`).
    ```bash
    # Set ownership to uid 999 (postgres user)
    sudo chown 999:999 .secrets/certs/internal/postgres.key

    # Set permissions to 0600 (read/write only for owner)
    sudo chmod 600 .secrets/certs/internal/postgres.key
    ```
3.  **Restart containers:**
    ```bash
    docker compose up -d
    ```

### redis "Permission Denied" for SSL Key

chmod 644 ./.secrets/certs/internal/redis.key


### "Database does not exist" after fixing keys

If the Postgres container repeatedly crashed due to SSL key issues during its first run, the initialization scripts (which create the `swarmcloud` database) might have been skipped because the data directory was partially initialized.

To fix this, you must wipe the corrupt database volume and start fresh:

1.  **Stop and remove volumes:**
    ```bash
    # WARNING: This deletes all database data!
    docker compose down -v
    ```
2.  **Start fresh:**
    ```bash
    docker compose up -d
    ```

### Missing `.secrets` Directory

If you encounter errors about missing files in `.secrets/` (e.g., `mount: .../pgbouncer/userlist.txt: not a directory`):

1.  **Clean up incorrect directories:**
    If you ran `docker compose up` before generating secrets, Docker may have created empty directories where files should be.
    ```bash
    docker compose down
    sudo rm -rf .secrets
    ```
2.  **Regenerate secrets:**
    ```bash
    mkdir -p .secrets/certs .secrets/docker .secrets/pgbouncer
    ./scripts/generate_internal_certs.sh
    python3 ./scripts/setup_pgbouncer.py
    ```

