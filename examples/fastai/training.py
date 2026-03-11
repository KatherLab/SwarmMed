import os
import glob
import pandas as pd
import torch
from fastai.tabular.all import *
import flare_adapter

# --- 1. Data Loading ---

def get_data(data_dir):
    file_pattern = os.path.join(data_dir, "**", "*.csv")
    file_list = glob.glob(file_pattern, recursive=True)
    if not file_list:
        raise RuntimeError(f"No CSV files found in '{data_dir}'.")

    df_list = [pd.read_csv(f) for f in file_list]
    df = pd.concat(df_list, ignore_index=True)
    
    # Simple preprocessing
    cont_names = [col for col in df.columns if col not in ["patient_id", "diagnosis"]]
    dls = TabularDataLoaders.from_df(df, y_names="diagnosis",
                                    cont_names=cont_names,
                                    procs=[Normalize],
                                    bs=32)
    return dls

# --- 2. Main Training Function ---

def main(project_id: str):
    flare_adapter.init_flare()

    with flare_adapter.get_data_filesystem(project_id) as fs:
        data_dir = fs.get_data_path()
        dls = get_data(data_dir)

        # Create fastai Learner
        learn = tabular_learner(dls, layers=[64, 32], metrics=accuracy)
        
        # Swarm Loop
        while True:
            input_model = flare_adapter.receive_model()
            if input_model is None:
                break

            if input_model.params:
                state_dict = flare_adapter.get_pytorch_state_dict(input_model.params)
                learn.model.load_state_dict(state_dict)

            # Train for 1 cycle per round
            learn.fit_one_cycle(1, lr_max=1e-3)

            # Send back to server
            flare_adapter.send_model(
                params=learn.model.state_dict(),
                metrics={"accuracy": float(learn.recorder.metrics[0].value)}
            )

if __name__ == "__main__":
    main(project_id="default_project")
