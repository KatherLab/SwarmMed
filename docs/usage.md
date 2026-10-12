---
title: Usage
description: How to use the SwarmMedHub platform.
---

# Usage

This page explains how to use the SwarmMedHub web interface in more detail. Open it at `https://<host>:5085`, where `<host>` is a name or address of the SwarmMedHub host listed in its `DJANGO_ALLOWED_HOSTS` ([Installation](installation.md)).

## 👮 For Admins

??? info "Admin Rights"
    As an admin, you have the ability to manage users and oversee the overall operation of the platform.

### 👤 Create Users
As an admin, you can create new users who can participate in decentralized learning experiments.

1.  Navigate to the **Users** page from the sidebar.
2.  Click on the **Add New User** button.
3.  Fill in the user details:
    *   **Username:** A unique username for the user.
    *   **First name:** The user's first name.
    *   **Last name:** The user's last name.
    *   **Email address:** The user's email address.
    *   **Password:** A secure password for the user.
    *   **Password confirmation:** Confirm password for the user.
    *   **Role:** The role of the user. This can be either `admin`, `developer` or `user`.
4.  Click on the **Add User** button to save the new user.
5.  Please share the login credentials with the new user securely.

!!! info "What the roles control"
    Roles control only user management (`admin`) and the **Logs** page (`developer` and `admin`). Everything else, from creating projects to starting training, needs only an account and, inside a project, membership of that project ([Security](security.md#access-control-rbac)).

??? example "Image of create user form"
    ![Create User](assets/usage/users_new_dark.png#only-dark)
    ![Create User](assets/usage/users_new_light.png#only-light)

### ⚙️ Manage Users
As an admin, you can manage existing users, including editing their details or deleting them.

1.  Navigate to the **Users** page from the sidebar.
2.  Find the user you want to manage in the list.
3.  Click on the **Update** button next to the user's name to modify their details.
4.  Make the necessary changes and click on the **Update** button.
5.  To delete a user, click on the **Delete** button next to the user's name and confirm the action.

!!! info "Login lockout"
    After five failed logins, a username is locked out for one hour from the client address the attempts came from; the same user can still log in from other addresses. To lift a lockout earlier, run on the SwarmMedHub host:

    ```bash
    docker exec -w /app swarmmedhub python3 manage.py axes_reset_username <username>
    ```
   
??? example "Image of user page"
    ![User Page](assets/usage/users_dark.png#only-dark)
    ![User Page](assets/usage/users_light.png#only-light)

## 💻 For Developers

??? info "Developer Rights"
    As a developer, you can create and manage swarm learning experiments on the SwarmMedHub platform and see logs.

!!! info "Developer Guide"
    A more detailed developer guide can be found [here](developer.md).

### 📜 View Logs

As a developer, you can view the logs of the training jobs to monitor their progress and troubleshoot any issues.

1.  Navigate to the **Logs** page from the sidebar.
2.  You will see different sections with Project, Data, Network, Training, and Results logs.
3.  Click on the **Refresh** button to update the logs.
   
??? example "Image of logs page"
    ![Logs Page](assets/usage/logs_dark.png#only-dark)
    ![Logs Page](assets/usage/logs_light.png#only-light)

## 👥 For All Users

??? info "User Rights"
    As a user, you can create projects, import data, set up networks, and start training jobs.

!!! info "CLI workflow"
    The same workflow is also available through the `swarmed` CLI, which runs inside the application container on the SwarmMedHub host. See the dedicated [CLI guide](cli.md) for command-line equivalents of the steps below.

!!! info "Shared Project Bundle"
    SwarmMedHub and `swarmed` use the same project code bundle. The required root entrypoint is `training.py`, with optional `requirements.txt`, `validation.py`, `visualization.py`, and `results_visualization.py`.

### 1. 📁 Create a Project

??? info "Project creator rights"
    Only the project creator can **edit**, **finish**, or **delete** the project.

A project is a workspace for your decentralized learning experiment. It contains the code, data, and configuration.

1.  Navigate to the **Project** page from the sidebar.
2.  Click on the **Add New Project** button.
3.  Fill in the project details:
    *   **Title:** A descriptive name for your project.
    *   **Members:** Add members via the user uuid (the user can find their uuid on the Profile page).
    *   **Description:** A brief description of your project.
4.  Import your training code. Select only the files you need containing your Python scripts including main script `training.py`.
5.  Import your requirements file as `requirements.txt`.
6.  Import your data validation script as `validation.py` (optional).
7.  Import your data visualization script as `visualization.py` (optional).
8.  Import your results visualization script as `results_visualization.py` (optional).
9.  Click on the **Save Project** button to save your project.

!!! warning "All network participants need the same project code"
    All network participants need to have the same project code. Make sure to share the project code with all participants.

!!! danger "Main training script name"
    The main training script must be named `training.py`. This is the script that will be executed when you start the training job. If the project's stored code has no `training.py` when training is started, for example while the object store is unavailable, SwarmMedHub does not submit the job.

??? example "Image of project page"
    ![Project Page](assets/usage/project_dark.png#only-dark)
    ![Project Page](assets/usage/project_light.png#only-light)

??? example "Image of create project page"
    ![Create Project](assets/usage/project_new_dark.png#only-dark)
    ![Create Project](assets/usage/project_new_light.png#only-light)

### 2. ✅ Select your current project

Before you can import data or create a network, you need to select your current project.

1.  Navigate to the **Projects** page from the sidebar.
2.  Find the project you want to work on in the list.
3.  Click on the **Set** button next to the project's name.

### 3. 📤 Import Data

After creating a project, you need to import the data that will be used for training.

1.  Navigate to the **Data** page from the sidebar.
2.  Click on the **Import Data** button.
3.  Select your **Destination folder** where the data will be stored.
4.  Select the data files from your local machine. The data should be in a format and structure that is compatible with your training code.
5.  Click on the **Import** button to start the import process.

??? example "Image of data page"
    ![Data Page](assets/usage/data_dark.png#only-dark)
    ![Data Page](assets/usage/data_light.png#only-light)

??? example "Image of data import page"
    ![Data Import](assets/usage/data_new_dark.png#only-dark)
    ![Data Import](assets/usage/data_new_light.png#only-light)

### 4. 📂 View Files

You can view the files you have imported to your project.

1.  Navigate to the **Data** page from the sidebar.
2.  Click on the **View Files** button.
3.  you can **Export**, **Rename**, or **Delete** files you have imported.

!!! info "Folder export"
    In the current version only single file export is supported.

??? example "Image of Files page"
    ![Files Page](assets/usage/files_dark.png#only-dark)
    ![Files Page](assets/usage/files_light.png#only-light)

### 5. 🧪 Start Data Validation (optional)

If you have imported a data validation script, you can run it to ensure that your data is suitable for training.

1.  Navigate to the **Data** page from the sidebar.
2.  Click on the **Start Validation** button.
3.  Wait for the validation process to complete. You can monitor the progress on the **Logs** page.
4.  Once the validation is complete, you can view the results on the same **Data** page.

??? example "Image of data validation results"
    ![Data Validation](assets/usage/data_val_dark.png#only-dark)
    ![Data Validation](assets/usage/data_val_light.png#only-light)

### 6. 📊 View Data Visualization (optional)

If you have imported a data visualization script, you can run it to visualize your data.

1.  Navigate to the **Data** page from the sidebar.
2.  Click on the **Generate Plots** button.
3.  Wait for the visualization process to complete. You can monitor the progress on the **Logs** page.
4.  Once the visualization is complete, you can view the plots on the same **Data** page.

??? example "Image of data visualization results"
    ![Data Visualization](assets/usage/data_viz_dark.png#only-dark)
    ![Data Visualization](assets/usage/data_viz_light.png#only-light)

### 7. 🌐 Create a Network

A network defines the participants in your decentralized learning experiment.

1.  Navigate to the **Network** page from the sidebar.
2.  Click on the **Add New Network** button.
3.  Fill in the network details:
    *   **Title:** A descriptive name for your network.
    *   **Description:** A brief description of your network.
4. Select the **Creation Method**:
    *   **Create New Startup Package:** You will provide the necessary configurations, and the platform provisions the network: it generates the study's certificates and keys and a startup kit for every participant.
    *   **Import Startup Package:** You will need to import the startup package that the network partner who created the network exported.
    *   **Test in local environment:** This option is for testing purposes only. It allows you to run the training locally without setting up a real network.

!!! warning "One site creates the startup package"
    Only one site creates the startup package of a network, with `Create New Startup Package`. It exports the package and gives it to the other network members, who choose `Import Startup Package`.

!!! danger "The startup package is the study's master credential"
    The package contains every participant's private key, the FL server's private key and, for each site, an admin identity with NVFlare's `project_admin` role, which may run code at every site. Every site that imports it holds all of them. Move the package only over an encrypted, authenticated channel to a named person at each site, and delete copies once they are imported ([Key custody](threat-model.md#key-custody-what-the-startup-package-contains)).

5.  If `Create New Startup Package` was selected: Add participants to the network. For each participant, you need to provide:
    *   **Hostname:** A unique name for the participant, displayed on the Network page of the participant.
    *   **IP Address:** The IPv4 address of the participant's machine, which must stay assigned to it for the study. The Network page of the participant displays the machine's Tailscale address. A participant without a valid IPv4 address gets no startup kit.

6. If `Import Startup Package` was selected: Import the startup package file provided by another network partner.
7. Click on the **Create Network** button to save your network.

!!! info "Networks created in the web interface"
    The web interface uses this host's Tailscale address as the FL server's address and always adds this host as a training site. To give another server address, for example on a network other than Tailscale, or to run the FL server on a host that does not train, create the network with the [CLI](cli.md) (`--server-ip`, `--no-local-client`); see [Networking](networking.md#addressing).

!!! info "Network editing"
    Networks cannot be edited, because the participants and their addresses are written into the startup kits when the network is provisioned. Create a new network instead.

??? example "Image of network page"
    ![Network Page](assets/usage/network_dark.png#only-dark)
    ![Network Page](assets/usage/network_light.png#only-light)

??? example "Image of create network page"
    ![Create Network](assets/usage/network_new_dark.png#only-dark)
    ![Create Network](assets/usage/network_new_light.png#only-light)

### 8. ✅ Select your current network

Before you can start training, you need to select your current network.

1.  Navigate to the **Network** page from the sidebar.
2.  Find the network you want to work on in the list.
3.  Click on the **Set as Current** button next to the network's name.

### 9. 🚀 Start Network

After creating a network, you need to start it to enable communication between participants.

1.  Navigate to the **Network** page from the sidebar.
2.  Click on the **Start Network** button.
3.  Starting the network can take a few minutes. You can monitor the progress on the **Logs** page.

!!! warning "Participating sites start in client-only mode"
    Every startup package also contains the FL server's kit. At a participating site, set client-only mode for the Hub's Celery worker before starting the network (`SWARMMEDHUB_CLIENT_ONLY_MODE=true` and `SWARMMEDHUB_LOCAL_PARTICIPANT=<the site's name>`). Otherwise the start also launches a copy of the FL server, and the site's client registers with that copy instead of the coordinator. A coordinating host that does not train uses `SWARMMEDHUB_SERVER_ONLY_MODE=true` instead. These are environment variables, not options in the interface; see [Networking](networking.md).

### 10. 🧠 Start Training

Once you have set up your project, data, and network, you can start the training process.

1.  Navigate to the **Training** page from the sidebar.
2.  Click on the **Start Training** button.
3.  You can monitor the progress of the training job on the **Logs** section of the page.
4.  Once the training job is complete, you can view the results on the **Results** page.

Training can only be started while the network is running on this Hub.

!!! warning "The training code runs at every site"
    The project's training code runs at every participating site, with access to that site's data and network. Every site should review it before taking part ([Threat model](threat-model.md#4-adversaries-considered)).

??? example "Image of training page"
    ![Training Page](assets/usage/training_dark.png#only-dark)
    ![Training Page](assets/usage/training_light.png#only-light)

### 11. 📥 Export Results

After the training job is complete, you can view the results.

1.  Navigate to the **Results** page from the sidebar.
2.  Click on the **Sync Results** button, to sync the results.
3.  Click on the **Export All** button, to export all results from all training jobs.
4.  Or select individual result files to export.

??? example "Image of results page"
    ![Results Page](assets/usage/results_dark.png#only-dark)
    ![Results Page](assets/usage/results_light.png#only-light)

### 12. 📊 View Results Visualization (optional)

If you have imported a results visualization script, you can run it to visualize your results.

1.  Navigate to the **Results** page from the sidebar.
2.  Click on the **Run Visualization** button.
3.  Wait for the visualization process to complete. You can monitor the progress on the **Logs** page.
4.  Once the visualization is complete, you can view the plots on the same **Results** page.

??? example "Image of results visualization"
    ![Results Visualization](assets/usage/results_viz_dark.png#only-dark)
    ![Results Visualization](assets/usage/results_viz_light.png#only-light)

### ✉️ Messages

You can send messages to other project members.

1.  Navigate to the **Messages** page from the sidebar.
2.  Click on **New Chat** button.
3.  Select the recipient from the dropdown menu.
4.  Type your message in the input field and click on the **Send** button.
   
   or

2.  Click on a project board and share a update

!!! info "Automatic updates via Email"
    You will receive automatic email updates about new messages.

!!! info "Messages only with project members of same institution"
    You can only send messages to project members of the same institution, not with other network participants. This is to ensure data privacy and security.

### 🏴 CLI

Most of the above steps can also be performed via the `swarmed` CLI, which runs inside the application container on the SwarmMedHub host. The CLI provides a command-line interface to interact with the SwarmMed platform, allowing you to manage your projects, data, networks, and training jobs directly from your terminal. Please refer to the dedicated [CLI guide](cli.md) for detailed instructions on how to use the CLI for each of the steps mentioned above.
