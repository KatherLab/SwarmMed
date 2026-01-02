import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# --- Linter Fallback ---
# 'visualization' is injected by the SwarmCloud sandbox.
# We define a dummy here to avoid F821 linting errors.
if 'visualization' not in globals():
    class DummyVisualization:
        def exists(self, *args, **kwargs): return False
        def open(self, *args, **kwargs): pass
        def save_plot(self, *args, **kwargs): pass
    visualization = DummyVisualization()

# Define the directory where data generation script saved files
data_folder = 'biomed_data'

try:
    # --- 1. Load and Aggregate Data ---
    df_list = []

    # Attempt to load batches 1 through 10
    for i in range(1, 11):
        file_path = f"{data_folder}/batch_{i}.csv"

        # specific check using the provided interface
        if visualization.exists(file_path):
            with visualization.open(file_path, 'r') as f:
                batch_df = pd.read_csv(f)
                df_list.append(batch_df)

    if not df_list:
        print(
            f"No data found in {data_folder}. Please run the generation script first.")
    else:
        # Combine all batches
        df = pd.concat(df_list, ignore_index=True)
        print(f"Successfully loaded {len(df)} records for visualization.")

        # --- Set Seaborn Theme for Prettier Plots ---
        sns.set_theme(style="whitegrid", context="talk")

        # --- Plot 1: Age Distribution (Histogram with KDE) ---
        plt.figure(figsize=(10, 6))
        sns.histplot(
            data=df,
            x='age',
            kde=True,
            color='teal',
            bins=20,
            alpha=0.6)

        plt.title('Patient Age Distribution', fontsize=16, pad=20)
        plt.xlabel('Age (Years)')
        plt.ylabel('Count')

        visualization.save_plot("Age Distribution")

        # --- Plot 2: Diagnosis Balance (Count Plot) ---
        # Replacing Pie chart with a cleaner Bar plot which is standard in
        # scientific papers
        plt.figure(figsize=(8, 6))
        ax = sns.countplot(data=df, x='diagnosis', palette='viridis')

        plt.title(
            'Target Class Balance (0=Healthy, 1=Diagnosed)',
            fontsize=16,
            pad=20)
        plt.xlabel('Diagnosis Group')
        plt.ylabel('Patient Count')
        plt.bar_label(ax.containers[0])  # Add numbers on top of bars

        visualization.save_plot("Diagnosis Balance")

        # --- Plot 3: Biomarker A vs Diagnosis (Violin Plot) ---
        # Violin plots are often "prettier" and more informative than boxplots
        # for bio-data
        plt.figure(figsize=(10, 6))
        sns.violinplot(
            data=df,
            x='diagnosis',
            y='biomarker_A',
            palette="Set2",
            split=True)

        plt.title('Biomarker A Distribution by Diagnosis', fontsize=16, pad=20)
        plt.xlabel('Diagnosis (0=Healthy, 1=Diagnosed)')
        plt.ylabel('Biomarker Level')

        visualization.save_plot("Biomarker A Separation")

        # --- Plot 4: Age vs BMI Scatter (Risk Analysis) ---
        plt.figure(figsize=(10, 6))
        sns.scatterplot(
            data=df,
            x='age',
            y='bmi',
            hue='diagnosis',
            style='diagnosis',
            palette='deep',
            alpha=0.7,
            s=80  # larger dot size
        )

        plt.title('Age vs BMI: Risk Factor Clusters', fontsize=16, pad=20)
        plt.xlabel('Age')
        plt.ylabel('BMI')
        plt.legend(title='Diagnosis', loc='upper right')

        visualization.save_plot("Age vs BMI Scatter")

        # --- Plot 5: Correlation Matrix (Heatmap) ---
        plt.figure(figsize=(10, 8))
        corr = df.drop(columns=['patient_id']).corr()

        # Create a heatmap with diverging colors
        sns.heatmap(
            corr,
            annot=True,
            fmt=".2f",
            cmap='RdBu_r',
            center=0,
            square=True,
            linewidths=.5,
            cbar_kws={"shrink": .8}
        )

        plt.title('Feature Correlation Matrix', fontsize=16, pad=20)
        plt.tight_layout()

        visualization.save_plot("Correlation Matrix")

except Exception as e:
    print(f"Error creating visualizations: {e}")
