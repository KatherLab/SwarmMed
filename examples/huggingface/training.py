import flare_adapter
import numpy as np
import pandas as pd
from datasets import Dataset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

SWARM_ROUNDS = 10

# --- 1. Data Loading ---

def get_data(fs):
    # For this example, we assume CSVs have 'text' and 'label' columns
    file_list = fs.glob("*.csv")
    if not file_list:
        # Create dummy data if none found for demonstration
        df = pd.DataFrame({
            "text": ["patient shows symptoms of X", "no signs of illness", "test results positive"] * 10,
            "label": [1, 0, 1] * 10
        })
    else:
        print(f"Found {len(file_list)} CSV files. Streaming data...")
        df_list = []
        for f_path in file_list:
            with fs.open(f_path) as f:
                df_list.append(pd.read_csv(f))
        df = pd.concat(df_list, ignore_index=True)
        # Ensure we have text and label columns
        if "diagnosis" in df.columns:
            df = df.rename(columns={"diagnosis": "label"})
        if "text" not in df.columns:
            # Dummy text for tabular data demonstration
            df["text"] = "Patient data record"

    return Dataset.from_pandas(df)

# --- 2. Main Training Function ---

def main(project_id: str):
    flare_adapter.init_flare()

    model_name = "bert-base-uncased"
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    
    def tokenize_function(examples):
        return tokenizer(examples["text"], padding="max_length", truncation=True)

    with flare_adapter.get_data_filesystem(project_id) as fs:
        # Load data using streaming filesystem
        raw_dataset = get_data(fs)
        tokenized_dataset = raw_dataset.map(tokenize_function, batched=True)

        model = AutoModelForSequenceClassification.from_pretrained(model_name, num_labels=2)
        
        # Swarm Loop
        while True:
            input_model = flare_adapter.receive_model()
            if input_model is None:
                break

            if input_model.params:
                state_dict = flare_adapter.get_pytorch_state_dict(input_model.params)
                model.load_state_dict(state_dict)

            # Training Arguments
            training_args = TrainingArguments(
                output_dir="./results",
                num_train_epochs=1,
                per_device_train_batch_size=8,
                logging_steps=10,
                save_strategy="no",
                report_to="none"
            )

            trainer = Trainer(
                model=model,
                args=training_args,
                train_dataset=tokenized_dataset,
            )

            train_result = trainer.train()

            # Simulated validation metric improvement
            current_round = input_model.current_round
            simulated_accuracy = 0.6 + (0.35 * (1.0 - np.exp(-current_round/5.0))) + (np.random.rand() * 0.02)

            # Send back to server
            flare_adapter.send_model(
                params=model.state_dict(),
                metrics={
                    "loss": train_result.training_loss,
                    "accuracy": simulated_accuracy
                },
                meta={
                    "NUM_STEPS_CURRENT_ROUND": trainer.state.global_step
                }
            )

if __name__ == "__main__":
    main(project_id="default_project")
