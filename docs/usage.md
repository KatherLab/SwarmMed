---
title: Usage
description: How to use the MediSwarmCloud platform.
---

# Usage

This page explains how to use the MediSwarmCloud platform in more detail.

## For Admins

??? info "Admin Rights"
    As an admin, you have the ability to manage users and oversee the overall operation of the platform.

### Create Users
As an admin, you can create new users who can participate in federated learning experiments.

1.  Navigate to the **Users** page from the sidebar.
2.  Click on the **Add New User** button.
3.  Fill in the user details:
    *   **Username:** A unique username for the user.
    *   **Email address:** The user's email address.
    *   **Password:** A secure password for the user.
    *   **Password confirmation:** Confirm password for the user.
    *   **Role:** The role of the user. This can be either `admin`, `developer` or `user`.
4.  Click on the **Add User** button to save the new user.
5.  Please share the login credentials with the new user securely.

??? example "Image of user page"
    ![User Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

??? example "Image of Create User form"
    ![Create User](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### Manage Users
As an admin, you can manage existing users, including editing their details or deleting them.

1.  Navigate to the **Users** page from the sidebar.
2.  Find the user you want to manage in the list.
3.  Click on the **Update** button next to the user's name to modify their details.
4.  Make the necessary changes and click on the **Update** button.
5.  To delete a user, click on the **Delete** button next to the user's name and confirm the action.

## For Developers

??? info "Developer Rights"
    As a developer, you can create and manage swarm learning experiments on the MediSwarm Cloud platform and see logs.

!!! info "Developer Guide"
    A more detailed developer guide can be found [here](/developer).

### View Logs

As a developer, you can view the logs of the training jobs to monitor their progress and troubleshoot any issues.

1.  Navigate to the **Logs** page from the sidebar.
2.  You will see different sections with Project, Data, Network, Training, and Results logs.
3.  Click on the **Refresh** button to update the logs.
   
??? example "Image of Logs page"
    ![Logs Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

## For All Users

??? info "User Rights"
    As a user, you can create projects, upload data, set up networks, and start training jobs.

### 1. Create a Project

A project is a workspace for your decentralized learning experiment. It contains the code, data, and configuration.

1.  Navigate to the **Project** page from the sidebar.
2.  Click on the **Add New Project** button.
3.  Fill in the project details:
    *   **Title:** A descriptive name for your project.
    *   **Members:** Add members via the user uuid (the user can find their uuid on the Profile page).
    *   **Description:** A brief description of your project.
4.  Upload your training code. Select only the files you need containing your Python scripts including main script `train.py` and any other necessary files for training your model as `requirements.txt`.
5.  Upload your data validation script as `validation.py` (optional).
6.  Upload your data visualization script as `visualization.py` (optional).
7.  Upload your results visualization script as `results_visualization.py` (optional).
8.  Click on the **Save Project** button to save your project.

!!! warning "All network participants need the same project code"
    All network participants need to have the same project code. Make sure to share the project code with all participants.

??? warning "Project creator rights"
    Only the project creator can **edit**, **finish**, or **delete** the project.

??? example "Image of project page"
    ![Project Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

??? example "Image of create project page"
    ![Create Project](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 2. Select your current project

Before you can upload data or create a network, you need to select your current project.

1.  Navigate to the **Projects** page from the sidebar.
2.  Find the project you want to work on in the list.
3.  Click on the **Set as Current** button next to the project's name.

### 3. Upload Data

After creating a project, you need to upload the data that will be used for training.

1.  Navigate to the **Data** page.
2.  Click on the **Upload Data** button.
3.  Select your **Destination folder** where the data will be stored.
4.  Select the data files from your local machine. The data should be in a format and structure that is compatible with your training code.
5.  Click on the **Upload** button to start the upload process.

??? example "Image of data page"
    ![Data Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

??? example "Image of data upload page"
    ![Data Upload](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 4. View Files

You can view the files you have uploaded to your project.

1.  Navigate to the **Files** page.
2.  you can **Download**, **Rename**, or **Delete** files you have uploaded.

??? info "Folder download"
    In the current version only single file download is supported.

??? example "Image of Files page"
    ![Files Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 5. Start Data Validation (optional)

If you have uploaded a data validation script, you can run it to ensure that your data is suitable for training.

1.  Navigate to the **Data** page.
2.  Click on the **Start Validation** button.
3.  Wait for the validation process to complete. You can monitor the progress on the **Logs** page.
4.  Once the validation is complete, you can view the results on the same **Data** page.

??? example "Image of data validation"
    ![Data Validation](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 6. View Data Visualization (optional)

If you have uploaded a data visualization script, you can run it to visualize your data.

1.  Navigate to the **Data** page.
2.  Click on the **Generate Plots** button.
3.  Wait for the visualization process to complete. You can monitor the progress on the **Logs** page.
4.  Once the visualization is complete, you can view the plots on the same **Data** page.

??? example "Image of data visualization"
    ![Data Visualization](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 7. Create a Network

A network defines the participants in your decentralized learning experiment.

1.  Navigate to the **Network** page.
2.  Click on the **Add New Network** button.
3.  Fill in the network details:
    *   **Title:** A descriptive name for your network.
    *   **Description:** A brief description of your network.
4. Select the **Creation Method**:
    *   **Create New Startup Package:** You will provide the necessary configurations, and the platform will create and configure the network for you.
    *   **Upload Startup Package:** You will need to upload a startup package provided by another network partner, who created the startup package.
    *   **Test in local environment:** This option is for testing purposes only. It allows you to run the training locally without setting up a real network.

!!! warning "Only create `New Startup Package` ones for a network"
    Only one startup package can be created for a network. And the startup packages can be downloaded to provided it to other network members for `Upload Startup Package`.
        
5.  If `Create New Startup Package` was selected: Add participants to the network. For each participant, you need to provide:
    *   **Hostname:** A unique name for the participant, displayed on the Network page of the participant.
    *   **IP Address:** The IP address of the participant's machine, displayed on the Network page of the participant.

6. If `Upload Startup Package` was selected: Upload the startup package file provided by another network partner.
7. Click on the **Create Network** button to save your network.

??? warning "Network editing"
    Networks cant be edited in order to the provision of startup kits.

??? example "Image of network page"
    ![Network Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

??? example "Image of create network page"
    ![Create Network](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 8. Select your current network

Before you can start training, you need to select your current network.

1.  Navigate to the **Networks** page from the sidebar.
2.  Find the network you want to work on in the list.
3.  Click on the **Set as Current** button next to the network's name.

### 9. Start Network

After creating a network, you need to start it to enable communication between participants.

1.  Navigate to the **Network** page.
2.  Click on the **Start Network** button.

### 10. Start Training

Once you have set up your project, data, and network, you can start the training process.

1.  Navigate to the **Training** page.
2.  Click on the **Start Training** button.
3.  You can monitor the progress of the training job on the **Logs** page.
4.  Once the training job is complete, you can view the results on the **Results** page.

??? example "Image of training page"
    ![Training Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 11. Download Results

After the training job is complete, you can view the results.

1.  Navigate to the **Results** page
2.  Click on the **Download Model** button, to download the trained model.
3.  Click on the **Download Stats** button, to download the training stats.
4.  Click on the **Download Logs** button, to download the training logs.

??? example "Image of results page"
    ![Results Page](https://amadine.com/assets/img/articles/ui-design/contemporary-ui-design@2x.jpg)

### 12. View Results Visualization (optional)

If you have uploaded a results visualization script, you can run it to visualize your results.

1.  Navigate to the **Results** page.
2.  Click on the **Generate Plots** button.
3.  Wait for the visualization process to complete. You can monitor the progress on the **Logs** page.
4.  Once the visualization is complete, you can view the plots on the same **Results** page.