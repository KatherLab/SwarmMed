import pandas as pd

# Initialize counters for the summary
validation_results = {"critical_errors": 0, "warnings": 0, "info_checks": 0}


def add_check_with_count(name, status, message, details=None):
    """Add a check and update counters."""
    validation.add_check(name, status, message, details or {})
    if status == "error":
        validation_results["critical_errors"] += 1
    elif status == "warning":
        validation_results["warnings"] += 1
    else:
        validation_results["info_checks"] += 1


validation.add_check(
    "Validation Started", "ok", "Beginning breast MRI data validation"
)

# Target folder names
METADATA_DIR_NAME = "metadata_unilateral"
DATA_DIR_NAME = "data_unilateral"

# Required files in each patient folder
REQUIRED_FILES = ["Pre.nii.gz", "Post_1.nii.gz", "Sub_1.nii.gz", "T2.nii.gz"]

metadata_folder = None
data_folder = None

try:
    # 1. DYNAMIC FOLDER DISCOVERY
    # We look for metadata_unilateral and data_unilateral. 
    # They might be in the root or inside a parent folder (e.g., 'CAM')
    
    # Debug: Print ALL keys in the manifest to see what we actually have
    all_files = list(validation.manifest.keys())
    
    # Try to find the folders from the flat list of files
    possible_metadata_folders = set()
    possible_data_folders = set()
    
    for file_path in all_files:
        parts = file_path.split('/')
        for i, part in enumerate(parts):
            if part == METADATA_DIR_NAME:
                possible_metadata_folders.add('/'.join(parts[:i+1]))
            elif part == DATA_DIR_NAME:
                possible_data_folders.add('/'.join(parts[:i+1]))
    
    # Pick the first match for each
    if possible_metadata_folders:
        metadata_folder = sorted(list(possible_metadata_folders))[0]
    if possible_data_folders:
        data_folder = sorted(list(possible_data_folders))[0]

    if not metadata_folder or not data_folder:
        add_check_with_count(
            "Folder Discovery", 
            "error", 
            f"Could not locate required folders. Found metadata: {metadata_folder}, data: {data_folder}. "
            f"Expected '{METADATA_DIR_NAME}' and '{DATA_DIR_NAME}' folders."
        )
    else:
        add_check_with_count(
            "Folder Discovery", "ok", f"Found structure at: {metadata_folder} and {data_folder}"
        )

        # 2. CHECK METADATA FILES
        annotation_path = f"{metadata_folder}/annotation.csv"
        
        # Check split.csv existence
        split_found = any(f.startswith(metadata_folder + "/split.csv") or f == metadata_folder + "/split.csv" for f in all_files)
        if split_found:
            add_check_with_count("Metadata: split.csv", "ok", "Found split.csv")
        else:
            add_check_with_count("Metadata: split.csv", "warning", "split.csv not found in metadata folder")

        try:
            with validation.open(annotation_path, "r") as f:
                df = pd.read_csv(f)
            
            add_check_with_count(
                "Annotation File", "ok", f"Successfully loaded {annotation_path}"
            )
            
            # Check if UID column exists
            if "UID" not in df.columns:
                add_check_with_count(
                    "Schema: UID", "error", "Missing 'UID' column in annotation.csv"
                )
            else:
                uids = df["UID"].tolist()
                add_check_with_count(
                    "Annotation Content", "ok", f"Found {len(uids)} patient UIDs in annotation.csv"
                )
                
                # 3. CHECK DATA FOLDERS
                missing_folders = []
                incomplete_folders = {} # maps uid -> list of missing files
                
                # Get unique folder prefixes from manifest
                all_folder_prefixes = set()
                # Also index all files for fast lookup
                files_set = set(all_files)
                for f in all_files:
                    if '/' in f:
                        all_folder_prefixes.add(f.rsplit('/', 1)[0])

                for uid in uids:
                    uid_str = str(uid)
                    uid_folder_path = f"{data_folder}/{uid_str}"
                    
                    if uid_folder_path not in all_folder_prefixes:
                        missing_folders.append(uid_str)
                        continue
                    
                    # Check for specific required files
                    missing_required = []
                    for req_file in REQUIRED_FILES:
                        full_req_path = f"{uid_folder_path}/{req_file}"
                        if full_req_path not in files_set:
                            missing_required.append(req_file)
                    
                    if missing_required:
                        incomplete_folders[uid_str] = missing_required
                
                if missing_folders:
                    missing_str = ", ".join(missing_folders[:15])
                    if len(missing_folders) > 15:
                        missing_str += " ..."
                    add_check_with_count(
                        "Data Folders", "error", f"Missing {len(missing_folders)} folders for UIDs: {missing_str}",
                        {"missing_uids": missing_folders}
                    )
                else:
                    add_check_with_count(
                        "Data Folders", "ok", "All UIDs from annotation.csv have corresponding data folders"
                    )
                
                if incomplete_folders:
                    count = len(incomplete_folders)
                    # Get details for a few folders for the message
                    sample_uids = list(incomplete_folders.keys())[:5]
                    sample_details = []
                    for s_uid in sample_uids:
                        missing = incomplete_folders[s_uid]
                        sample_details.append(f"{s_uid} (missing: {', '.join(missing)})")
                    
                    msg = f"{count} patient folders are missing required files."
                    if count <= 5:
                        msg = f"Incomplete folders: {'; '.join(sample_details)}"
                    else:
                        msg += f" Examples: {'; '.join(sample_details)} ..."
                        
                    add_check_with_count(
                        "Required Files", "error", msg,
                        {"incomplete_folders": incomplete_folders}
                    )
                else:
                    add_check_with_count(
                        "Required Files", "ok", f"All folders contain the required files: {', '.join(REQUIRED_FILES)}"
                    )
                    
        except Exception as e:
            add_check_with_count(
                "Annotation Access", "error", f"Could not read '{annotation_path}': {str(e)}"
            )

except Exception as e:
    add_check_with_count(
        "General Error", "error", f"An unexpected error occurred: {str(e)}"
    )

# Summary
validation.add_check(
    "Validation Summary",
    "ok",
    f"Completed: {validation_results['critical_errors']} errors, "
    + f"{validation_results['warnings']} warnings.",
)
