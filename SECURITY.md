# Security Policy

## Supported Versions

The following versions of MediSwarmCloud are currently being supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

We take the security of our project seriously. If you find a security vulnerability, please report it to us responsibly.

**Please do not open a public issue for security vulnerabilities.**

Instead, please send an email to [INSERT SECURITY EMAIL ADDRESS] with a description of the issue. We will acknowledge receipt of your vulnerability report as soon as possible and outline the next steps in our response process.

### What to include in your report:
- A detailed description of the vulnerability.
- Steps to reproduce the issue (proof-of-concept).
- Potential impact of the vulnerability.
- Any suggested fixes (optional).

## Secure Configuration

MediSwarmCloud handles sensitive medical data. Always ensure the following:
- `DEBUG` is set to `False` in production.
- Use secure, unique passwords for MinIO, Postgres, and Redis.
- Ensure `SECRET_KEY` is kept private and changed if compromised.
- Use TLS/SSL for all public-facing endpoints.
