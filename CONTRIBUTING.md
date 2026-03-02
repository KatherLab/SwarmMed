# Contributing to SwarmCloud

Welcome! We are thrilled that you are interested in contributing to SwarmCloud. This project is a decentralized bio data storage and collaborative training platform leveraging Swarm Learning (NVIDIA FLARE).

This guide will help you get onboarded and explain how to contribute effectively.

---

## 🚀 Getting Started

1.  **Fork the repository** on GitHub.
2.  **Clone your fork** locally:
    ```bash
    git clone https://github.com/your-username/SwarmCloud.git
    cd SwarmCloud
    ```
3.  **Set up your environment**:
    - Run `make install` to install uv (if needed) and synchronize the Python dependencies into `.venv`.
    - Install frontend tools: `npm install`
4.  **Configure environment variables**:
    - Copy `.env.example` to `.env` (if provided) or create one with your DB, Redis, and S3 credentials.
    - Edit `.env` with your secrets before you bring the services up.
5.  **Prepare the application stack**:
    - `make setup` (creates `.env` if needed, runs the PgBouncer helper, and generates TLS material)
    - `make start` (builds the Docker services and brings the stack online)
    - Stop the stack when you are done: `make stop`
6.  **Run migrations and start the server**:
    - `make manage-migrate` to apply migrations inside the app container
    - `make manage-shell` or `make manage-test` for interactive work or tests

If you want to work with Django directly (without Docker), continue using `python manage.py ...` as before.

---

## 🛠 Development Workflow

### During Development

When working locally, ensure your virtual environment is active:
```bash
source venv/bin/activate
# To exit:
deactivate
```

### Frontend Development (CSS & JS)

To change CSS files locally and see changes in real-time, run these commands in a new terminal:
```bash
npm i
npm run build
npx tailwindcss -i ./static/assets/style.css -o ./static/dist/css/output.css --watch
npx webpack --watch
```

### When Changing Tasks

If you modify any Celery tasks in `tasks.py`, you **must** restart the worker to apply the changes:
```bash
docker compose restart celery_worker
```

---

## 🏗 App Structure

The project follows a modular Django architecture. Each specific functionality is encapsulated in an app within the `apps/` directory:

| App | Description |
| :--- | :--- |
| **`core`** | Project configuration, settings, Celery initialization, and root URLs. |
| **`home`** | Main dashboard, statistics aggregation, and overview cards. |
| **`apps.users`** | User authentication, profiles, and role-based access control (Admin, Developer, User). |
| **`apps.project`** | Collaborative project management and code/requirement script uploads. |
| **`apps.data`** | Management of datasets, S3 storage integration, and data validation. |
| **`apps.network`** | Infrastructure provisioning for Swarm networks using Docker. |
| **`apps.training`** | Job submission to NVIDIA FLARE, status tracking, and real-time log streaming. |
| **`apps.results`** | Synchronization of training results from S3 and automated visualization runs. |
| **`apps.logs`** | Centralized, project-specific logging stored in the database. |
| **`apps.communication`**| Internal messaging system for project participants. |

---

## 📜 Coding Guidelines

To maintain a clean and readable codebase, we strictly follow these rules:

### 1. PEP 8 Compliance
All Python code must adhere to [PEP 8](https://peps.python.org/pep-0008/) standards. We use the following tools to maintain code quality:

- **Ruff** (Fast linting and automatic fixes):
  ```bash
  python -m ruff check . --fix
  ```
- **autopep8** (Automatic code formatting):
  ```bash
  python -m autopep8 --in-place --recursive --aggressive --aggressive . --exclude=venv,node_modules,workspaces,migrations,postgres_data,staticfiles,staticfiles_build,nvflare_swarm_learning
  ```
- **flake8** (Final compliance verification):
  ```bash
  python -m flake8 . --exclude=.venv,node_modules,migrations,postgres_data,staticfiles,staticfiles_build,nvflare_swarm_learning,workspaces,example --max-line-length=120 --statistics --count
  ```

*Note: While standard PEP 8 suggests 79-88 characters, this project allows up to **120 characters** for better readability.*

### 2. Security Scans
We prioritize security. Please run these scans before submitting a Pull Request:

- **Snyk** (Dependency & Code Vulnerabilities):
  ```bash
  snyk test --json-file-output=snyk_report.json
  snyk code test --json-file-output=snyk_code_report.json
  ```
- **Bandit** (Common Python Security Issues):
  ```bash
  bandit -r apps core manage.py -f json -o bandit_report.json    
  ```

### 3. Documentation & Style
We prioritize a "soft onboarding" experience. 
- **Docstrings:** Every module, class, and function must have a clear docstring explaining its purpose.
- **Comments:** Add detailed inline comments explaining the *logic* (the "why"), especially for complex infrastructure code (S3, NVFlare, Celery).
- **Simplicity:** Prefer readable, explicit code over "clever" one-liners.

### 4. Testing
Proactively add unit tests in the respective `tests.py` files of the app you are modifying. Ensure all existing tests pass before submitting a Pull Request.

---

## 📥 How to Contribute

1.  **Branching**: Create a feature branch from `main`: `git checkout -b feature/amazing-feature`.
2.  **Commits**: Use descriptive, atomic commit messages.
3.  **Push**: Push your branch: `git push origin feature/amazing-feature`.
4.  **Pull Request**: Open a PR against the `main` branch. 
    - Describe your changes in detail.
    - Reference any related issues.
    - Attach screenshots if you modified the UI.

---

## 🆘 Need Help?

If you have questions or get stuck, feel free to open an Issue on GitHub. We are here to help!

Thank you for helping us make **SwarmCloud** better!