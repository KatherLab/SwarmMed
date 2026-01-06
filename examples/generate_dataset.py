import os

import numpy as np
import pandas as pd


def create_dummy_biomed_data(
    num_samples=1000, num_files=5, output_dir="biomed_data"
):
    """
    Creates a dummy biomedical dataset split across multiple files.

    Features:
    - age (numerical)
    - sex (categorical: 0 or 1)
    - bmi (numerical)
    - biomarker_A (numerical, correlates with target)
    - biomarker_B (numerical, noise)
    - diagnosis (binary target)
    """

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    samples_per_file = num_samples // num_files

    print(
        f"Generating {num_samples} samples across {num_files} files in '{output_dir}/'..."
    )

    for i in range(num_files):
        # Generate random data
        data = {
            "patient_id": range(
                i * samples_per_file, (i + 1) * samples_per_file
            ),
            "age": np.random.randint(18, 90, samples_per_file),
            "sex": np.random.randint(0, 2, samples_per_file),
            "bmi": np.random.normal(25, 5, samples_per_file).round(2),
            # skewed feature
            "biomarker_A": np.random.exponential(
                scale=2.0, size=samples_per_file
            ),
            # noise feature
            "biomarker_B": np.random.normal(0, 1, samples_per_file),
        }

        df = pd.DataFrame(data)

        # Create a synthetic target: Risk increases with Age, BMI, and Biomarker A
        # Logistic function to create probability
        logits = (
            (df["age"] - 50) / 20
            + (df["bmi"] - 25) / 10
            + (df["biomarker_A"] - 2) / 1
        )
        probs = 1 / (1 + np.exp(-logits))
        df["diagnosis"] = (np.random.rand(samples_per_file) < probs).astype(
            int
        )

        # Save to CSV
        file_path = os.path.join(output_dir, f"batch_{i + 1}.csv")
        df.to_csv(file_path, index=False)
        print(f"Saved {file_path}")


# Run the generation
create_dummy_biomed_data()
