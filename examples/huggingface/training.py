import os
import glob
import pandas as pd
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer, Trainer, TrainingArguments
from datasets import Dataset
import flare_adapter

# --- 1. Data Loading ---

def get_data(data_dir):
    # For this example, we assume CSVs have 'text' and 'label' columns
    file_pattern = os.path.join(data_dir, "**", "*.csv")
    file_list = glob.glob(file_pattern, recursive=True)
    if not file_list:
        # Create dummy data if none found for demonstration
        df = pd.DataFrame({
            "text": ["patient shows symptoms of X", "no signs of illness", "test results positive"],
            "label": [1, 0, 1]
        })
    else:
        df_list = [pd.read_csv(f) for f in file_list]
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
        data_dir = fs.get_data_path()
        raw_dataset = get_data(data_dir)
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

            trainer.train()

            # Send back to server
            flare_adapter.send_model(
                params=model.state_dict(),
                metrics={"loss": trainer.state.log_history[-1].get("train_loss", 0) if trainer.state.log_history else 0}
            )

if __name__ == "__main__":
    main(project_id="default_project")
