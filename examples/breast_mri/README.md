# Breast MRI Example

This directory contains examples for working with breast MRI data, specifically focused on unilateral datasets.

## Data Structure

The validation script expects the following structure:

```
.
├── metadat_unileteral/
│   └── annotation.csv
└── data_unileteral/
    ├── UID_1/
    │   └── image.nii.gz
    ├── UID_2/
    │   └── image.nii.gz
    └── ...
```

### `annotation.csv`

The annotation file must contain a `UID` column which matches the folder names in `data_unileteral`.

Example:
```csv
UID,PatientID,Age,Lesion,__index_level_0__
ODELIA_BRAID1_0158_1_left,ODELIA_BRAID1_0158_1,24410,2,0
ODELIA_BRAID1_0177_1_left,ODELIA_BRAID1_0177_1,19389,0,5
```

## Validation

The `val.py` script performs the following checks:
1. Verifies that `metadat_unileteral/annotation.csv` exists and can be read.
2. Checks for the existence of the `UID` column in the CSV.
3. For each `UID` in the CSV, it ensures a corresponding folder exists in `data_unileteral/`.
4. Checks that each UID folder contains at least one `.nii.gz` file.
