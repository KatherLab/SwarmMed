# Getting Started

This guide will walk you through the basic steps to get MediSwarm Cloud up and running.

## Prerequisites

*   **Docker and Docker Compose:** You need to have Docker and Docker Compose installed on your system. You can find the installation instructions here: [https://docs.docker.com/engine/install/](https://docs.docker.com/engine/install/)
*   **Git:** You need Git to clone the repository.

## Installation

1.  **Clone the repository:**

    ```bash
    git clone https://github.com/pfeifferis/MediSwarmCloud.git
    cd MediSwarmCloud
    ```

2.  **Build and run the Docker containers:**

    ```bash
    docker compose build
    docker compose up
    ```

3.  **Create a superuser:**

    Open a new terminal and run the following command:

    ```bash
    docker exec -it mediswarmcloud python manage.py createsuperuser
    ```

    Follow the prompts to create a superuser account. This account will be used to log in to the web interface.

## Access the Web Interface

Once the containers are running, you can access the web interface at [http://localhost:8000](http://localhost:8000).

Log in with the superuser credentials you just created.