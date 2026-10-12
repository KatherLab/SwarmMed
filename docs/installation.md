---
title: Installation
description: Installing SwarmMedHub and the swarmed CLI on a Linux host.
---

# Installation

This page installs SwarmMed on one host: the SwarmMedHub web interface with the
services it runs on (PostgreSQL, Redis, MinIO, nginx, a Celery worker and the
script sandbox), and the `swarmed` CLI, which runs inside the web interface's
application container. Every site that runs its own SwarmMedHub repeats these
steps. How the sites of a study reach each other is described in
[Networking](networking.md), and what each site has to trust in
[Threat model](threat-model.md).

## 📋 System Requirements

### Operating System

* **Linux** on every host that runs the FL server or a training client. GPU
  passthrough and host networking for the client containers need it; see
  [Operating systems](os-support.md), which also lists the prerequisites any
  distribution has to meet. The commands on this page use `apt`; on RPM-based
  systems use `dnf` and the corresponding Docker and NVIDIA repositories.
* **macOS and Windows:** using the web interface from a browser is supported.
  Running a training site under Docker Desktop is not supported, and running the
  coordinating site there is untested.

A fresh-install test on Ubuntu 24.04 ran `make install`, `make env` and
`make start`, reached the web interface, created a superuser, and ran
`project create`, `data import` and `network create` through the CLI inside the
application container.

### Hardware Resources

For the platform alone, without training, [Hardware](hardware.md) suggests:

* **Minimum:** 4 cores, 8 GB RAM, 100 GB disk, no GPU.
* **Recommended:** 8 cores, 16 GB RAM, 500 GB SSD.

Both are estimates from measurements on the worked example's coordinating host,
not tested configurations. The platform's containers used about 3.5 GiB of memory
there, the application image measured 19.9 GB and the Python environment that
`make install` creates 9.6 GiB.

