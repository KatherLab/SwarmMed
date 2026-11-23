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
    SwarmMedHub and `swarmed` use the same project code bundle. The required root entrypoint is `training.py`, with optional `requirements.txt`, `validation.py`, `visualization.py`, `results_visualization.py` and `explainability.py`. To start from a template for your kind of data, see [Templates](templates.md).

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
4.  Import your training code with **Select project folder** (the folder that holds `training.py`) or **Select individual files** (the main script `training.py` and any Python modules it imports). A folder's files are stored relative to the selected folder, without the folder's own name, so its `training.py` is at the root of the training code, where training looks for it, and subfolders are kept. An upload without a root-level `training.py` is refused. Only `.py` files are taken, and each of them is stored as training code, also a folder's `validation.py`, `explainability.py` and other optional scripts; import those in their own fields (steps 6 to 9).
5.  Import your requirements file as `requirements.txt`.
6.  Import your data validation script as `validation.py` (optional).
7.  Import your data visualization script as `visualization.py` (optional).
8.  Import your results visualization script as `results_visualization.py` (optional).
9.  Import your explainability script as `explainability.py` (optional; see [Explainability](explainability.md#in-swarmmedhub)).
10. Click on the **Save Project** button to save your project.

To start from a template, take the files from one `examples/<template>/` folder: its `training.py` in step 4 and each of its other scripts and its `requirements.txt` in its own field ([Templates](templates.md)).

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
4.  Select the data folder on your local machine. Every file is stored below the folder's name (for example `fl-client-1/train.npz`) inside the destination folder. The data should be in a format and structure that is compatible with your training code; each template's README describes the layout its training script expects. Files whose extension is not in `ALLOWED_EXTENSIONS` are skipped on import.
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
    Only one site creates the startup package of a network, with `Create New Startup Package`. It exports the package and gives it to the other network members, who choose `Import Startup Package`. After the export (reload the Network page once the download has finished), and at every site after the import, the Network page shows the package's network fingerprint, which all sites compare by phone or video call before the network is started ([Verifying the startup package](networking.md#verifying-the-startup-package)).

!!! danger "The startup package is the study's master credential"
    The package contains every participant's private key, the FL server's private key and, for each site, an admin identity with NVFlare's `project_admin` role, which may run code at every site. Every site that imports it holds all of them. Move the package only over an encrypted, authenticated channel to a named person at each site, and delete copies once they are imported ([Key custody](threat-model.md#key-custody-what-the-startup-package-contains)).

5.  If `Create New Startup Package` was selected: Add participants to the network. For each participant, you need to provide:
    *   **Hostname:** A unique name for the participant, displayed on the Network page of the participant.
    *   **IP Address:** The IPv4 address of the participant's machine, which must stay assigned to it for the study. The Network page of the participant displays the machine's overlay address (NetBird or Tailscale). A participant without a valid IPv4 address gets no startup kit.

6. If `Import Startup Package` was selected: Import the startup package file provided by another network partner. In **Expected fingerprint**, enter the network fingerprint that the coordinating site read out to you; a package with another fingerprint, or without one, is not imported. Without it, compare the fingerprint shown after the import with every other site.
7. Click on the **Create Network** button to save your network.

!!! info "Networks created in the web interface"
    Leave **FL server address** empty to use this host's overlay address (NetBird or Tailscale), or enter another IPv4 address that every site can reach. With the field empty, the network is not created if the overlay is not connected or the overlay helper has stopped reporting. The web interface always adds this host as a training site; to run the FL server on a host that does not train, create the network with the [CLI](cli.md) (`--no-local-client`); see [Networking](networking.md#addressing).

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

If a running job stops making progress, for example because a site dropped out, a warning appears under the progress bar and as a **Stalled** badge on the dashboard. The job keeps its status; [Server failover](failover.md#when-a-site-drops-out) describes the warning and what to do.

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

### 13. 🔍 Explain and Review Predictions (optional)

If the project has an explainability script, you can run it on a finished job and review the cases it explains.

1.  Navigate to the **Results** page from the sidebar and select the job.
2.  Click on **Run Explainability** in the **Explainability** section. The script runs in the sandbox with its own limits (by default 2 CPUs, 2 GB and 15 minutes). Starting a run cancels the pending or running runs of the job that you may stop: your own, or every run if you are the project author. While a run that another member started is pending or running, and you are not the project author, the start is refused.
3.  Once the run has finished, the section shows its cases in a random order, fixed when the run was stored. Select a case to see its panels and record your review.
4.  The first time you open a project's explanations, **Blinded review** is on: until you save your own read of a case, it shows only the case ID, the label and the input panels, and the run-level figures and the script output appear once every case is read. Later runs start in the mode you chose last. Switching blinded review off, downloading the bundle, listing or exporting the run with the CLI, or opening one of its maps or run-level figures with blinded review off gives you the run's model output: from then on all its cases are shown even with blinded review on, the page says so, and your reviews of that run are recorded as made after seeing the output ([Reviewing cases](explainability.md#reviewing-cases)).

[Explainability](explainability.md#in-swarmmedhub) describes the script, the review and what the explanations can and cannot show.

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
