---
title: Security
description: Security controls implemented in SwarmMedHub, how they work, and their known limitations.
---

# Security

!!! warning "Pre-release Software"
    This platform has **not** undergone an external security audit or professional penetration test. Use it with appropriate caution in sensitive environments.

!!! warning "Shared Responsibility"
    The security of a decentralized learning setup also depends on the security of each participant's own infrastructure and on every participant following security best practices.

This page lists the security controls that SwarmMedHub implements, how each one works, and where it falls short. What leaves a site during training, which adversaries are considered and what is not implemented are described in the [Threat model](threat-model.md).

## 🏗️ Infrastructure & Network Security

A SwarmMedHub installation runs as a set of Docker containers on one host, connected by Docker networks.

### Internal TLS (Private PKI)

- **Root CA:** `make generate-certs`, which `make start` also runs, creates a private root CA and uses it to issue server certificates for MinIO, PostgreSQL, PgBouncer, Redis and the nginx web server. The CA's private key is stored encrypted with a passphrase that the script asks for, or takes from `CA_PASSPHRASE`. The CA certificate is valid for 10 years and the service certificates for 365 days. Nothing renews them automatically; running the target again, as every `make start` does, replaces the CA and every service certificate.
- **Server-Authenticated TLS:** The application and worker containers trust this CA and check the service certificates: Redis with `ssl_cert_reqs=required`, MinIO through `AWS_CA_BUNDLE`, and PostgreSQL with `sslmode=verify-full` when `DEBUG=False` (`sslmode=require` when `DEBUG=True`). Clients authenticate to these services with passwords or access keys, not with certificates (Redis runs with `--tls-auth-clients no`), so this is not mutual TLS.
- **TLS-Only Services:** PostgreSQL accepts TCP connections only over TLS (`hostssl` in `pg_hba.conf`), Redis listens only on its TLS port, PgBouncer requires TLS from its clients, and MinIO serves only HTTPS.
- **Unencrypted Hops:** nginx forwards requests to the application server over plain HTTP, and the application and worker containers reach the `docker-proxy` service (the host Docker API) over plain TCP on port 2375. Both hops run over the host's Docker networks.

### Host Docker Access

The application and worker containers start and stop the federated-learning containers through the `docker-proxy` service. It forwards their requests to the host's Docker socket with read and write access to the container, image, network, volume, build and exec APIs, and it does not authenticate callers. Control of the Docker API amounts to root access on the host, so anyone who can run code in the application or worker container, or reach port 2375 on `db_network`, controls the host.

### Script Sandbox

User-provided validation and visualization scripts run in short-lived containers:

- **Separate Docker Daemon:** The containers run on a dedicated Docker-in-Docker service, `sandbox-dind`, rather than on the host Docker daemon. `sandbox-dind` itself is a privileged container with the whole project directory, including `.env` and `.secrets/`, mounted.
- **Mutual TLS:** The application connects to the `sandbox-dind` daemon with a client certificate and checks the daemon's certificate. These certificates come from the `docker:dind` image's own CA, not from the internal root CA.
- **Resource Limits:** Each container is limited to 1 GB of memory and one CPU core, runs as an unprivileged user, and is removed after at most 5 minutes. Scripts read the project's data from MinIO through presigned URLs listed in a read-only manifest.
- **Network Isolation:** `sandbox-dind` is isolated from the platform's internal `db_network`, which hosts the unauthenticated Docker socket proxy (`docker-proxy`), PostgreSQL, and Redis. It is connected only to `sandbox_control` (an internal network shared with the application and worker for mutual TLS Docker API calls on port 2376), `sandbox_internal` (an internal network shared with MinIO for S3 data access), and `sandbox_egress` (a bridge network providing outbound internet access for package installation during image builds). Containers running inside `sandbox-dind` therefore have no network path to the host Docker daemon or backend databases. Scripts should still be treated as project-level code.

### Overlay Network (Optional)

SwarmMedHub neither requires nor enforces an overlay network; see [Networking](networking.md).

