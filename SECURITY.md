# Security Policy

## Supported Versions

The following versions SwarmCloud are currently being supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

We take the security of SwarmCloud seriously. If you find a security vulnerability, please report it to us responsibly.

**Please do not open a public issue for security vulnerabilities.**

Instead, please send an email to [mediswarmcloud@gmail.com](mailto:mediswarmcloud@gmail.com) with a description of the issue. We will acknowledge receipt of your vulnerability report as soon as possible and outline the next steps in our response process.

### What to include in your report:
- A detailed description of the vulnerability.
- Steps to reproduce the issue (proof-of-concept).
- Potential impact of the vulnerability.
- Any suggested fixes (optional).

## Secure Configuration

SwarmCloud handles sensitive bio data. Always ensure the following:
- **Environment Variables:** Never commit secrets to the repository. Use the provided `.env.template` as a base for your local `.env` file and ensure it is ignored by git.
- **Production Mode:** `DEBUG` must be set to `False` in production environments.
- **Database & Services:** Use secure, unique passwords for MinIO, Postgres, and Redis.
- **Secret Key:** Ensure `SECRET_KEY` is kept private and changed immediately if compromised.
- **Encryption:** Use TLS/SSL (HTTPS) for all public-facing endpoints.
- **Data Privacy:** Be mindful of PII (Personally Identifiable Information) and PHI (Protected Health Information) when logging or storing data.

## Compliance

SwarmCloud is designed with data protection as a core principle and is **HIPAA Capable**.

- **GDPR (General Data Protection Regulation):** The platform's decentralized architecture supports data minimization by keeping raw data local. It also includes self-service tools for the "Right to Erasure" and mandatory consent for data processing.
- **HIPAA (Health Insurance Portability and Accountability Act):** Technical safeguards (encryption at rest/transit, MFA, brute-force protection, audit logs) are implemented to support PHI handling.

### HIPAA Readiness & Remaining Gaps

While SwarmCloud provides the technical foundation for HIPAA compliance, achieving full compliance requires operational and administrative measures by the hosting organization:

1. **Administrative Safeguards:** HIPAA is a program, not just a set of features. You must implement risk analysis, policies/procedures, training, incident response, and access reviews.
2. **Business Associate Agreements (BAA):** You must have BAAs in place with any third-party vendors (hosting, email, etc.) that may have access to ePHI.
3. **Infrastructure Encryption:** SwarmCloud uses named Docker volumes for all persistent data (`postgres_data`, `minio_data`, `workspace_data`, etc.). To ensure encryption at rest, these volumes should be configured with an encrypted volume driver (e.g., LUKS-backed local driver or cloud-provider encrypted storage).
4. **Postgres SSL Enforcement:** SSL is enforced at the database level via a mandatory initialization script (`scripts/init_postgres_ssl.sh`) that runs whenever the database is initialized, ensuring `pg_hba.conf` only allows `hostssl` connections.
5. **Secrets Management:** Current environments use `.env` files based on `.env.template`. For higher security, it is recommended to use a dedicated secrets manager (e.g., HashiCorp Vault, AWS Secrets Manager) and rotate secrets regularly.
6. **Audit Log Governance:** Technical log signing is present, but you must implement operational log review, alerting, and retention policies (e.g., exporting to a SIEM).
7. **Container Security:** The `docker-socket-proxy` is a high-privilege component. Treat it as such in your threat model, ensuring network isolation and strict monitoring.

## Security Scans

We utilize automated security scanning tools to maintain the integrity of our codebase. You can run these scans locally using the following commands:

### Dependency & Vulnerability Scanning (Snyk)
Snyk helps identify known vulnerabilities in dependencies and provides security analysis for the source code.
```bash
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json
```

### Python Static Analysis (Bandit)
Bandit is used to find common security issues in Python code.
```bash
bandit -r apps core home manage.py -f json -o bandit_report.json    
```

## Security Testing & Audit

We are committed to maintaining a secure codebase. SwarmCloud is regularly scanned and tested to identify and mitigate known vulnerabilities. We encourage contributors to run the security scans mentioned above before submitting Pull Requests.