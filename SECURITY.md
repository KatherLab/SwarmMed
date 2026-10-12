# 🛡️ Security Policy

> [!WARNING]
> **Pre-release Software:** SwarmMed has **not** undergone an external security audit or professional penetration test. Use it with appropriate caution in sensitive environments.

## 🚀 Supported Versions

Security fixes are made for the following release of SwarmMed, which comprises the SwarmMedHub web interface and the `swarmed` CLI:

| Version | Supported          |
| ------- | ------------------ |
| 1.0.0   | :white_check_mark: |

## 📧 Reporting a Vulnerability

We take the security of SwarmMed seriously. If you find a security vulnerability, please report it to us responsibly.

**⚠️ Please do not open a public issue for security vulnerabilities.**

Instead, please send an email to [mediswarmcloud@gmail.com](mailto:mediswarmcloud@gmail.com) with a description of the issue. We will acknowledge receipt of your vulnerability report as soon as possible and outline the next steps in our response process.

### 📝 What to include in your report:
- 📖 A detailed description of the vulnerability.
- 👣 Steps to reproduce the issue (proof-of-concept).
- 💥 Potential impact of the vulnerability.
- 🛠️ Any suggested fixes (optional).

## 🏗️ Security Architecture

The security design, its controls and their known limitations are documented in three pages, which this file does not repeat:

- [Threat model](docs/threat-model.md): what leaves a site during training, which adversaries are considered, who holds which keys, and what is not implemented.
- [Security](docs/security.md): each control (TLS, encryption at rest, sandbox, access control, audit log, backups, compliance-related features) and where it falls short.
- [Networking](docs/networking.md): the ports and connections each host needs, and how to restrict them.

Limitations to be aware of before reporting, because they are documented design properties rather than undisclosed vulnerabilities:

- The coordinating FL server relays every model update between sites and, in the default configuration, can read it.
- The startup package that every site imports contains every participant's private key, the FL server's private key and, for each site, an admin identity that may run code at every site.
- The web interfaces of different sites exchange status over HTTPS without certificate verification by default, authenticated by one shared token per network.
- Validation and visualization scripts run in containers whose network is not isolated, and the application and worker containers control the host's Docker daemon through an unauthenticated proxy.
- The `swarmed` CLI performs no authentication; anyone who can run it on a SwarmMedHub host can act as any user.

## ⚙️ Secure Configuration

- **🛠️ Environment Setup:** Use `make env` to create `.env`. It generates random passwords and keys and writes the file readable only by its owner.
- **🚫 Production Mode:** Keep `DEBUG=False`, the default in `.env.template`.
- **Startup Packages:** Treat an exported startup package as the study's master credential: move it only over an encrypted, authenticated channel, and restrict access to `workspaces/`, which holds the kits and their private keys on every host.
- **👤 Data Privacy:** Be mindful of PII (Personally Identifiable Information) and PHI (Protected Health Information) when logging or storing data.

## ⚖️ Compliance

SwarmMed has not been assessed for compliance with HIPAA, GDPR or other regulations. The technical controls that relate to the HIPAA Security Rule and to GDPR, and their limitations, are listed in [Security](docs/security.md). Whether a deployment complies depends on the operating institution's configuration and procedures.

## 🔍 Security Scans

You can run these security scans locally before submitting a Pull Request. Neither runs in CI.

### 📦 Dependency & Vulnerability Scanning (Snyk)
```bash
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json
```

### 🐍 Python Static Analysis (Bandit)
```bash
bandit -r apps core manage.py -f json -o bandit_report.json
```
