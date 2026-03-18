---
title: Security
description: Security best practices and implementations in MedSwarmHub.
---

# Security

!!! warning "Pre-release Software"
    This platform has **not yet** undergone external security audits or professional penetration testing. These rigorous evaluations are scheduled for the next major version. Use this software with appropriate caution in sensitive environments.

!!! warning "Shared Responsibility"
    While MedSwarmHub provides a secure platform, the overall security of your decentralized learning setup also depends on the security of your own infrastructure and the adherence to security best practices by all participants.

Security is a foundational aspect of the MedSwarmHub platform, designed to protect the confidentiality, integrity, and availability of medical data and machine learning models.

## 🏗️ Infrastructure & Network Security

MedSwarmHub employs multiple layers of isolation and encryption to protect the host system and secure communication between services.

### Internal Service Mesh (Private PKI)
MedSwarmHub employs an internal Public Key Infrastructure (PKI) to secure communication between all backend services.

- **Root CA:** A private internal Root CA issues certificates for each service (MinIO, Postgres, Redis).
- **Mutual TLS (mTLS):** The application and worker containers trust this internal Root CA, ensuring secure and verified connections across the internal Docker network.
- **Service Isolation:** All backend services are enforced to use SSL/TLS (PostgreSQL SSL, Redis TLS, MinIO TLS).

### TLS-Isolated Sandboxing
User-provided Python scripts (validation and visualization) are executed in ephemeral Docker containers with strict isolation:

- **Sandbox-dInd:** Workloads run against a dedicated `sandbox-dind` (Docker-in-Docker) service rather than the host Docker socket.
- **Mutual TLS:** Communication between the application and the sandbox daemon is secured via mutual TLS with client certificates.
- **Resource Limits:** Containers are strictly limited to **1GB RAM** and **1 CPU core**.
- **Network Isolation:** Workloads run on a dedicated `sandbox_internal` bridge network with no access to the host network or the internet, but restricted access to the internal MinIO gateway.

### Secure Network Telemetry
To monitor VPN status without exposing the host system, MedSwarmHub uses a **Sidecar Architecture**:
- The application queries a dedicated `tailscale-status` sidecar over HTTP.
- This eliminates the need to mount the host's `/var/run/tailscale` socket into the application container, preventing unauthorized access to the host VPN daemon.

### Database Proxy (PgBouncer)
Access to the PostgreSQL database is mediated by PgBouncer for connection pooling and enhanced security:

- **SCRAM-SHA-256:** Uses strong authentication for database connections.
- **Locked-down Config:** Configuration files are generated with restricted (`0600`) permissions.
- **Baseline Security:** The system refuses to start with default or weak passwords.

### VPN Isolation (Tailscale)
MedSwarmHub leverages [Tailscale](https://tailscale.com/security/) to create a secure, private network for participants:

- **End-to-End Encryption:** All traffic is encrypted using WireGuard.
- **Zero-Config VPN:** Simplifies secure peer-to-peer communication without complex firewall rules.

## 💻 Application Security

### Access Control (RBAC)
MedSwarmHub implements a robust Role-Based Access Control (RBAC) system:

- **Admin:** Full control over the platform, users, projects, and system settings.
- **Developer:** Can create and manage projects, upload data, run training jobs, and view logs.
- **User:** Can view and interact with projects they are members of.

### Multi-Factor Authentication (MFA)
- **2FA Support:** Multi-Factor Authentication (TOTP) is supported via `django-otp` and `two-factor`.
- **Enforced Flow:** MFA can be enforced for all users to provide an additional layer of identity security.

### Auditing & Tamper-Evident Logging
Comprehensive logging and auditing provide full visibility into platform activities:

- **Audit Trail:** All user actions (logins, project edits, training submissions) are logged.
- **Cryptographic Chaining:** Logs are signed using **HMAC-SHA256** and chained to previous entries (similar to a blockchain) to ensure they are tamper-evident.
- **Log Integrity:** The signing key is stored in the database and can be rotated periodically.

### Brute-Force Protection
- **django-axes:** Automatically blocks IP addresses or users after multiple failed login attempts.
- **Cool-off Period:** Configured to enforce a temporary lockout to prevent automated attacks.

### Vulnerability Management
- **Static Analysis:** We use `Bandit` to identify common security issues in Python code.
- **Dependency Scanning:** `Snyk` is used to monitor and patch vulnerabilities in open-source components.
- **Secure Defaults:** Built on [Django](https://docs.djangoproject.com/en/stable/topics/security/), providing built-in protection against XSS, CSRF, and SQL Injection.

## 💾 Data Security

### Encryption at Rest
- **MinIO (S3):** All objects (datasets, models) are encrypted using **Server-Side Encryption (SSE-S3)** with AES-256.
- **Database Encryption:** Sensitive fields (PHI/PII like names, addresses, phone numbers) are encrypted in the PostgreSQL database using `django-fernet-fields` (AES-256).
- **Volume Encryption:** It is strongly recommended to host data directories (`postgres_data/`, `minio_data/`, `workspaces/`) on encrypted volumes (LUKS, FileVault).

### Safe Backup & Restore
The backup system is designed to prevent exploitation:

- **Encrypted Archives:** Backups are encrypted using Fernet (AES) before storage.
- **Archive Inspection:** During restoration, archives are inspected for symbolic links and path traversal attempts before extraction.
- **Safe Extraction:** Files are never overwritten outside the designated restore directory.

## 🤝 Decentralized Learning Security

Powered by [NVIDIA FLARE](https://nvidia.github.io/NVFlare/security/), the platform ensures:

- **No Raw Data Exchange:** Only model updates (gradients/weights) are exchanged; raw medical data never leaves local infrastructure.
- **Identity Security:** Mutual TLS authentication for all participating sites via a central Root CA.
- **Privacy Preservation:** Support for Differential Privacy, Homomorphic Encryption, and Secure Aggregation to prevent data leakage from model updates.

## 📋 Compliance

### HIPAA Capable
MedSwarmHub provides the technical safeguards required for handling Protected Health Information (PHI):

- **Unique Identification:** Every user has a unique UUID and audit trail.
- **Emergency Access (Break-Glass):** Supports temporary administrative override for emergency access, with mandatory justification and `CRITICAL` severity logging.
- **Automatic Logoff:** Sessions are configured to expire after **30 minutes of inactivity** and close on browser exit.
- **Encryption:** AES-256 for data at rest and TLS 1.2+ for data in transit.

### GDPR Ready
- **Data Minimization:** Raw data remains local to the participant.
- **Right to Erasure:** Users can permanently delete their accounts and associated data.
- **Right to Restriction:** Administrators can restrict processing of a user's data.
- **Consent Management:** Mandatory acceptance of Terms and Privacy Policy during registration.

### Data Retention & Purging
- **Automated Purge:** Celery tasks automatically purge expired data and anonymize IP addresses based on configurable retention periods (e.g., 6 years for HIPAA compliance).

## 🔍 Security Operations

### Key Rotation
- **Audit Log Keys:** Rotate log signing keys via management command:
  ```bash
  python manage.py rotate_signing_key
  ```
- **Infrastructure Keys:** Annual rotation of S3/MinIO SSE-S3 keys is recommended.

### Local Security Scans
Run these locally to identify vulnerabilities:
```bash
# Dependency & Source Analysis
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json

# Python Static Analysis
bandit -r apps core manage.py -f json -o bandit_report.json   
```