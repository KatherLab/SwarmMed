# Duke Breast MRI Worked Example: Data Splits & Metadata

This directory provides the exact patient-level stratified split definitions and clinical annotations used in the SwarmMed protocol paper (Nature Protocols, NP-P260024A).

## Partition Overview

The Duke Breast Cancer MRI dataset (available publicly via [The Cancer Imaging Archive (TCIA)](https://doi.org/10.7937/TCIA.65004D0D)) comprises 922 patients. After unilateral examination processing (one record per breast exam) and exclusion criteria, 651 unique patients (1,302 exams) form the study cohort:

* **Held-out Test Cohort (`test/`)**: 131 patients (262 exams, 137 malignant / 125 benign). Kept strictly isolated from all model training and validation across all experimental arms.
* **Consortium Training & Validation Cohort (`node_all/`)**: 520 patients (1,040 exams: 832 training / 208 validation, with stratified 51.9% malignant balance).
  * **Site A (`node_A/`)**: 260 patients (50.0% of consortium data; 416 training exams, 104 validation exams).
  * **Site B (`node_B/`)**: 195 patients (37.5% of consortium data; 312 training exams, 78 validation exams).
  * **Site C (`node_C/`)**: 65 patients (12.5% of consortium data; 104 training exams, 26 validation exams).

## Patient Stratification & Leakage Prevention

* **Strict Patient-Level Isolation**: Splitting was performed at the subject level (`PatientID`). No patient appears in more than one node or split.
* **Label Balance**: The malignant fraction is preserved identically across every node's training and validation partition (51.9%–53.8%).

## File Formats

Each folder contains:
1. `split.csv`: Maps each unilateral examination identifier (`UID`) to `Fold` (0) and `Split` (`train`, `val`, or `test`).
2. `annotation.csv`: Maps each `UID` to `PatientID`, `Age` (in days), and ground-truth `Lesion` class (`0` = Normal / non-malignant, `2` = Malignant).
