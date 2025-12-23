import pandas as pd

# Initialize counters for the summary
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

validation.add_check("Validation Started", "ok", "Beginning biomedical data validation")

# 1. FOLDER AND FILE STRUCTURE
data_folder = 'biomed_data'

try:
    # List files in the root or data directory
    root_items = validation.listdir(data_folder)
    
    # We expect multiple batch files (e.g., batch_1.csv, batch_2.csv)
    batch_files = [f for f in root_items if f.startswith('batch_') and f.endswith('.csv')]
    
    if len(batch_files) > 0:
        add_check_with_count("Batch Files", "ok", f"Found {len(batch_files)} data batch files")
    else:
        add_check_with_count("Batch Files", "error", f"No batch_*.csv files found in {data_folder}")

    # 2. DATA CONTENT VALIDATION
    required_cols = ['patient_id', 'age', 'sex', 'bmi', 'biomarker_A', 'biomarker_B', 'diagnosis']
    total_patients = 0
    
    for file_name in batch_files:
        full_path = f"{data_folder}/{file_name}"
        try:
            with validation.open(full_path, 'r') as f:
                df = pd.read_csv(f)
            
            # Update counts
            total_patients += len(df)
            
            # Check 1: Schema
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                add_check_with_count(f"Schema: {file_name}", "error", f"Missing columns: {missing}")
            else:
                add_check_with_count(f"Schema: {file_name}", "ok", "All columns present")
                
            # Check 2: Age Validity
            if 'age' in df.columns:
                invalid_ages = df[(df['age'] < 0) | (df['age'] > 120)]
                if len(invalid_ages) > 0:
                    add_check_with_count(f"Age Quality: {file_name}", "warning", f"{len(invalid_ages)} rows with invalid age")
                else:
                    add_check_with_count(f"Age Quality: {file_name}", "ok", "Ages are valid")

            # Check 3: Null Values
            if df.isnull().values.any():
                add_check_with_count(f"Completeness: {file_name}", "warning", "File contains missing (NaN) values")
            else:
                add_check_with_count(f"Completeness: {file_name}", "ok", "No missing values found")

        except Exception as e:
            add_check_with_count(f"Read Error: {file_name}", "error", f"Could not parse CSV: {str(e)}")
            
    add_check_with_count("Total Volume", "ok", f"Total dataset size: {total_patients} patients")

except Exception as e:
    add_check_with_count("Directory Access", "error", f"Could not access '{data_folder}': {str(e)}")

# Summary
validation.add_check("Validation Summary", "ok", 
                   f"Completed: {validation_results['critical_errors']} errors, " +
                   f"{validation_results['warnings']} warnings.")