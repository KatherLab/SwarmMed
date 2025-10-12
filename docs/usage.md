# Usage

This page explains how to use the MediSwarm Cloud platform in more detail.

## 1. Create a Project

A project is a workspace for your federated learning experiment. It contains the code, data, and network configuration.

1.  Navigate to the **Projects** page from the sidebar.
2.  Click on the **New Project** button.
3.  Fill in the project details:
    *   **Name:** A descriptive name for your project.
    *   **Description:** A brief description of your project.
4.  Upload your training code. This should be a zip file containing your Python scripts and any other necessary files for training your model.

## 2. Upload Data

After creating a project, you need to upload the data that will be used for training.

1.  Navigate to the **Data** page.
2.  Select the project you want to add data to from the dropdown menu.
3.  Click on the **Upload Data** button.
4.  Select the data files from your local machine. The data should be in a format that is compatible with your training code (e.g., CSV, images).

## 3. Create a Network

A network defines the participants in your federated learning experiment.

1.  Navigate to the **Networks** page.
2.  Click on the **New Network** button.
3.  Select the project you want to associate the network with.
4.  Add participants to the network. For each participant, you need to provide:
    *   **Name:** A unique name for the participant.
    *   **IP Address:** The IP address of the participant's machine (this should be their Tailscale IP if you are using Tailscale).
    *   **Role:** The role of the participant in the federated learning process. This can be either `server` or `client`.

## 4. Start Training

Once you have set up your project, data, and network, you can start the training process.

1.  Navigate to the **Training** page.
2.  Select the network you want to train on.
3.  Click on the **Start Training** button.

MediSwarm Cloud will then prepare the training job and submit it to the NVIDIA FLARE system. The platform will distribute the training code to the participants and orchestrate the federated learning process.

## 5. View Results

After the training job is complete, you can view the results.

1.  Navigate to the **Results** page.
2.  Select the training job you want to inspect.

The platform will display the following information:

*   **Training Logs:** The logs generated during the training process.
*   **Model Performance:** Metrics and charts showing the performance of the trained model.
*   **Other Artifacts:** Any other files or artifacts generated during training.