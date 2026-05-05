import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

# Target folder names
METADATA_DIR_NAME = "metadata_unilateral"

metadata_folder = None
annotation_path = None

try:
    # 1. DYNAMIC FOLDER DISCOVERY
    # Use manifest keys to find metadata_unilateral/annotation.csv
    all_files = list(visualization.manifest.keys())
    
    possible_metadata_folders = set()
    for file_path in all_files:
        parts = file_path.split('/')
        for i, part in enumerate(parts):
            if part == METADATA_DIR_NAME:
                possible_metadata_folders.add('/'.join(parts[:i+1]))
    
    if possible_metadata_folders:
        metadata_folder = sorted(list(possible_metadata_folders))[0]
        annotation_path = f"{metadata_folder}/annotation.csv"

    if not annotation_path or not visualization.exists(annotation_path):
        print(f"Error: Could not locate {METADATA_DIR_NAME}/annotation.csv")
    else:
        # 2. LOAD DATA
        with visualization.open(annotation_path, "r") as f:
            df = pd.read_csv(f)
        
        print(f"Successfully loaded {len(df)} records for visualization.")

        # Set Seaborn Theme
        sns.set_theme(style="whitegrid", context="talk")

        # --- Plot 1: Lesion Distribution (Bar Plot) ---
        plt.figure(figsize=(10, 6))
        
        # Mapping lesion codes to labels
        lesion_map = {
            0: "No Lesion",
            1: "Benign Lesion",
            2: "Malignant Lesion"
        }
        
        # Ensure all categories are represented even if count is 0
        df_plot = df.copy()
        df_plot['Lesion_Label'] = df_plot['Lesion'].map(lesion_map)
        
        order = ["No Lesion", "Benign Lesion", "Malignant Lesion"]
        ax = sns.countplot(data=df_plot, x="Lesion_Label", order=order, palette="viridis")

        plt.title("Distribution of Lesion Types", fontsize=16, pad=20)
        plt.xlabel("Lesion Category")
        plt.ylabel("Patient Count")
        
        # Add bar labels
        if len(ax.containers) > 0:
            plt.bar_label(ax.containers[0])

        visualization.save_plot("Lesion Distribution")

        # --- Plot 2: Age Distribution in Years (Histogram) ---
        plt.figure(figsize=(10, 6))
        
        # Convert Age from days to years
        df_plot['Age_Years'] = df_plot['Age'] / 365.25
        
        sns.histplot(
            data=df_plot, x="Age_Years", kde=True, color="teal", bins=15, alpha=0.6
        )

        plt.title("Patient Age Distribution", fontsize=16, pad=20)
        plt.xlabel("Age (Years)")
        plt.ylabel("Count")

        visualization.save_plot("Age Distribution")

except Exception as e:
    print(f"Error creating breast MRI visualizations: {e}")
