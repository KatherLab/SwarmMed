---
title: Contribution Guide
description: Detailed guidelines for contributing to SwarmMedHub and the swarmed CLI.
---

# Contribution Guide

Welcome to the **SwarmMed** developer community. This repository contains both SwarmMedHub and the local `swarmed` CLI, and this document provides detailed information on how to set up your environment, follow our coding standards, and successfully contribute to the project.

## 🏗️ System Overview

SwarmMed is a modular Django-based platform designed for decentralized data management and Swarm Learning.

### 💻 Core Technology Stack
- **Backend:** Django 6.0, Celery, Redis.
- **AI/ML:** NVIDIA FLARE (NVFlare) for Swarm Learning.
- **Storage:** S3-compatible storage (MinIO for local dev).
- **Frontend:** Tailwind CSS, Flowbite, Webpack.
- **Infrastructure:** Docker & Docker Compose.

## 🛠️ Local Development Setup

### 1. Python Environment
Use the Makefile to provision uv and the locked dependencies into `.venv`:
```bash
make install
```

### 2. Frontend Assets
Install Node.js dependencies and build assets:
```bash
npm install
npm run build
```

To change CSS files locally and see changes in real-time, run these commands in a new terminal:
```bash
npx tailwindcss -i ./static/assets/style.css -o ./static/dist/css/output.css --watch
npx webpack --watch
```

### 3. Services (Docker)
Use the Makefile to bootstrap the full stack. This handles environment variables, TLS certificates, and PgBouncer configuration automatically:
```bash
make env
make start
```

Use `make stop` to tear the stack down, and `make logs` to follow the `swarmmedhub` container logs.

### 4. Django Initialization
```bash
make migrate        # runs migrations inside the app container
make superuser      # creates an administrative user
```

## 📂 Project Structure

The project follows a modular Django architecture. Each specific functionality is encapsulated in an app within the `apps/` directory:

| App | Description |
| :--- | :--- |
| **`core`** | Project configuration, settings, Celery initialization, and root URLs. |
| **`home`** | Main dashboard, statistics aggregation, and overview cards. |
| **`apps.users`** | User authentication, profiles, and role-based access control (Admin, Developer, User). |
| **`apps.project`** | Collaborative project management and code/requirement script import. |
| **`apps.data`** | Management of datasets, S3 storage integration, and data validation. |
| **`apps.network`** | Infrastructure provisioning for Swarm networks using Docker. |
| **`apps.training`** | Job submission to NVIDIA FLARE, status tracking, and real-time log streaming. |
| **`apps.results`** | Synchronization of training results from S3 and automated visualization runs. |
| **`apps.logs`** | Centralized, project-specific logging stored in the database. |
| **`apps.communication`**| Internal messaging system for project participants. |
| **`apps.backup`** | Encrypted database and media backup/restore utilities. |

## 📜 Coding Guidelines

### 🐍 PEP 8 & Python Style
We strictly adhere to **PEP 8**. Your code should be clean, readable, and well-commented.

- Use **Ruff** for linting and automatic fixes: `python -m ruff check . --fix`.
- Provide type hints where possible.
- Every function and class must have a docstring.
- Add detailed comments for logic involving background tasks (Celery) or infrastructure (NVFlare).

### 🔍 Security Scans
We prioritize security. Please run these scans before submitting a Pull Request:

**Snyk (Dependency & Code Vulnerabilities):**
```bash
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json
```

**Bandit (Common Python Security Issues):**
```bash
bandit -r apps core manage.py -f json -o bandit_report.json    
```

### 🎨 Frontend Standards
- Use **Tailwind CSS** utility classes for styling.
- Ensure components are responsive and accessible.

## 📂 Project Structure

- `apps/`: Contains all functional modules (users, project, training, etc.).
- `core/`: Project-wide settings and configuration.
- `templates/`: HTML templates organized by app.
- `static/`: Source assets (CSS, JS) before bundling.
- `scripts/`: Utility scripts for setup, maintenance, and deployment.
- `infrastructure/`: Certbot, PGBouncer, nginx and postgres configurations.
- `swarmed_cli/`: Local CLI tool for interacting with SwarmMed.

## 🤝 Contribution Process

1. **Find an issue** or open a new one to discuss your ideas.
2. **Fork and Branch:** Create a branch like `feature/your-feature-name`.
3. **Develop & Test:** Ensure your code passes all linting and logic checks.
4. **Pull Request:** Submit a PR with a clear description of the "why" and "what".
