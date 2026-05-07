---
title: Developer Guide
description: A guide for developers to write shared project scripts for SwarmMedHub and the swarmed CLI.
---

# Developer Guide

This guide describes the shared project script contract used by both SwarmMedHub and the local `swarmed` CLI. The same project code bundle can be imported through the web UI or ingested locally with `swarmed project create --code-dir PATH` or `swarmed project update --code-dir PATH`.

SwarmMed supports four script types:

*   **Data Validation Scripts:** Verify the quality and format of your datasets.
*   **Data Visualization Scripts:** Explore your datasets through plots and charts.
*   **Training Scripts (NVFlare):** Custom machine learning logic for decentralized training.
*   **Results Visualization Scripts:** Analyze and visualize the models and metrics produced by training.

!!! tip "Example Code"
    You can find example scripts in the [examples/](https://github.com/pfeifferis/SwarmCloud/tree/main/examples) directory of the repository.

!!! tip "Testing Locally"
    You can test your validation and visualization scripts locally either by selecting `Test in local environment` as the creation method in the network settings or by creating a CLI local-test network with `swarmed network create --local-test` and reviewing the output on the logs page.

## 📦 Shared Project Bundle

Both interfaces use the same project bundle layout:

*   **Required:** `training.py`
*   **Optional:** `requirements.txt`, `validation.py`, `visualization.py`, `results_visualization.py`
*   **Additional support files:** Preserved relative to the project root and imported under the training code layout

This shared bundle is what lets the same project be created and executed through either SwarmMedHub or the local `swarmed` workflow.

## 🧪 Validation Scripts

Validation scripts are used to verify the quality, format, and integrity of your data before starting a training experiment.

### `validation` Object

Your validation script has access to a global `validation` object:

*   **`add_check(name, status, message="", details=None)`**: Records the result of a validation check.
    *   **`name`** (str): The name of the check (e.g., "Check for missing values").
    *   **`status`** (str): The outcome, which can be `"ok"`, `"info"`, `"warning"` or `"error"`.
    *   **`message`** (str): A descriptive message about the outcome.
    *   **`details`** (dict): A dictionary for any additional information.
*   **`get_data_path(relative_path="")`**: Gets the local path to a file or directory from your project's data folder. Files are accessed from the object storage storage on demand.
*   **`open(relative_path, mode='r', **kwargs)`**: Opens a file from your project's data folder.
*   **`exists(relative_path)`**: Checks if a file or directory exists.
*   **`listdir(relative_path="")`**: Lists the contents of a directory.

!!! warning "Available packages for validation and visualization"
    The execution environment (sandbox) comes pre-installed with a wide range of data science and machine learning libraries:

    *   **Numerical & Data:** `numpy`, `pandas`, `scikit-learn`
    *   **Plotting:** `matplotlib`, `seaborn`
    *   **Deep Learning:** `torch`, `tensorflow`, `keras`, `pytorch-lightning`
    *   **NLP & Vision:** `transformers`, `datasets`, `monai`
 
??? example "Example Validation Script"
    ```python title="validation.py" linenums="1"
    import pandas as pd

    # Initialize counters for the summary
    validation_results = {"critical_errors": 0, "warnings": 0, "info_checks": 0}


    def add_check_with_count(name, status, message, details=None):
        """Add a check and update counters."""
        validation.add_check(name, status, message, details or {})
        if status == "error":
            validation_results["critical_errors"] += 1
        elif status == "warning":
            validation_results["warnings"] += 1
        else:
            validation_results["info_checks"] += 1


    validation.add_check(
        "Validation Started", "ok", "Beginning biomedical data validation"
    )

    # 1. FOLDER AND FILE STRUCTURE
    data_folder = "biomed_data"

    try:
        # List files in the root or data directory
        root_items = validation.listdir(data_folder)

        # We expect multiple batch files (e.g., batch_1.csv, batch_2.csv)
        batch_files = [
            f for f in root_items if f.startswith("batch_") and f.endswith(".csv")
        ]

        if len(batch_files) > 0:
            add_check_with_count(
                "Batch Files", "ok", f"Found {len(batch_files)} data batch files"
            )
        else:
            add_check_with_count(
                "Batch Files",
                "error",
                f"No batch_*.csv files found in {data_folder}",
            )

        # 2. DATA CONTENT VALIDATION
        required_cols = [
            "patient_id",
            "age",
            "sex",
            "bmi",
            "biomarker_A",
            "biomarker_B",
            "diagnosis",
        ]
        total_patients = 0

        for file_name in batch_files:
            full_path = f"{data_folder}/{file_name}"
            try:
                with validation.open(full_path, "r") as f:
                    df = pd.read_csv(f)

                # Update counts
                total_patients += len(df)

                # Check 1: Schema
                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    add_check_with_count(
                        f"Schema: {file_name}",
                        "error",
                        f"Missing columns: {missing}",
                    )
                else:
                    add_check_with_count(
                        f"Schema: {file_name}", "ok", "All columns present"
                    )

                # Check 2: Age Validity
                if "age" in df.columns:
                    invalid_ages = df[(df["age"] < 0) | (df["age"] > 120)]
                    if len(invalid_ages) > 0:
                        add_check_with_count(
                            f"Age Quality: {file_name}",
                            "warning",
                            f"{len(invalid_ages)} rows with invalid age",
                        )
                    else:
                        add_check_with_count(
                            f"Age Quality: {file_name}", "ok", "Ages are valid"
                        )

                # Check 3: Null Values
                if df.isnull().values.any():
                    add_check_with_count(
                        f"Completeness: {file_name}",
                        "warning",
                        "File contains missing (NaN) values",
                    )
                else:
                    add_check_with_count(
                        f"Completeness: {file_name}",
                        "ok",
                        "No missing values found",
                    )

            except Exception as e:
                add_check_with_count(
                    f"Read Error: {file_name}",
                    "error",
                    f"Could not parse CSV: {str(e)}",
                )

        add_check_with_count(
            "Total Volume", "ok", f"Total dataset size: {total_patients} patients"
        )

    except Exception as e:
        add_check_with_count(
            "Directory Access",
            "error",
            f"Could not access '{data_folder}': {str(e)}",
        )

    # Summary
    validation.add_check(
        "Validation Summary",
        "ok",
        f"Completed: {validation_results['critical_errors']} errors, "
        + f"{validation_results['warnings']} warnings.",
    )
    ```

## 📊 Visualization Scripts

Visualization scripts allow you to generate plots and charts to explore your data.

### `visualization` Object

*   **`save_plot(title="Untitled Plot")`**: Saves the current Matplotlib figure. You can save up to **4 plots** per run.
*   **`get_data_path(relative_path="")`**: Returns the internal URL/path.
*   **`open(relative_path, mode='r', **kwargs)`**: Opens a data file.
*   **`exists(relative_path)`**: Checks if a file exists.
*   **`listdir(relative_path="")`**: Lists contents.

!!! warning "Available packages for visualization"
    Only standard Python packages and the following additional packages are available in visualization scripts:

    *   `numpy`
    *   `pandas`
    *   `matplotlib`
    *   `seaborn`

??? example "Example Visualization Script"
    ```python title="visualization.py" linenums="1"
    import matplotlib.pyplot as plt
    import pandas as pd
    import seaborn as sns

    # Define the directory where data generation script saved files
    data_folder = "biomed_data"

    try:
        # --- 1. Load and Aggregate Data ---
        df_list = []

        # Attempt to load batches 1 through 10
        for i in range(1, 11):
            file_path = f"{data_folder}/batch_{i}.csv"

            # specific check using the provided interface
            if visualization.exists(file_path):
                with visualization.open(file_path, "r") as f:
                    batch_df = pd.read_csv(f)
                    df_list.append(batch_df)

        if not df_list:
            print(
                f"No data found in {data_folder}. Please run the generation script first."
            )
        else:
            # Combine all batches
            df = pd.concat(df_list, ignore_index=True)
            print(f"Successfully loaded {len(df)} records for visualization.")

            # --- Set Seaborn Theme for Prettier Plots ---
            sns.set_theme(style="whitegrid", context="talk")

            # --- Plot 1: Age Distribution (Histogram with KDE) ---
            plt.figure(figsize=(10, 6))
            sns.histplot(
                data=df, x="age", kde=True, color="teal", bins=20, alpha=0.6
            )

            plt.title("Patient Age Distribution", fontsize=16, pad=20)
            plt.xlabel("Age (Years)")
            plt.ylabel("Count")

            visualization.save_plot("Age Distribution")

            # --- Plot 2: Diagnosis Balance (Count Plot) ---
            # Replacing Pie chart with a cleaner Bar plot which is standard in
            # scientific papers
            plt.figure(figsize=(8, 6))
            ax = sns.countplot(data=df, x="diagnosis", palette="viridis")

            plt.title(
                "Target Class Balance (0=Healthy, 1=Diagnosed)",
                fontsize=16,
                pad=20,
            )
            plt.xlabel("Diagnosis Group")
            plt.ylabel("Patient Count")
            plt.bar_label(ax.containers[0])  # Add numbers on top of bars

            visualization.save_plot("Diagnosis Balance")

            # --- Plot 3: Biomarker A vs Diagnosis (Violin Plot) ---
            # Violin plots are often "prettier" and more informative than boxplots
            # for bio-data
            plt.figure(figsize=(10, 6))
            sns.violinplot(
                data=df, x="diagnosis", y="biomarker_A", palette="Set2", split=True
            )

            plt.title("Biomarker A Distribution by Diagnosis", fontsize=16, pad=20)
            plt.xlabel("Diagnosis (0=Healthy, 1=Diagnosed)")
            plt.ylabel("Biomarker Level")

            visualization.save_plot("Biomarker A Separation")

            # --- Plot 4: Age vs BMI Scatter (Risk Analysis) ---
            plt.figure(figsize=(10, 6))
            sns.scatterplot(
                data=df,
                x="age",
                y="bmi",
                hue="diagnosis",
                style="diagnosis",
                palette="deep",
                alpha=0.7,
                s=80,  # larger dot size
            )

            plt.title("Age vs BMI: Risk Factor Clusters", fontsize=16, pad=20)
            plt.xlabel("Age")
            plt.ylabel("BMI")
            plt.legend(title="Diagnosis", loc="upper right")

            visualization.save_plot("Age vs BMI Scatter")

            # --- Plot 5: Correlation Matrix (Heatmap) ---
            plt.figure(figsize=(10, 8))
            corr = df.drop(columns=["patient_id"]).corr()

            # Create a heatmap with diverging colors
            sns.heatmap(
                corr,
                annot=True,
                fmt=".2f",
                cmap="RdBu_r",
                center=0,
                square=True,
                linewidths=0.5,
                cbar_kws={"shrink": 0.8},
            )

            plt.title("Feature Correlation Matrix", fontsize=16, pad=20)
            plt.tight_layout()

            visualization.save_plot("Correlation Matrix")

    except Exception as e:
        print(f"Error creating visualizations: {e}")
    ```

## 📈 Results Visualization Scripts

These scripts are used to analyze the output of a training job. They have access to the models and metrics generated during execution.

### `visualization` Object (Results Context)

In addition to the standard methods, the results visualization object includes:

*   **`get_model(client_name="fl-client-1", model_filename="model.pt")`**: Retrieves a model file and automatically parses it into a NumPy-compatible format (works for PyTorch `.pt`, `.pth`, `.ckpt` and NumPy `.npy`, `.npz`).
*   **`load_weights(model, client_name="fl-client-1", model_filename="model.pt")`**: Automatically loads retrieved weights into a provided model instance (supports PyTorch, Keras, and Scikit-learn).

??? example "Example Results Visualization Script"
    ```python title="results_visualization.py" linenums="1"
    import os
    import matplotlib.pyplot as plt
    import pandas as pd
    import seaborn as sns
    import torch
    import torch.nn as nn
    from sklearn.metrics import (
        auc,
        average_precision_score,
        confusion_matrix,
        precision_recall_curve,
        roc_curve,
    )
    from sklearn.preprocessing import StandardScaler

    # --- 1. Define Model Architecture (Must match training.py) ---


    class BioMedNet(nn.Module):
        def __init__(self, input_dim):
            super().__init__()
            self.layer_1 = nn.Linear(input_dim, 64)
            self.batch_norm1 = nn.BatchNorm1d(64)
            self.layer_2 = nn.Linear(64, 32)
            self.batch_norm2 = nn.BatchNorm1d(32)
            self.layer_out = nn.Linear(32, 1)
            self.relu = nn.ReLU()
            self.dropout = nn.Dropout(p=0.3)

        def forward(self, x):
            x = self.layer_1(x)
            x = self.batch_norm1(x)
            x = self.relu(x)
            x = self.dropout(x)
            x = self.layer_2(x)
            x = self.batch_norm2(x)
            x = self.relu(x)
            x = self.dropout(x)
            x = self.layer_out(x)
            return x


    def main():
        print("--- Starting Model Performance Visualization ---")

        input_dim = 10  # Default for Biomed dataset
        X_tensor = None
        y_true = None
        y_probs = None

        # --- 2. Load Actual Data ---
        print("Loading data from project filesystem...")
        df_list = []
        for folder in ["biomed_data/", ""]:
            try:
                files = visualization.listdir(folder)
                csv_files = [f for f in files if f.endswith(".csv")]
                for csv_file in csv_files:
                    with visualization.open(
                        os.path.join(folder, csv_file), "r"
                    ) as f:
                        df_list.append(pd.read_csv(f))
                if df_list:
                    break
            except BaseException:
                pass

        if not df_list:
            raise RuntimeError(
                "No actual data found in project filesystem. Cannot proceed with visualization."
            )

        full_df = pd.concat(df_list, ignore_index=True)
        if "diagnosis" not in full_df.columns:
            raise KeyError("Target column 'diagnosis' not found in loaded data.")

        X = full_df.drop(
            columns=["patient_id", "diagnosis"], errors="ignore"
        ).values.astype("float32")
        y_true = full_df["diagnosis"].values.astype("float32").reshape(-1, 1)
        input_dim = X.shape[1]
        X_tensor = torch.tensor(StandardScaler().fit_transform(X))
        print(f"Successfully loaded and preprocessed {len(full_df)} rows.")

        # --- 3. Load Model ---
        model = BioMedNet(input_dim)
        print("Loading trained model weights...")
        # This will now raise an exception if loading fails
        visualization.load_weights(model)
        model.eval()
        print("Model weights loaded successfully.")

        # --- 4. Generate Probabilities ---
        print("Running inference on actual data...")
        with torch.no_grad():
            y_probs = torch.sigmoid(model(X_tensor)).numpy()

        # --- 5. Generate Plots ---
        sns.set_theme(style="whitegrid")

        # Confusion Matrix
        plt.figure(figsize=(8, 6))
        cm = confusion_matrix(y_true, (y_probs > 0.5).astype(int))
        sns.heatmap(cm, annot=True, fmt="d", cmap="Blues", cbar=False)
        plt.title("Confusion Matrix")
        plt.tight_layout()
        visualization.save_plot("Confusion Matrix")

        # ROC Curve
        plt.figure(figsize=(8, 6))
        fpr, tpr, _ = roc_curve(y_true, y_probs)
        plt.plot(
            fpr, tpr, color="darkorange", lw=2, label=f"AUC = {auc(fpr, tpr):.2f}"
        )
        plt.plot([0, 1], [0, 1], color="navy", lw=2, linestyle="--")
        plt.title("ROC Curve")
        plt.legend(loc="lower right")
        plt.tight_layout()
        visualization.save_plot("ROC Curve")

        # Precision-Recall
        plt.figure(figsize=(8, 6))
        precision, recall, _ = precision_recall_curve(y_true, y_probs)
        plt.plot(
            recall,
            precision,
            color="teal",
            lw=2,
            label=f"AP = {average_precision_score(y_true, y_probs):.2f}",
        )
        plt.title("Precision-Recall Curve")
        plt.legend(loc="upper right")
        plt.tight_layout()
        visualization.save_plot("Precision-Recall")

        # Confidence Scores
        plt.figure(figsize=(8, 6))
        plt.hist(
            y_probs[y_true == 0], bins=15, alpha=0.5, label="Healthy", color="blue"
        )
        plt.hist(
            y_probs[y_true == 1],
            bins=15,
            alpha=0.5,
            label="Diagnosed",
            color="red",
        )
        plt.title("Confidence Distribution")
        plt.legend()
        plt.tight_layout()
        visualization.save_plot("Confidence Scores")

        print("--- Visualization Script Completed ---")


    if __name__ == "__main__":
        main()
    ```

## 🧠 Training Scripts (NVFlare)

Custom training code is executed within the NVIDIA FLARE framework. This training contract is shared by SwarmMedHub and `swarmed`. We provide a `flare_adapter.py` module to simplify data access and model exchange.

### The `flare_adapter` Module

*   **`init_flare()`**: Initializes the FLARE client.
*   **`get_data_filesystem(project_id)`**: Returns a virtual filesystem (`fs`) for streaming data.
    *   **`fs.open(path)`**: Streams a file directly from S3.
    *   **`fs.ls(path)`**: Lists files in a directory. 
    *   **`fs.glob(pattern)`**: Navigate the dataset.
*   **`receive_model()`**: Receives the latest global model.
*   **`send_model(params, metrics)`**: Sends updated parameters and metrics back to the server.

!!! danger "Provide suitable requirements"
    Please ensure that your training script's `requirements.txt` includes all necessary libraries used in your training script to avoid runtime errors.

??? example "Example Training Script"
    ```python title="training.py" linenums="1"
    import flare_adapter
    import pandas as pd
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from dotenv import find_dotenv, load_dotenv
    from sklearn.preprocessing import StandardScaler
    from torch.utils.data import DataLoader, Dataset

    # Load environment variables from .env file
    load_dotenv(find_dotenv())

    SWARM_ROUNDS = 5

    # --- 1. Dataset Class (Streaming via Adapter) ---


    class BiomedTabularDataset(Dataset):
        def __init__(self, fs):
            """Initializes the dataset by streaming CSV files directly from the virtual filesystem."""
            file_list = fs.glob("*.csv")

            if not file_list:
                raise RuntimeError("No CSV files found in the project data.")

            print(f"Found {len(file_list)} CSV files. Streaming data...")
            
            # Stream files directly from fsspec into pandas
            df_list = []
            for f_path in file_list:
                print(f"BiomedTabularDataset: Loading {f_path}...")
                try:
                    with fs.open(f_path) as f:
                        df = pd.read_csv(f)
                        print(f"BiomedTabularDataset: Successfully loaded {f_path} ({len(df)} rows).")
                        df_list.append(df)
                except Exception as e:
                    print(f"BiomedTabularDataset: Error loading {f_path}: {e}")
                    raise
            
            self.full_df = pd.concat(df_list, ignore_index=True)
            print(f"BiomedTabularDataset: Total rows loaded: {len(self.full_df)}")

            self.X = self.full_df.drop(
                columns=["patient_id", "diagnosis"]
            ).values.astype("float32")
            self.y = (
                self.full_df["diagnosis"].values.astype("float32").reshape(-1, 1)
            )

            self.scaler = StandardScaler()
            self.X = self.scaler.fit_transform(self.X)

        def __len__(self):
            return len(self.full_df)

        def __getitem__(self, idx):
            return torch.tensor(self.X[idx]), torch.tensor(self.y[idx])


    # --- 2. Model Definition (Standard PyTorch) ---


    class BioMedNet(nn.Module):
        def __init__(self, input_dim):
            super().__init__()
            self.layer_1 = nn.Linear(input_dim, 64)
            self.batch_norm1 = nn.BatchNorm1d(64)
            self.layer_2 = nn.Linear(64, 32)
            self.batch_norm2 = nn.BatchNorm1d(32)
            self.layer_out = nn.Linear(32, 1)
            self.relu = nn.ReLU()
            self.dropout = nn.Dropout(p=0.3)

        def forward(self, x):
            x = self.layer_1(x)
            x = self.batch_norm1(x)
            x = self.relu(x)
            x = self.dropout(x)
            x = self.layer_2(x)
            x = self.batch_norm2(x)
            x = self.relu(x)
            x = self.dropout(x)
            x = self.layer_out(x)
            return x


    # --- 3. Main Training Function with Adapter API ---


    def main(project_id: str):
        # A. Initialize NVFlare
        flare_adapter.init_flare()

        # B. Use the adapter to get a virtual streaming filesystem
        with flare_adapter.get_data_filesystem(project_id) as fs:
            batch_size = 32
            lr = 0.001
            epochs_per_round = 5

            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

            # Load Data using the streaming filesystem
            try:
                dataset = BiomedTabularDataset(fs)
                train_loader = DataLoader(
                    dataset, batch_size=batch_size, shuffle=True
                )
                input_dim = dataset.X.shape[1]
            except Exception as e:
                import traceback
                print(f"Data loading error: {e}")
                traceback.print_exc()
                return

            # Initialize Model
            model = BioMedNet(input_dim).to(device)
            criterion = nn.BCEWithLogitsLoss()
            optimizer = optim.Adam(model.parameters(), lr=lr)

            # C. NVFlare Loop
            print("Starting NVFlare training loop...")
            while True:
                # 1. Receive the Global Model via Adapter
                input_model = flare_adapter.receive_model()

                if input_model is None:
                    print("Training finished or aborted.")
                    break

                # Load parameters into the local model
                if input_model.params:
                    state_dict = flare_adapter.get_pytorch_state_dict(
                        input_model.params
                    )
                    model.load_state_dict(state_dict)
                    print(f"Round {input_model.current_round}: Global model loaded.")
                else:
                    print(f"Round {input_model.current_round}: Starting from scratch.")

                # 2. Local Training Steps
                model.train()
                total_loss = 0.0
                steps = 0
                for _epoch in range(epochs_per_round):
                    for inputs, targets in train_loader:
                        inputs, targets = inputs.to(device), targets.to(device)
                        optimizer.zero_grad()
                        outputs = model(inputs)
                        loss = criterion(outputs, targets)
                        loss.backward()
                        optimizer.step()
                        total_loss += loss.item()
                        steps += 1
                
                avg_loss = total_loss / steps if steps > 0 else 0
                print(f"Round {input_model.current_round} complete. Avg Loss: {avg_loss:.4f}")

                # 3. Send Results Back to Server via Adapter
                flare_adapter.send_model(
                    params=model.state_dict(),
                    metrics={"loss": avg_loss},
                    meta={"NUM_STEPS_CURRENT_ROUND": steps}
                )


    if __name__ == "__main__":
        main(project_id="default_project")
    ```

## 📦 Sandbox Environment

The execution environment (sandbox) comes pre-installed with the following libraries:

| Category | Packages |
| :--- | :--- |
| **Data Science** | `numpy`, `pandas`, `scikit-learn`, `scipy` |
| **Deep Learning** | `torch`, `tensorflow`, `keras`, `pytorch-lightning` |
| **Vision & NLP** | `monai`, `transformers`, `datasets` |
| **Plotting** | `matplotlib`, `seaborn` |

### Customizing the Sandbox

**Administrators** can add packages by:

1.  Modifying `Dockerfile.sandbox`.
2.  Adding packages to the `sandbox` extra in `pyproject.toml`.
3.  Rebuilding via `make sandbox-build`.
