import pandas as pd

# Initialize validation results
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

# 1. FOLDER STRUCTURE VALIDATION
validation.add_check("Validation Started", "ok", "Beginning comprehensive data validation")

required_folders = ['patients', 'studies']
optional_folders = ['reports', 'backup']

try:
    root_items = validation.listdir()
    folders = [item for item in root_items if item.endswith('/')]
    folder_names = [f.rstrip('/') for f in folders]
    
    for req_folder in required_folders:
        if req_folder in folder_names:
            add_check_with_count(f"Required Folder: {req_folder}", "ok", f"Found required folder '{req_folder}'")
        else:
            add_check_with_count(f"Required Folder: {req_folder}", "error", f"Missing required folder '{req_folder}'")
    
    for opt_folder in optional_folders:
        if opt_folder in folder_names:
            add_check_with_count(f"Optional Folder: {opt_folder}", "ok", f"Found optional folder '{opt_folder}'")
    
    
except Exception as e:
    add_check_with_count("Folder Structure", "error", f"Could not read root directory: {str(e)}")

# 2. PATIENTS DATA VALIDATION
try:
    patients_files = validation.listdir('patients')
    required_patient_files = ['demographics.csv', 'medical_history.csv', 'contact_info.json']
    
    for req_file in required_patient_files:
        if req_file in patients_files:
            add_check_with_count(f"Patient File: {req_file}", "ok", f"Found required file '{req_file}'")
        else:
            add_check_with_count(f"Patient File: {req_file}", "error", f"Missing required file '{req_file}'")
    
    # Validate demographics CSV
    if 'demographics.csv' in patients_files:
        try:
            with validation.open('patients/demographics.csv', 'r') as f:
                df_demographics = pd.read_csv(f)
            
            # Check required columns
            required_cols = ['patient_id', 'age', 'gender', 'enrollment_date', 'study_group']
            missing_cols = [col for col in required_cols if col not in df_demographics.columns]
            
            if missing_cols:
                add_check_with_count("Demographics Columns", "error", f"Missing columns: {missing_cols}")
            else:
                add_check_with_count("Demographics Columns", "ok", "All required columns present")
            
            # Check data quality
            patient_count = len(df_demographics)
            add_check_with_count("Patient Count", "ok", f"Found {patient_count} patients")
            
            # Age validation
            if 'age' in df_demographics.columns:
                invalid_ages = df_demographics[(df_demographics['age'] < 18) | (df_demographics['age'] > 120)]
                if len(invalid_ages) > 0:
                    add_check_with_count("Age Validation", "warning", f"{len(invalid_ages)} patients with invalid ages")
                else:
                    add_check_with_count("Age Validation", "ok", "All ages are valid (18-120)")
            
            # Check for duplicates
            duplicates = df_demographics['patient_id'].duplicated().sum()
            if duplicates > 0:
                add_check_with_count("Patient ID Duplicates", "error", f"Found {duplicates} duplicate patient IDs")
            else:
                add_check_with_count("Patient ID Duplicates", "ok", "No duplicate patient IDs found")
                
        except Exception as e:
            add_check_with_count("Demographics Validation", "error", f"Could not validate demographics.csv: {str(e)}")

except Exception as e:
    add_check_with_count("Patients Directory", "error", f"Could not access patients directory: {str(e)}")

# Summary
validation.add_check("Validation Summary", "ok", 
                   f"Completed validation: {validation_results['critical_errors']} errors, " +
                   f"{validation_results['warnings']} warnings, " +
                   f"{validation_results['info_checks']} info checks")
