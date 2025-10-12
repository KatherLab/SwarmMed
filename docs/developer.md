# Developer Guide

This guide provides information for developers who want to contribute to the MediSwarm Cloud project.

## Local Development Setup

For local development, you can run the application without Docker.

1.  **Create a virtual environment:**

    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

2.  **Install dependencies:**

    ```bash
    pip install -r requirements.txt
    npm install
    ```

3.  **Run the development server:**

    ```bash
    python manage.py runserver
    ```

4.  **Build static files:**

    In a new terminal, run the following commands to build the CSS and JavaScript files:

    ```bash
    npx tailwindcss -i ./static/assets/style.css -o ./static/dist/css/output.css --watch
    npx webpack --watch
    ```

## Coding Conventions

*   **Python:** We follow the [PEP 8](https://www.python.org/dev/peps/pep-0008/) style guide for Python code.
*   **JavaScript:** We use [Prettier](https://prettier.io/) for formatting JavaScript code.
*   **Git:** Please follow the conventional commit message format.

## Contributing

We welcome contributions to the MediSwarm Cloud project. If you would like to contribute, please follow these steps:

1.  Fork the repository.
2.  Create a new branch for your feature or bug fix.
3.  Make your changes.
4.  Write tests for your changes.
5.  Submit a pull request.