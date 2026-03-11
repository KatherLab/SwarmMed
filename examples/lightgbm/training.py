import os
import glob
import pandas as pd
import lightgbm as lgb
import flare_adapter
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

# --- 1. Data Loading ---

def load_tabular_data(data_dir):
    file_pattern = os.path.join(data_dir, "**", "*.csv")
    file_list = glob.glob(file_pattern, recursive=True)
    if not file_list:
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

        # LightGBM parameters
        params = {
            'objective': 'binary',
            'metric': 'binary_logloss',
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.9
        }

        # Swarm Loop
        while True:
            input_model = flare_adapter.receive_model()
            if input_model is None:
                break

            train_data = lgb.Dataset(X_train, label=y_train)
            test_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

            # Training
            gbm = lgb.train(params, train_data, num_boost_round=20, valid_sets=[test_data])

            # Evaluate
            y_pred = gbm.predict(X_test, num_iteration=gbm.best_iteration)
            accuracy = accuracy_score(y_test, [round(x) for x in y_pred])

            # Send back to server (Booster string/json representation)
            model_str = gbm.model_to_string()
            
            flare_adapter.send_model(
                params={"model_str": model_str},
                metrics={"accuracy": float(accuracy)}
            )

if __name__ == "__main__":
    main(project_id="default_project")
