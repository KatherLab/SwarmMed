import os
import glob
import pandas as pd
import xgboost as xgb
import flare_adapter
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score
import json

# --- 1. Data Loading ---

def load_tabular_data(data_dir):
    file_pattern = os.path.join(data_dir, "**", "*.csv")
    file_list = glob.glob(file_pattern, recursive=True)
    if not file_list:
        # Create dummy data if none found
        df = pd.DataFrame({
            "feature1": [1, 2, 3, 4, 5, 6],
            "feature2": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
            "diagnosis": [0, 1, 0, 1, 0, 1]
        })
    else:
        df_list = [pd.read_csv(f) for f in file_list]
        df = pd.concat(df_list, ignore_index=True)
    
    X = df.drop(columns=["patient_id", "diagnosis"], errors="ignore")
    y = df["diagnosis"]
    return X, y

# --- 2. Main Training Function ---

def main(project_id: str):
    flare_adapter.init_flare()

    with flare_adapter.get_data_filesystem(project_id) as fs:
        data_dir = fs.get_data_path()
        X, y = load_tabular_data(data_dir)
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)

        # XGBoost parameters
        param = {
            'max_depth': 3,
            'eta': 0.1,
            'objective': 'binary:logistic',
            'eval_metric': 'logloss'
        }

        # Swarm Loop
        while True:
            input_model = flare_adapter.receive_model()
            if input_model is None:
                break

            # In XGBoost, "merging" global models is more complex (histogram vs booster averaging)
            # For this simple adapter example, we'll demonstrate a round of local training
            # and sending the booster's binary representation or JSON.
            
            dtrain = xgb.DMatrix(X_train, label=y_train)
            dtest = xgb.DMatrix(X_test, label=y_test)
            
            # Use input_model.params to initialize if available (e.g. via model serialisation)
            # For simplicity, we train locally and send the update
            num_round = 10
            bst = xgb.train(param, dtrain, num_round)
            
            preds = bst.predict(dtest)
            predictions = [round(value) for value in preds]
            accuracy = accuracy_score(y_test, predictions)

            # Send model booster as binary/json
            # Send results
            # XGBBaggingAggregator expects "model_data" key for the booster.
            bst_bytes = bst.save_raw()

            # Send results
            flare_adapter.send_model(
                params={"model_data": bst_bytes},
                metrics={"accuracy": float(accuracy)}
            )
if __name__ == "__main__":
    main(project_id="default_project")
