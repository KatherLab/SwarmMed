---
title: Developer Guide
description: A guide for developers to write scripts for the MedSwarmHub platform.
---

# 💻 Developer Guide

This guide provides information for developers who write scripts to be run on the MedSwarmHub platform. The platform supports three types of scripts:

*   Data Validation Scripts
*   Data Visualization Scripts
*   Training Scripts for NVIDIA FLARE
*   Results Visualization Scripts


!!! tip "Example Code"
    You can find example scripts [here](https://github.com/pfeifferis/MedSwarmHub/tree/main/examples).

!!! tip "Testing Locally"
    You can test your validation and visualization scripts locally by selecting `Test in local environment` in the network settings and reviewing the logs on the logs page.

## 🧪 Validation Scripts

Validation scripts are used to verify the quality, format, and integrity of your data.

### 📝 `validation` Object

Your validation script has access to a global `validation` object with the following methods:

*   `add_check(name, status, message="", details=None)`: Records the result of a validation check.
    *   `name` (str): The name of the check (e.g., "Check for missing values").
    *   `status` (str): The outcome, which can be `"ok"`, `"info"`, `"warning"` or `"error"`.
    *   `message` (str): A descriptive message about the outcome.
    *   `details` (dict): A dictionary for any additional information.
*   `get_data_path(relative_path="")`: Gets the local path to a file or directory from your project's data folder. Files are downloaded from the cloud storage on demand.
*   `open(relative_path, mode='r', **kwargs)`: Opens a file from your project's data folder.
*   `exists(relative_path)`: Checks if a file or directory exists.
*   `listdir(relative_path="")`: Lists the contents of a directory.

!!! warning "Available packages for validation and visualization"
    The execution environment (sandbox) comes pre-installed with a wide range of data science and machine learning libraries:

    *   **Numerical & Data:** `numpy`, `pandas`, `scikit-learn`
    *   **Plotting:** `matplotlib`, `seaborn`
    *   **Deep Learning:** `torch`, `tensorflow`, `keras`, `pytorch-lightning`
    *   **NLP & Vision:** `transformers`, `datasets`, `monai`
    *   **Utilities:** `fsspec`, `aiohttp`, `requests`

??? example "Example Validation Script"
    ```python title="validation.py" linenums="1"
    import pandas as pd

    # Initialize validation results
    validation_results = {
        'critical_errors': 0,
        'warnings': 0,
        'info_checks': 0
    }

    def add_check_with_count(name, status, message, details=None):
        """Add a check and update counters"""
        validation.add_check(name, status, message, details or {})
        if status == 'error':
            validation_results['critical_errors'] += 1
        elif status == 'warning':
            validation_results['warnings'] += 1
        else:
            validation_results['info_checks'] += 1

    # 1. FOLDER STRUCTURE VALIDATION
    validation.add_check("Validation Started", "ok", "Beginning comprehensive data validation")

    required_folders = ['patients', 'studies']
    optional_folders = ['reports', 'backup']

    try:
        root_items = validation.listdir()
        folders = [item for item in root_items if item.endswith('/')]
        folder_names = [f.rstrip('/') for f in folders]
        
        for req_folder in required_folders:
            if req_folder in folder_names:
                add_check_with_count(f"Required Folder: {req_folder}", "ok", f"Found required folder '{req_folder}'")
            else:
                add_check_with_count(f"Required Folder: {req_folder}", "error", f"Missing required folder '{req_folder}'")
        
        for opt_folder in optional_folders:
            if opt_folder in folder_names:
                add_check_with_count(f"Optional Folder: {opt_folder}", "ok", f"Found optional folder '{opt_folder}'")
        
        
    except Exception as e:
        add_check_with_count("Folder Structure", "error", f"Could not read root directory: {str(e)}")

    # 2. PATIENTS DATA VALIDATION
    try:
        patients_files = validation.listdir('patients')
        required_patient_files = ['demographics.csv', 'medical_history.csv', 'contact_info.json']
        
        for req_file in required_patient_files:
            if req_file in patients_files:
                add_check_with_count(f"Patient File: {req_file}", "ok", f"Found required file '{req_file}'")
            else:
                add_check_with_count(f"Patient File: {req_file}", "error", f"Missing required file '{req_file}'")
        
        # Validate demographics CSV
        if 'demographics.csv' in patients_files:
            try:
                with validation.open('patients/demographics.csv', 'r') as f:
                    df_demographics = pd.read_csv(f)
                
                # Check required columns
                required_cols = ['patient_id', 'age', 'gender', 'enrollment_date', 'study_group']
                missing_cols = [col for col in required_cols if col not in df_demographics.columns]
                
                if missing_cols:
                    add_check_with_count("Demographics Columns", "error", f"Missing columns: {missing_cols}")
                else:
                    add_check_with_count("Demographics Columns", "ok", "All required columns present")
                
                # Check data quality
                patient_count = len(df_demographics)
                add_check_with_count("Patient Count", "ok", f"Found {patient_count} patients")
                
                # Age validation
                if 'age' in df_demographics.columns:
                    invalid_ages = df_demographics[(df_demographics['age'] < 18) | (df_demographics['age'] > 120)]
                    if len(invalid_ages) > 0:
                        add_check_with_count("Age Validation", "warning", f"{len(invalid_ages)} patients with invalid ages")
                    else:
                        add_check_with_count("Age Validation", "ok", "All ages are valid (18-120)")
                
                # Check for duplicates
                duplicates = df_demographics['patient_id'].duplicated().sum()
                if duplicates > 0:
                    add_check_with_count("Patient ID Duplicates", "error", f"Found {duplicates} duplicate patient IDs")
                else:
                    add_check_with_count("Patient ID Duplicates", "ok", "No duplicate patient IDs found")
                    
            except Exception as e:
                add_check_with_count("Demographics Validation", "error", f"Could not validate demographics.csv: {str(e)}")

    except Exception as e:
        add_check_with_count("Patients Directory", "error", f"Could not access patients directory: {str(e)}")

    # Summary
    validation.add_check("Validation Summary", "ok", 
                    f"Completed validation: {validation_results['critical_errors']} errors, " +
                    f"{validation_results['warnings']} warnings, " +
                    f"{validation_results['info_checks']} info checks")
    ```

## 📊 Visualization Scripts

Visualization scripts allow you to generate plots and charts to explore your data.

### 📝 `visualization` Object

Your visualization script has access to a global `visualization` object with the following methods:

*   `save_plot(title="Untitled Plot")`: Saves the current Matplotlib figure as a plot. You can save up to 4 plots.
*   `get_data_path(relative_path="")`: Gets the local path to a file or directory.
*   `open(relative_path, mode='r', **kwargs)`: Opens a data file.
*   `exists(relative_path)`: Checks if a file or directory exists.
*   `listdir(relative_path="")`: Lists the contents of a directory.

!!! warning "Available packages for visualization"
    Only standard Python packages and the following additional packages are available in visualization scripts:

    *   `numpy`
    *   `pandas`
    *   `matplotlib`
    *   `seaborn`

??? example "Example Visualization Script"
    ```python title="visualization.py" linenums="1"
    import pandas as pd
    import matplotlib.pyplot as plt

    # Read patient demographics data
    try:
        with visualization.open('patients/demographics.csv', 'r') as f:
            df_demo = pd.read_csv(f)
        
        # Plot 1: Age Distribution
        plt.figure(figsize=(10, 6))
        plt.hist(df_demo['age'], bins=15, alpha=0.7, color='skyblue', edgecolor='black')
        plt.title('Age Distribution of Patients')
        plt.xlabel('Age')
        plt.ylabel('Frequency')
        plt.grid(True, alpha=0.3)
        visualization.save_plot("Age Distribution")
        
        # Plot 2: Gender Distribution
        plt.figure(figsize=(8, 6))
        gender_counts = df_demo['gender'].value_counts()
        plt.pie(gender_counts.values, labels=gender_counts.index, autopct='%1.1f%%', startangle=90)
        plt.title('Gender Distribution')
        visualization.save_plot("Gender Distribution")
        
        # Read medical history if available
        if visualization.exists('patients/medical_history.csv'):
            with visualization.open('patients/medical_history.csv', 'r') as f:
                df_medical = pd.read_csv(f)
            
            # Merge datasets
            df_merged = pd.merge(df_demo, df_medical, on='patient_id')
            
            # Plot 3: BMI by Study Group
            plt.figure(figsize=(10, 6))
            study_groups = df_merged['study_group'].unique()
            for group in study_groups:
                group_data = df_merged[df_merged['study_group'] == group]
                plt.hist(group_data['bmi'], alpha=0.6, label=group, bins=10)
            plt.title('BMI Distribution by Study Group')
            plt.xlabel('BMI')
            plt.ylabel('Frequency')
            plt.legend()
            plt.grid(True, alpha=0.3)
            visualization.save_plot("BMIs by Study Group")
            
            # Plot 4: Age vs BMI Scatter Plot
            plt.figure(figsize=(10, 6))
            colors = {'treatment': 'red', 'control': 'blue'}
            for group in study_groups:
                group_data = df_merged[df_merged['study_group'] == group]
                plt.scatter(group_data['age'], group_data['bmi'], 
                        c=colors.get(group, 'gray'), label=group, alpha=0.6)
            plt.title('Age vs BMI by Study Group')
            plt.xlabel('Age')
            plt.ylabel('BMI')
            plt.legend()
            plt.grid(True, alpha=0.3)
            visualization.save_plot("Age vs BMI")

    except Exception as e:
        print(f"Error creating visualizations: {e}")
    ```


## 🧠 Training Scripts

Your custom training code is executed within the NVIDIA FLARE framework. To facilitate the interaction with the FLARE environment, a special `flare_adapter.py` module is provided.

### 🔌 The `flare_adapter` Module

This module provides a simplified, streaming interface to handle communication with the FLARE server and access project data.

*   `init_flare()`: Initializes the FLARE client.
*   `get_data_filesystem(project_id)`: Returns a virtual filesystem object (`fs`) for streaming data.
    *   `fs.ls(path)`: Lists available files.
    *   `fs.glob(pattern)`: Finds files using pattern matching (e.g., `*.csv`).
    *   `fs.open(path)`: Returns a file-like object for streaming (compatible with Pandas, PyTorch, etc.).
*   `receive_model()`: Receives the latest global model from the server.
*   `send_model(params, metrics)`: Sends your updated local model and metrics back to the server.

??? example "Example Training Script"
    ```python title="train.py"
    import torch
    import pandas as pd
    import flare_adapter

    flare_adapter.init_flare()

    # 1. Access the streaming filesystem
    with flare_adapter.get_data_filesystem(project_id) as fs:
        
        # 2. Stream data directly into your favorite library
        with fs.open("data/train.csv") as f:
            df = pd.read_csv(f)

        # 3. Standard Training Loop
        model = torch.nn.Linear(df.shape[1], 1)
        
        while True:
            input_model = flare_adapter.receive_model()
            if not input_model: break
            
            # Load weights
            model.load_state_dict(flare_adapter.get_pytorch_state_dict(input_model.params))
            
            # ... Train ...
            
            # 4. Send updates back
            flare_adapter.send_model(model.state_dict(), metrics={"loss": 0.1})
    ```

        ```bash title="requirements.txt"

        torch

        scikit-learn

        ```

    

    ## Customizing the Sandbox Environment

    

    The execution environment for validation and visualization scripts is defined by a Docker image (`medswarmhub-sandbox`).

    

    ### For Users

    If your script requires a Python package that is not currently available in the sandbox, please contact your platform administrator. The standard set of packages is chosen to balance functionality and security.

    

    ### For Administrators

    To add additional packages to the sandbox environment:

    

    1.  Open `Dockerfile.sandbox` in the project root.

    2.  Add the desired packages to the `RUN uv pip install` command.

    3.  Rebuild and restart the sandbox container:

        ```bash
        make sandbox-build
        make sandbox-up
        ```

    

    The platform will automatically detect the changes and rebuild the internal `medswarmhub-sandbox` image during the next script execution.

    