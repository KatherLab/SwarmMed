# 🛡️ Security Policy

> [!WARNING]
> **Pre-release Software:** This platform has **not yet** undergone external security audits or professional penetration testing. These rigorous evaluations are scheduled for the next major version. Use this software with appropriate caution in sensitive environments.

## 🚀 Supported Versions

The following versions of SwarmMedHub are currently being supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## 📧 Reporting a Vulnerability

We take the security of SwarmMedHub seriously. If you find a security vulnerability, please report it to us responsibly.

**⚠️ Please do not open a public issue for security vulnerabilities.**

Instead, please send an email to [mediswarmcloud@gmail.com](mailto:mediswarmcloud@gmail.com) with a description of the issue. We will acknowledge receipt of your vulnerability report as soon as possible and outline the next steps in our response process.

### 📝 What to include in your report:
- 📖 A detailed description of the vulnerability.
- 👣 Steps to reproduce the issue (proof-of-concept).
- 💥 Potential impact of the vulnerability.
- 🛠️ Any suggested fixes (optional).

## 🏗️ Security Architecture

SwarmMedHub is built on five core security pillars, providing a defense-in-depth strategy for decentralized medical AI.

### 1. 🔐 Confidentiality & Data Protection
We ensure that sensitive data remains private whether it is at rest or in transit.
- **Encryption at Rest:**
    - **Object Storage:** All datasets and models in MinIO are encrypted using **AES-256 (SSE-S3)** with a unique KMS master key generated during setup.
    - **Database Fields:** Sensitive PHI/PII metadata in PostgreSQL is protected via **Fernet symmetric encryption** (AES-128 in CBC mode with HMAC-SHA256) using dedicated keys.
    - **Backups:** Database and configuration backups are fully encrypted using a separate **BACKUP_ENCRYPTION_KEY**.
- **Encryption in Transit:**
    - All external and internal communication is enforced via **TLS 1.2/1.3**.
    - Connections to PostgreSQL and Redis require SSL/TLS with certificate verification against an internal Root CA.

### 2. 🐳 Compute Isolation & Sandboxing
User-provided code (validation/visualization) is treated as untrusted and executed in a highly restricted environment.
- **Sandbox-dind:** Workloads run in ephemeral Docker containers against a dedicated **Docker-in-Docker** service, isolated from the host Docker socket.
- **Mutual TLS (mTLS):** The application communicates with the sandbox daemon over a secure mTLS connection, using certificates that are automatically validated before execution.
- **Hard Resource Quotas:** Each sandbox container is strictly limited to **1GB RAM and 1 CPU core** to mitigate Denial of Service (DoS) attempts.
- **Network Air-gapping:** Sandbox containers run on an isolated bridge network with no access to the internet or the host network.

### 3. 👤 Identity & Access Governance
Strict controls ensure that only authorized users can access specific project resources.
- **Multi-Factor Authentication (MFA):** Supports and recommends TOTP-based 2FA for all administrative and developer accounts.
- **Brute-Force Protection:** Integrated **django-axes** locks accounts after **5 failed attempts** with a mandatory 1-hour cooling-off period.
- **Session Security:** 
    - **Automatic Timeout:** Sessions expire after **30 minutes** of inactivity.
    - **Strict Cookies:** `HttpOnly` and `Secure` flags are enforced for all session and CSRF cookies.
- **Password Policy:** Minimum **12-character** length with complexity requirements enforced via Django validators.

### 4. 🌐 Network Integrity & VPN Mesh
Decentralized training relies on secure peer-to-peer communication across institutional boundaries.
- **Private VPN Mesh:** Built-in **Tailscale (WireGuard)** integration creates a private, encrypted network between all swarm participants, bypassing public internet exposure.
- **Internal Service Mesh:** A private, internal **Root CA** issues short-lived certificates to all backend services (Nginx, MinIO, Postgres, Redis), ensuring zero-trust networking within the Docker stack.

### 5. 📜 Tamper-Evident Audit Logging
SwarmMedHub maintains a high-integrity audit trail for HIPAA and GDPR compliance.
- **Cryptographic Signing:** Every log entry is signed using **HMAC-SHA256**, combining a database-stored signing key with the environment's `SECRET_KEY`.
- **Log Chaining:** Logs are cryptographically chained (each entry contains the hash of the previous one), making retroactive log tampering or deletion mathematically detectable.
- **Data Retention:** PHI-related logs are retained for **6 years** by default, with automated daily purging of expired data.

## ⚙️ Secure Configuration

SwarmMedHub handles sensitive bio data. Always ensure the following:
- **🛠️ Environment Setup:** Always use `make env` to initialize your environment. This generates secure, unique random passwords for all services.
- **🚫 Production Mode:** `DEBUG` must be set to `False` in production environments.
- **🌐 Encryption:** Use TLS/SSL (HTTPS) for all public-facing endpoints.
- **👤 Data Privacy:** Be mindful of PII (Personally Identifiable Information) and PHI (Protected Health Information) when logging or storing data.

## ⚖️ Compliance

SwarmMedHub is designed with data protection as a core principle and is **HIPAA Capable**.

- **🇪🇺 GDPR (General Data Protection Regulation):** The platform's decentralized architecture supports data minimization by keeping raw data local and supports general GDPR requirements.
- **🏥 HIPAA (Health Insurance Portability and Accountability Act):** Technical safeguards (encryption at rest/transit, MFA, brute-force protection, audit logs) are implemented to support PHI handling.

### 🏁 HIPAA Readiness & Remaining Gaps

While SwarmMedHub provides the technical foundation for HIPAA compliance, achieving full compliance requires operational and administrative measures:

1. **📦 Infrastructure Encryption:** To ensure encryption at rest, Docker volumes should be configured with an encrypted volume driver (e.g., LUKS-backed local driver or cloud-provider encrypted storage).
2. **🔑 Secrets Management:** For production readiness, use a dedicated secrets manager (e.g., HashiCorp Vault) and rotate secrets regularly.
3. **📜 Audit Log Governance:** Implement operational log review and retention policies.

## 🔍 Security Scans

You can run these security scans locally before submitting a Pull Request:

### 📦 Dependency & Vulnerability Scanning (Snyk)
```bash
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json
```

### 🐍 Python Static Analysis (Bandit)
```bash
bandit -r apps core home manage.py -f json -o bandit_report.json    
```

## 🧪 Security Testing & Audit

We are committed to maintaining a secure codebase. SwarmMedHub is regularly scanned and tested to identify and mitigate known vulnerabilities. 
