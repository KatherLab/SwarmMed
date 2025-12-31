---
title: Contribution Guide
description: Detailed guidelines for contributing to SwarmCloud.
---

# Contribution Guide

Welcome to the **SwarmCloud** developer community! This document provides detailed information on how to set up your environment, follow our coding standards, and successfully contribute to the project.

## 🏗 System Overview

SwarmCloud is a modular Django-based platform designed for decentralized medical data management and Swarm Learning. 

### Core Technology Stack
- **Backend:** Django 4.2, Celery, Redis.
- **AI/ML:** NVIDIA FLARE (NVFlare) for Swarm Learning.
- **Storage:** S3-compatible storage (MinIO for local dev).
- **Frontend:** Tailwind CSS, Flowbite, Webpack.
- **Infrastructure:** Docker & Docker Compose.

---

## 🛠 Local Development Setup

### 1. Python Environment
Create and activate a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

Install Python dependencies:
```bash
pip install -r requirements.txt
```

### 2. Frontend Assets
Install Node.js dependencies and start the asset watchers:
```bash
npm install
npm run dev
```

### 3. Services (Docker)
The project relies on Redis, PostgreSQL, and MinIO. Use the provided `docker-compose.yml` to start these services:
```bash
docker-compose up -d
```

### 4. Django Initialization
```bash
python manage.py migrate
python manage.py runserver
```

---

## 📜 Coding Guidelines

### PEP 8 & Python Style
We strictly adhere to **PEP 8**. Your code should be clean, readable, and well-commented.
- Use meaningful variable and function names.
- Provide type hints where possible.
- **Crucial:** Every function and class must have a docstring.
- Add detailed comments for logic involving background tasks (Celery) or infrastructure (NVFlare).

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

For a quick reference, see the [CONTRIBUTING.md](https://github.com/your-repo/SwarmCloud/blob/main/CONTRIBUTING.md) file in the root directory.