!!! note "Sites that train"
    A site that trains also needs what its model needs; see
    [Hardware](hardware.md#training-compute-by-use-case). The breast-MRI worked
    example requires an NVIDIA GPU; it was run on 24 GB and 48 GB cards, and its
    runtime image takes 15.6 GB of disk.

### Software and Network Access

* **Docker Engine with the Compose plugin** (`docker compose`). The Makefile calls
  `docker` without `sudo`, so run it as a user who may use Docker (see Docker's
  [post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/)).
* **`git`, `make`, `curl` and `openssl`**, and `jq` for the Tailscale helper
  (step 2).
* **At sites that train on a GPU:** the NVIDIA driver and the NVIDIA Container
  Toolkit ([versions that have been run](os-support.md#prerequisites-any-linux-distribution)).
* **Internet access while installing.** `make install` downloads uv, Python 3.12
  and the Python packages. `make start` builds images from Docker Hub, PyPI, npm
  and Docker's package repository. MinIO no longer publishes community images
  (`minio/minio` no longer exists on Docker Hub), so the stack builds MinIO
  `RELEASE.2025-09-07T16-13-09Z` from its pinned upstream source
  (`infrastructure/minio/`). That build clones the source from `github.com`,
  which must be reachable at that point. Starting a network later also downloads
  a base image and Python packages, unless a prebuilt runtime image is configured
  ([Networking](networking.md#listening-ports-by-host-role)).
* **Ports.** The stack publishes the web interface on ports 5085, 443 and 80 on
  all of the host's addresses. Ports that Docker publishes are not filtered by
  host firewalls such as UFW; [Networking](networking.md#listening-ports-by-host-role)
  lists every port and how to restrict them.

## 1. 🐳 Docker

Install Docker Engine and the Compose plugin by following Docker's documentation:

[https://docs.docker.com/engine/install/](https://docs.docker.com/engine/install/)

Docker Desktop is not supported at sites that train
([Operating systems](os-support.md#macos-and-windows)).

## 2. 🌐 VPN Network (Tailscale)

Federated training does not need an overlay network: the participating sites only
have to reach the coordinating host's FL server port ([Networking](networking.md)).
Two features of the web interface do depend on Tailscale:

* A network created in the web interface uses this host's Tailscale address as
  the FL server's address. On a host without Tailscale, its startup kits contain no
  server address and remote sites cannot reach the server; create networks with
  the CLI and `--server-ip` instead ([CLI](cli.md)).
* A Hub recognises itself among a network's participants by its Tailscale
  address. Without it, the Hub sends no status to the other participants' Hubs and
  shows them as offline.

Install Tailscale on the hosts that will use these features. SwarmMed does not
install, configure or join Tailscale; joining a tailnet is a manual step.

### Installation

On Ubuntu (`$(lsb_release -cs)` inserts the release's codename):

``` bash
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/$(lsb_release -cs).tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list
```

``` bash
sudo apt update
sudo apt install tailscale
```

An installation guide for other platforms can be found in the [Tailscale documentation](https://tailscale.com/docs/install).

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

!!! warning "Tailscale CLI Access"
    `make start` launches a helper on the host, `scripts/tailscale_watcher.sh`,
    which runs `tailscale ip -4` and `tailscale status --json` every 30 s and
    writes the result to `tmp/tailscale_status.json`, where SwarmMedHub reads it.
    Check that `tailscale ip -4` prints this host's address before you run
    `make start`. The helper is not a service: after a reboot, start it again with
    `make tailscale`.

### Troubleshooting

If you have issues with DNS, you can try the following:

``` bash
tailscale set --accept-dns=false
sudo systemctl restart tailscaled
```

## 3. 🧩 SwarmMed Repository

### Clone the Repository

``` bash
git clone https://github.com/KatherLab/SwarmMed.git
cd SwarmMed
```

### Install Python Dependencies

``` bash
make install
```

`make install` installs [uv](https://docs.astral.sh/uv/) into `~/.local/bin` if
it is missing, then Python 3.12, and creates the project environment `.venv` on
the host. `make env`, the PgBouncer helper and `make docs-serve` run in it. The
web interface, the worker and the `swarmed` CLI run in the application
containers instead, which have their own Python environment.

### Environment Variables

SwarmMedHub reads its configuration and secrets from a `.env` file, which
`make env` creates from `.env.template`:

```bash
make env
```

It generates random values for `SECRET_KEY`, the PostgreSQL, Redis and MinIO
passwords, the MinIO encryption key (`MINIO_KMS_SECRET_KEY`), `FERNET_KEYS` and
`BACKUP_ENCRYPTION_KEY`, and sets `DOCKER_HOST_IP` (below). It asks for the
other values:

* **`DJANGO_ALLOWED_HOSTS`** and **`DJANGO_CSRF_TRUSTED_ORIGINS`.** List every
  name and address users will type into their browsers, and the address this
  host is given as a participant when a network is provisioned; the trusted
  origins also need the scheme and port, for example `https://site-a.example.org:5085`.
  A Hub rejects requests for names that are not listed, including other Hubs'
  status messages ([Networking](networking.md#traffic-between-hubs)).
* **`PUBLIC_URL`**, the object store's address used in download links.
* **`HOST_PROJECT_PATH`**: the absolute path of this directory on the host. The
  containers use it to translate paths for the containers they start.
* **`SWARMMEDHUB_HOSTNAME`**: the name shown for this site on the **Network**
  page, and the participant name this host gets when it adds itself as a
  training client to a network it creates. If it matches the participant name the
  site is given at provisioning, the Hub can also find its own kit in a startup
  package by it.
* `DEBUG` (keep `False`), the S3 region name, the organisation, contact,
  editorial and privacy details shown on the imprint, privacy and contact pages,
  the e-mail server settings, and `ALLOWED_EXTENSIONS` (the file types accepted
  for data import). `EMAIL_HOST` is the SMTP server's host name, although the
  prompt describes it as a port.

The defaults offered for `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` and
`PUBLIC_URL` contain example addresses (`192.168.33.107`, `100.127.11.1`); replace
them with this host's own. For `SWARMMEDHUB_HOSTNAME`, enter a name: if you leave
it empty, the placeholder `replace-with-your-custom-hostname` is stored as the
name, and `make env` asks again on its next run.

`DOCKER_HOST_IP` is the address on which MinIO's ports are published. On Linux,
`make env` sets it to the gateway address of Docker's default bridge network as
Docker reports it (usually 172.17.0.1, which is also the fallback when Docker
cannot be queried); on macOS and Windows it sets 127.0.0.1.

When `.env` already exists, `make env` keeps the values set in it and asks only
for those that are empty or still hold a placeholder. It writes the file readable
and writable only by its owner (mode 600), and sets an existing `.env` to that
mode when it rewrites it.

!!! warning "Variables that are not in the template"
    `make env`, which every `make start` runs again, rewrites `.env` from
    `.env.template` and drops any variable that the template does not contain.
    Put other settings, such as the port and start-mode variables described in
    [Networking](networking.md), in `docker-compose.override.yml`.

!!! note "Installations from before 9 October 2026"
    Earlier versions of `make env` wrote `.env` with the default file mode, which
    under common umask settings lets every local user read it; run
    `chmod 600 .env`, or `make env`. Application images built before that date
    also contain `.env`, `workspaces/` (the startup kits, private keys included)
    and `results/`; the next `make start` rebuilds them without these, after which
    delete the old images ([Security](security.md#secrets-and-start-up-checks)).

### Build and Run

After the env is generated, start the platform with:

```bash
make start
```

`make start` runs, in order: `make install`, `make env`, the PgBouncer
configuration script, `make generate-certs`, the Tailscale helper,
`docker compose build` and `docker compose up -d`.

* **CA passphrase.** `make generate-certs` creates the platform's internal
  certificate authority (CA) and the service certificates for MinIO, PostgreSQL,
  PgBouncer, Redis and nginx, and asks for a passphrase that protects the CA's
  private key (`Enter passphrase for the internal CA key:`). For an unattended
  installation, supply it in the environment instead:
  `CA_PASSPHRASE='…' make start`. Every later `make start`, and `make restart`,
  does the same again: it replaces the CA and all service certificates and asks
  for a passphrase for the new key ([Security](security.md#internal-tls-private-pki)).
* **MinIO.** The first build compiles MinIO from source, as described above.
* **Restarts.** Every service has the restart policy `always`, so Docker starts
  the stack again when the Docker daemon starts after a reboot. The Tailscale
  helper is not restarted; run `make tailscale`.

`make stop` stops the stack and removes its containers; the database and
object-store volumes are kept, and nothing restarts until the next `make start`.
`make logs` follows the logs of the application and the worker
(`docker compose logs -f app celery_worker`); `docker compose logs -f` follows
every service.

## 4. 👤 Create Superuser

To access the SwarmMedHub dashboard, you need to create a superuser account:

``` bash
make superuser
```

!!! info "Superuser"
    The `superuser` has full access to all features and settings in the SwarmMedHub platform like an `admin`.

Open the web interface at `https://<host>:5085`, where `<host>` is a name or
address listed in `DJANGO_ALLOWED_HOSTS`. Port 443 serves the same site, and
plain HTTP on port 80 redirects to port 5085. Expect a certificate warning
(next section).

Check that the CLI reaches the database, with the superuser's name:

```bash
docker exec -w /app swarmmedhub python3 -m swarmed_cli.main project list --user <username>
```

On a SwarmMedHub host the CLI runs inside the application container like this.
`uv run swarmed …` on the host fails with the generated `.env`, which names the
database host `postgres`, a name that resolves only inside the Docker network;
see [CLI](cli.md).

## 5. 🔐 Trusting the Internal Root CA

The web interface's certificate is issued by the installation's internal CA
(`.secrets/certs/internal/ca.crt`) for `localhost` and `127.0.0.1` only, so
browsers warn about it:

* On the host itself, a browser that trusts the internal CA shows no warning
  for `https://localhost:5085`.
* On other computers, which reach the host by its name or address, the warning
  remains even with the CA trusted, because the certificate does not name the
  host. SwarmMed has no setting for the names in the certificate.

Every `make start` creates a new CA, so repeat this step after each one.

### Linux (Ubuntu/Debian)

```bash
sudo cp .secrets/certs/internal/ca.crt /usr/local/share/ca-certificates/internal-ca.crt
sudo update-ca-certificates
```

This adds the CA to the system trust store, which tools such as `curl` use.
Browsers can keep their own certificate store and then need the CA imported
there.

## 📂 Troubleshooting

### Docker Logs

To follow the application and worker logs:

```bash
make logs
```

For every service, run `docker compose logs -f` in the repository directory.

## 🗑️ Deinstallation

If you wish to remove SwarmMed and its associated local data from your system:

### 1. Stop and Cleanup Environment

!!! danger "`make deinstall` deletes data and Docker images without asking"
    Export everything you need to keep before running it. `make deinstall`:

    * stops the stack and deletes its containers, networks and volumes
      (`docker compose down --remove-orphans --rmi local -v`). This deletes the
      PostgreSQL volume, with every user, project, network, training job and the
      audit log, and the MinIO volume, with all imported data, project code and
      synchronised results;
    * deletes the application and worker images that the stack built;
    * runs `docker builder prune -af` and `docker image prune -af`, which delete
      all build caches and **every image on the host that no container uses**, not
      only SwarmMed's: for example training runtime images and other deployments'
      images;
    * deletes `.venv`, `.cache/uv`, `staticfiles`, `_build`, `tmp` and
      `workspaces/`, which holds every network's startup kits and private keys and
      the FL workspaces with their training results;
    * deletes the generated certificates and keys in `.secrets/certs`,
      `.secrets/docker` and `.secrets/pgbouncer`.

    It does not delete `.env`, which holds the deployment's passwords and keys,
    `media/`, `results/`, the rest of the source directory, uv and the Python it
    installed in your home directory, or Tailscale.

```bash
make deinstall
```

`make deinstall` deletes these directories as your user and ignores errors.
Files that the containers created as root or as another user can therefore
remain, in `workspaces/` among them, where they can include private keys. Check
what is left and remove it with `sudo`.

`make deinstall` also does not stop the Tailscale helper that `make start`
launched; stop it with `pkill -f tailscale_watcher.sh`.

### 2. Remove the Source Directory (Optional)

To also remove the code, and with it `.env` and anything else left in the
directory, run from its parent directory:

```bash
sudo rm -r SwarmMed
```

Docker creates some files in it as root, for example `infrastructure/certbot/`,
hence `sudo`.

### 3. Remove Tailscale (Optional)

If you no longer need Tailscale:

```bash
sudo apt remove tailscale
```
