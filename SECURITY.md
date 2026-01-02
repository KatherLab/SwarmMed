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
- **Environment Variables:** Never commit secrets to the repository. Use `.env` files and ensure they are ignored by git.
- **Production Mode:** `DEBUG` must be set to `False` in production environments.
- **Database & Services:** Use secure, unique passwords for MinIO, Postgres, and Redis.
- **Secret Key:** Ensure `SECRET_KEY` is kept private and changed immediately if compromised.
- **Encryption:** Use TLS/SSL (HTTPS) for all public-facing endpoints.
- **Data Privacy:** Be mindful of PII (Personally Identifiable Information) and PHI (Protected Health Information) when logging or storing data.

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