- **Encryption:** When participating sites use an overlay such as [Tailscale](https://tailscale.com/security/), traffic between them on the overlay is carried over WireGuard. Federated-learning traffic is protected by NVFlare's mutual TLS whether or not an overlay is used.
- **Status Display:** To show the Tailscale state in the interface, a host-side script (`scripts/tailscale_watcher.sh`, started by `make tailscale` or `make start`) writes the host's Tailscale IPv4 address and connection state to `tmp/tailscale_status.json` every 30 seconds. The application reads this file from the bind-mounted project directory, so the host's Tailscale socket is not mounted into any container.

### Database Proxy (PgBouncer)

PgBouncer is deployed and configured for connection pooling, but in the default configuration (`DB_HOST=postgres` in `.env.template`) Django connects to PostgreSQL directly and PgBouncer carries no traffic.

- **SCRAM-SHA-256:** PgBouncer authenticates clients with SCRAM-SHA-256 over TLS. PostgreSQL itself requires SCRAM-SHA-256 passwords for connections from other containers.
- **Locked-down Config:** `scripts/setup_pgbouncer.py` writes `pgbouncer.ini` and `userlist.txt` with `0600` permissions into a `0700` directory (`.secrets/pgbouncer/`).

### Secrets and Start-up Checks

- **Start-up Checks:** With `DEBUG=False`, Django refuses to start if `SECRET_KEY`, `FERNET_KEYS`, `BACKUP_ENCRYPTION_KEY`, `REDIS_PASSWORD` or the S3 credentials are missing, or if `BACKUP_ENCRYPTION_KEY` equals `SECRET_KEY`. The application and worker containers also refuse to start if the database password is the literal default `swarmmedhub` (override: `ALLOW_INSECURE_PGBOUNCER=1`).
- **Not Checked:** The strength of these values is not checked, and the placeholder values in `.env.template` are accepted. With `DEBUG=True` none of the Django checks run, and missing S3 credentials fall back to `minioadmin`/`minioadmin`.
- **Generated Secrets:** `make env`, which `make start` also runs, generates random 32-character passwords and random secret, field-encryption, backup and MinIO KMS keys. It writes `.env` readable and writable only by its owner (mode 600), and sets an existing `.env` to that mode when it rewrites it. A `.env` written before 9 October 2026 got the default mode, which under common umask settings lets every local user read it, and keeps it until `make env` or `make start` runs again; `chmod 600 .env` fixes it at once.
- **Application Image:** The application and worker images are built from the project directory (`COPY . .`). `.dockerignore` keeps `.env`, `.secrets/`, `workspaces/`, `results/` and `docker-compose.override.yml` out of them; the containers do not need them in the image, because they get `.env` through `env_file` and see the project directory through a bind mount. Before 9 October 2026, of these only `.secrets/` was excluded, so an image built earlier contains the `.env`, `workspaces/` (the startup kits, private keys included) and `results/` as they were when it was built. Rebuild such images, delete the old ones, and treat any copy, for example in a registry, as holding the deployment's secrets.
- **Production Mode:** Run production deployments with `DEBUG=False`, the `.env.template` default. With `DEBUG=True`, besides skipping the checks above, PostgreSQL connections use `sslmode=require` instead of `verify-full`, the session and CSRF cookies are not marked `Secure`, and Django shows detailed error pages.

## 💻 Application Security

### Access Control (RBAC)

SwarmMedHub assigns each user one of three roles (Admin, Developer, User), but enforces them narrowly:

- **Role Checks:** The in-app user-management pages require the Admin role or a Django superuser, and the log viewer requires the Developer or Admin role.
- **Everything Else:** Every other action requires only a logged-in account and, for anything inside a project, authorship or membership of that project, whatever the role. This includes creating projects, importing data, provisioning and starting networks, running validation and training, and downloading startup packages and results. Actions on a network named in the URL, such as starting, stopping or deleting it, exporting its startup package, or starting and stopping training, are checked against the project the network belongs to; a network of another project is reported as not found.
- **Project Boundaries:** In the web interface, no role grants access to projects the user is not a member of. Only emergency access (see [HIPAA Technical Safeguards](#hipaa-technical-safeguards)) bypasses project membership.
- **Django Admin:** The Django admin panel is governed by Django's `is_staff` and `is_superuser` flags, not by the role.
- **Command-Line Interface:** The `swarmed` CLI performs no authentication. It acts as whichever user is named with `--user`, `SWARMED_USER` or the operating-system login, and needs only access to the Hub's database. Anyone who can run it on a Hub host, for example with `docker exec -w /app swarmmedhub python3 -m swarmed_cli.main …`, can act as any user.

### Multi-Factor Authentication (MFA)

- **2FA Support:** Users can register a TOTP authenticator app, with backup codes, through `django-two-factor-auth` and `django-otp`.
- **Optional per User:** Users who have registered a device must enter a code at login, and the Django admin login is redirected to the same flow. Users without a device log in with a password alone. SwarmMedHub has no setting that requires MFA for every user; enforcing it needs a code change (for example `otp_required` on views, or two_factor's `AdminSiteOTPRequired`). The `swarmed` CLI does not use MFA.

### Audit Logging and Signing

- **Coverage:** Logins, logouts and failed logins are logged by signal handlers. Every POST, PUT, PATCH and DELETE request to the web interface is logged with a redacted copy of the submitted data (multipart submissions, such as file uploads, are omitted), except requests to login paths and requests that return 404. Downloads of data files, results and startup packages are logged explicitly, as are many actions in the shared service code. Page views are not logged. In addition, every creation or update of a platform record, such as a project, network, training job or result, writes an automatic entry naming the model, the action and the record (`apps/common/signals.py`), attributed to the user of the web request or background task, or else to the record's own user or author field if it has one. Saves that only update a training job's progress are skipped. The `swarmed` CLI bypasses the web request middleware, so its actions appear only where the service code logs them and in these automatic entries. Deletions and project-membership changes have no automatic entry: the hooks meant to record them (`apps/logs/signals.py`) are never connected.
- **Delivery:** Entries are written asynchronously through Celery. An entry is lost if queueing or saving it fails, and setting `SWARMMEDHUB_DISABLE_ASYNC_LOGGING` turns database audit logging off.
- **Client Address:** The IP address stored with an entry is the left-most `X-Forwarded-For` value. nginx appends to that header instead of replacing it, so a client can choose the value. The login and failed-login messages name `REMOTE_ADDR`, which behind nginx is nginx's own address. (The login lockout uses the entry that nginx appends; see [Brute-Force Protection](#brute-force-protection).)
- **Cryptographic Chaining:** Each entry stores an HMAC-SHA256 signature over its ID, timestamp, user ID, category, message and the signature of the entry written before it. The signature does not cover the level, source, context data, IP address, user agent, path, or the project and network links. SwarmMedHub ships no routine that verifies the signatures or the chain, concurrent writers can fork the chain, and the entries of deleted users no longer verify because the user ID is part of the signed data. The log is designed to be tamper-evident, but tampering can be detected only once an independent verifier has been written and run.
- **Log Integrity:** Each entry's HMAC key is a random 64-character key stored in the database (`LogSigningKey`) followed by Django's `SECRET_KEY` from the environment. For rotation, see [Key Rotation](#key-rotation).

### Brute-Force Protection

- **django-axes:** Failed logins are counted per combination of username and client address (`AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]`). After five failures (`AXES_FAILURE_LIMIT = 5`), that username is locked out from that address for one hour (`AXES_COOLOFF_TIME = 1`); other users, and the same user from other addresses, can still log in. A successful login resets the count for its combination.
- **Client Address:** The address is the last entry of `X-Forwarded-For`, the one the bundled nginx appends (`$proxy_add_x_forwarded_for`), which a client cannot forge. Without that header, or if its last entry is not a valid address, `REMOTE_ADDR` is used.
- **Limitations:** Guesses spread over many client addresses are not limited by this control. A request that reaches the application server directly instead of through nginx, which containers on the platform's internal Docker networks can do, sandboxed scripts included (see [Script Sandbox](#script-sandbox)), can choose the address its failures are counted under.

### Vulnerability Management

- **Static Analysis and Dependency Scanning:** Contributors are asked to run Bandit and Snyk locally (see [Local Security Scans](#local-security-scans)). Neither tool runs in CI, there is no Snyk monitoring configuration, and no scan reports are kept in the repository. `# nosec` annotations in the code mark Bandit findings that were reviewed.
- **Framework Defaults:** Built on [Django](https://docs.djangoproject.com/en/stable/topics/security/), whose template auto-escaping, CSRF middleware and ORM protect against XSS, CSRF and SQL injection. Two endpoints used by other Hubs (status gossip and participant status) are exempt from CSRF checks and accept the network's shared token instead.

## 💾 Data Security

### Encryption at Rest

- **MinIO (S3):** Objects written through Django's storage backend (uploaded datasets, project code and scripts, plots), training results synchronized from the FL workspaces (model weights and logs), and the copies made when a file is renamed are stored with SSE-S3 (AES-256) under MinIO's built-in KMS key (`MINIO_KMS_SECRET_KEY`). Each write requests the encryption; no bucket-default encryption is configured. Training results and renamed copies written before 9 October 2026 were stored unencrypted and stay so until they are written again; SwarmMedHub has no command that re-encrypts them. The KMS key is stored in `.env` on the same host, so SSE-S3 does not protect against anyone who can read the project directory.
- **Database Encryption:** Selected fields are encrypted in PostgreSQL with Fernet (AES-128-CBC with HMAC-SHA256) by SwarmMedHub's own `EncryptedCharField` and `EncryptedTextField` (`apps/common/fields.py`), with keys derived from `FERNET_KEYS` using HKDF-SHA256. These are the profile's full name, address, ZIP code, city, country and phone number, the emergency-access justification, audit-log messages, chat messages and board posts, project secrets and gossip tokens. Django's built-in user table (username, first and last name, email address) is not encrypted.
- **Volume Encryption:** It is strongly recommended to place Docker's data root, which holds the `postgres_data` and `minio_data` named volumes, and the project's `workspaces/`, `media/`, `tmp/` and `.secrets/` directories on encrypted volumes (LUKS, FileVault). `workspaces/` holds the federated-learning startup kits, including private keys, and the training results.

### Backup & Restore

- **Configuration:** Backups are configured in the Django admin (Backup Configurations); none exists by default. A backup runs daily at 02:00 UTC for every active configuration and can also be triggered manually. By default an archive contains a database dump, the `media/` directory and every object in the S3 bucket.
- **Encrypted Archives:** Each `tar.gz` archive is encrypted with Fernet (AES-128-CBC with HMAC-SHA256, key derived from `BACKUP_ENCRYPTION_KEY` with SHA-256) before it is stored on a local path or in S3.
- **Known Defects:** The application image does not include the PostgreSQL client tools (`pg_dump`, `pg_restore`, `psql`, `createdb`, `dropdb`), so every backup that includes the database, which is the default, fails. The retention settings (`BACKUP_RETENTION_DAYS` and each configuration's retention days) are not enforced, so old backups are never deleted.
- **Archive Inspection:** A restore refuses an archive that contains links or members that would extract outside the temporary restore directory.
- **Restore Scope:** Archive members are extracted only into that temporary directory. The restore itself then drops and recreates the database, replaces `media/`, and overwrites objects in the S3 bucket with the archived copies.

## 🤝 Decentralized Learning Security

Federated training runs on [NVIDIA FLARE](https://nvidia.github.io/NVFlare/security/):

- **No Raw Data Exchange:** Sites exchange model weights (the full parameter tensors after local training, not gradients, in the default configuration), a small amount of metadata such as the round number and the local step count used to weight the average, and orchestration messages. The SwarmMedHub web interfaces also exchange status messages, and any holder of the network's shared token can download the final global model from a Hub that has it. None of these messages contains images, labels or patient identifiers. This holds only if the training, validation and visualization code supplied by the study team is trusted, because that code runs with network access. When a presigned data URL fails, the data adapter (`apps/training/flare_adapter.py`) retries the object path, which can contain exam identifiers, over HTTP and HTTPS without certificate verification, but only on a fixed list of local host names and addresses (`minio`, `host.docker.internal`, `localhost`, `127.0.0.1`, `DOCKER_HOST_IP` and the container's own address). Versions before 9 October 2026 tried the fixed overlay address `100.127.11.1` first, which on an overlay network in the 100.64.0.0/10 range can belong to another peer.
- **Identity Security:** Connections from clients and admin consoles to the FL server use mutual TLS, with certificates issued by a root CA that is generated for each network at provisioning. The exported startup package, however, contains every site's private keys, an admin identity for each site with the `project_admin` role (which may submit jobs whose custom code runs at every site), the FL server's private key and the network's shared token. Importing it gives each site all of them, so a certificate shows that a party belongs to the study, not which participant it is. Any project member can download the package; treat it as a secret and transfer it over a secure channel.
- **Internal Connections:** NVFlare's connections between each site's FL process and its job processes are not encrypted (`connection_security: clear`). On clients, which use host networking by default, that listener accepts connections on all of the host's interfaces.
- **Privacy Preservation:** NVIDIA FLARE ships differential-privacy filters and homomorphic encryption. SwarmMedHub does not configure or test either, and neither is active in a default deployment. Sharing model parameters alone does not prevent model inversion, membership inference or gradient leakage.

!!! warning "Read the threat model"
    Model updates are relayed through the FL server and, in the default
    configuration, are not end-to-end encrypted between clients — the
    coordinating institution can observe them. See [Threat model](threat-model.md)
    for what leaves a site, which adversaries are considered, and what is not
    implemented.

## 📋 Compliance

### HIPAA Technical Safeguards

SwarmMedHub implements several technical controls relevant to the HIPAA Security Rule (45 CFR 164.312), each with the limitations described on this page. It has not been assessed for HIPAA compliance; whether a deployment meets the Security Rule depends on the operating institution's configuration and procedures.

- **Unique Identification:** Every account has a unique username and UUID, and audit entries record the acting account. Actions taken through the `swarmed` CLI are attributed to whichever user the caller names.
- **Emergency Access (Break-Glass):** A superuser can grant a user temporary access that bypasses role and project-membership checks. A grant made from the in-app user list is logged at `CRITICAL` level with the justification and expiry. An hourly task revokes expired grants, so access can outlast its expiry by up to an hour. The justification and the 1–24 h duration limit are enforced only by the web form; the server accepts a missing justification and any duration. Anyone who can edit profiles in the Django admin (by default, superusers) can also set the flag there, which writes no `CRITICAL` entry and, without an expiry date, never expires.
- **Automatic Logoff:** Sessions expire 30 minutes after the last request (`SESSION_COOKIE_AGE = 1800` with `SESSION_SAVE_EVERY_REQUEST = True`), and the session cookie is discarded when the browser closes. Pages that poll the server in the background (the dashboard every 5 s, the training page every 3 s) count as activity, so an open tab keeps the session alive.
- **Encryption:** SSE-S3 (AES-256) protects the objects SwarmMedHub writes to MinIO, except training results and renamed copies stored before 9 October 2026, and Fernet (AES-128-CBC with HMAC-SHA256) protects selected database fields and backups. The PostgreSQL volume and the on-disk workspaces, which hold the startup kits and the training results, are not encrypted by the platform, so use encrypted volumes. In transit, TLS protects browser connections (TLS 1.2 or 1.3), the connections to PostgreSQL, Redis and MinIO, and NVFlare's client-to-server and admin connections. The nginx-to-application and application-to-`docker-proxy` hops inside the host and NVFlare's internal connections at each site are unencrypted. Hub-to-Hub HTTPS calls do not verify the peer's certificate unless `SWARMMEDHUB_SKIP_PEER_SSL_VERIFY=false` is set; the generated web certificate names only `localhost` and `127.0.0.1`.

### GDPR

- **Data Minimization:** Training data stays at each site; only model parameters, metadata and status messages are exchanged (see [Decentralized Learning Security](#decentralized-learning-security)).
- **Right of Access:** Users can download their account and profile data, their own audit-log entries and the metadata of the projects they authored as a JSON file from their settings page.
- **Right to Erasure:** Users can delete their own account. This removes the user, their profile and every project they authored, together with each such project's stored files, networks and audit entries, including entries written by other members. The user's remaining audit entries are pseudonymized (user link removed, identifier replaced, IP address and user agent cleared), but their message text, which can contain the username, is kept. django-axes login records, data the user contributed to other users' projects, and existing backups are not removed. When an administrator deletes a user instead, the stored files of that user's projects are left in the object store.
- **Right to Restriction:** An administrator can set a user's `is_restricted` flag on that user's page in the Django admin, which blocks the user from every page except the privacy policy and sign-out. Every other page shows an "Account Restricted" notice (HTTP 403) with a link to the privacy policy and a sign-out button. The flag does not stop other users, running jobs or the CLI from processing data the user contributed.
- **Consent Management:** There is no self-registration; accounts are created by administrators. On first login, every request is redirected to an acceptance page until the user accepts both the Terms and the Privacy Policy, and the acceptance dates are stored.

### Data Retention & Purging

- **Automated Purge:** Daily Celery tasks delete validation, visualization and results-visualization runs, training results (with their files), training jobs and networks older than `DATA_RETENTION_DAYS` (default 2,190 days, 6 years). They delete project-category audit entries, which include logins and access events, after `SECURITY_LOG_RETENTION_DAYS` (default 365 days) and other audit entries after `DATA_RETENTION_DAYS`. They also clear the IP-address column of audit entries older than `IP_ANONYMIZATION_DAYS` (default 90 days). IP addresses written into log message text and stored in the django-axes tables are not anonymized.
- **Uninstalling:** `make deinstall` deletes the database and object-store volumes, and with them the audit log, without asking for confirmation.

## 🔍 Security Operations

### Key Rotation

- **Audit Log Keys:** Rotate the database part of the log signing key inside the application container:
  ```bash
  docker exec -w /app swarmmedhub python3 manage.py rotate_signing_key
  ```
  Old keys stay in the database, so earlier entries remain checkable. The active key is cached for up to an hour, so entries may still be signed with the previous key during that time. Nothing rotates the key on a schedule. Because `SECRET_KEY` is part of every signing key, changing `SECRET_KEY` makes all existing signatures unverifiable.
- **Field Encryption Keys:** `FERNET_KEYS` is a comma-separated list: the first key encrypts and all keys are tried for decryption. A new key can be put first while old values stay readable; a value is re-encrypted with the new key only when its record is saved again, and no command re-encrypts existing values. Removing a key that still protects stored values makes those fields show the encrypted token instead of the value.
- **Backup Key:** Backups are encrypted with a single key derived from `BACKUP_ENCRYPTION_KEY`. Changing it makes existing backups unreadable, so keep the old value for as long as its backups are kept.
- **Infrastructure Keys:** MinIO uses a single static KMS key (`MINIO_KMS_SECRET_KEY`), and SwarmMedHub provides no rotation procedure. Do not simply replace the key in `.env`: MinIO would then no longer be able to decrypt objects already encrypted under the old key. Plan any rotation with MinIO's KMS documentation (for example by moving to an external KMS) before changing it.

### Local Security Scans

Run these locally to identify vulnerabilities:

```bash
# Dependency & Source Analysis
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json

# Python Static Analysis
bandit -r apps core manage.py -f json -o bandit_report.json
```
