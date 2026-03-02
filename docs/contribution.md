---
title: Contribution Guide
description: Detailed guidelines for contributing SwarmCloud.
---

# Contribution Guide

Welcome to the **SwarmCloud** developer community! This document provides detailed information on how to set up your environment, follow our coding standards, and successfully contribute to the project.

## 🏗 System Overview

SwarmCloud is a modular Django-based platform designed for decentralized data management and Swarm Learning. 
### Core Technology Stack
- **Backend:** Django 6.0, Celery, Redis.
- **AI/ML:** NVIDIA FLARE (NVFlare) for Swarm Learning.
- **Storage:** S3-compatible storage (MinIO for local dev).
- **Frontend:** Tailwind CSS, Flowbite, Webpack.
- **Infrastructure:** Docker & Docker Compose.

---

## 🛠 Local Development Setup

### 1. Python Environment
Use the Makefile to provision uv and the locked dependencies into `.venv`:
```bash
make install
```

If you prefer to manage the Python tooling manually you can install uv and sync the requirements yourself:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv pip sync requirements.txt
```

### 2. Frontend Assets
Install Node.js dependencies:
```bash
npm install
```

To change CSS files locally and see changes in real-time, run these commands in a new terminal:
```bash
npm run build
npx tailwindcss -i ./static/assets/style.css -o ./static/dist/css/output.css --watch
npx webpack --watch
```

### 3. Services (Docker)
The project relies on Redis, PostgreSQL, and MinIO. Use the provided `docker-compose.yml` to start these services:
```bash
docker compose up -d
```

### 4. Django Initialization
```bash
python manage.py migrate
python manage.py runserver
```

---

## 💻 Development Workflow

### Docker Configuration
When working locally with Docker, you often use volumes for live code updates. However, for production, these should be removed:
```yaml
# Remove these in production within docker-compose.yml:
volumes:
  - ./:/app
ports:
  - "8000:8000"
command: python manage.py runserver 0.0.0.0:8000
```

### When Changing Tasks
If you modify any Celery tasks in `tasks.py`, you **must** restart the worker to apply the changes:
```bash
docker compose restart celery_worker
```

---

## 📜 Coding Guidelines

### PEP 8 & Python Style
We strictly adhere to **PEP 8**. Your code should be clean, readable, and well-commented.
- Use meaningful variable and function names.
- Provide type hints where possible.
- **Crucial:** Every function and class must have a docstring.
- Add detailed comments for logic involving background tasks (Celery) or infrastructure (NVFlare).

### Security Scans
We prioritize security. Please run these scans before submitting a Pull Request:

**Snyk (Dependency & Code Vulnerabilities):**
```bash
snyk test --json-file-output=snyk_report.json
snyk code test --json-file-output=snyk_code_report.json
```

**Bandit (Common Python Security Issues):**
```bash
bandit -r apps core home manage.py -f json -o bandit_report.json    
```

### Frontend Standards
- Use **Tailwind CSS** utility classes for styling.
- Follow the **Material Design** principles established in the templates.
- Ensure components are responsive and accessible.

---

## 📂 Project Structure

- `apps/`: Contains all functional modules (users, project, training, etc.).
- `core/`: Project-wide settings and configuration.
- `templates/`: HTML templates organized by app.
- `static/`: Source assets (CSS, JS) before bundling.
- `workspaces/`: Local directory for NVFlare job data and logs.

---

## 🤝 Contribution Process

1. **Find an issue** or open a new one to discuss your ideas.
2. **Fork and Branch:** Create a branch like `feature/your-feature-name`.
3. **Develop & Test:** Ensure your code passes all linting and logic checks.
4. **Pull Request:** Submit a PR with a clear description of the "why" and "what".

---

For a quick reference, see the [CONTRIBUTING.md](https://github.com/pfeifferis/SwarmCloud/blob/main/CONTRIBUTING.md) file in the root directory.