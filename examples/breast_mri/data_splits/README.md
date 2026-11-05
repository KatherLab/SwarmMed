# Duke breast MRI worked example: split and annotation files

This folder holds the patient-level split of the Duke breast MRI cohort used in
the worked example of the SwarmMed protocol ([Reproducing the worked
example](../../../docs/reproduce.md)). It contains no images.

## Source data and licence

The files are derived from the Duke-Breast-Cancer-MRI collection of The Cancer
Imaging Archive (TCIA), which is distributed under the
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) licence. These
derived files are subject to the same licence, not to the Apache License 2.0 of
the SwarmMed code. The images must be obtained from TCIA under its terms of use.
If you use these files, cite the dataset and its publication:

* Saha, A., Harowicz, M. R., Grimm, L. J., Weng, J., Cain, E. H., Kim, C. E.,
  Ghate, S. V., Walsh, R., & Mazurowski, M. A. (2021). Dynamic contrast-enhanced
  magnetic resonance images of breast cancer patients with tumor locations
  [Data set]. The Cancer Imaging Archive.
  <https://doi.org/10.7937/TCIA.e3sv-re93>
* Saha, A., Harowicz, M. R., Grimm, L. J., Kim, C. E., Ghate, S. V., Walsh, R.
  and Mazurowski, M. A. (2018). A machine learning approach to radiogenomics of
  breast cancer: a study of 922 subjects and 529 DCE-MRI features. British
  Journal of Cancer, 119(4), 508–516. <https://doi.org/10.1038/s41416-018-0185-8>

## Partition

The collection comprises 922 patients. The worked example uses 651 of them, with
one record per breast (1,302 breasts); the script that selected them and made
the partition is not part of this repository.

| Folder | Patients | Breasts | Training | Validation | Test | Malignant |
|---|---|---|---|---|---|---|
| `node_A/` | 260 | 520 | 416 | 104 | | 271 |
| `node_B/` | 195 | 390 | 312 | 78 | | 203 |
| `node_C/` | 65 | 130 | 104 | 26 | | 68 |
| `node_all/` (the three nodes pooled) | 520 | 1,040 | 832 | 208 | | 542 |
| `test/` (internal test set) | 131 | 262 | | | 262 | 137 |

* The split is at patient level: both breasts of a patient are in the same
  folder and split, and no patient is in more than one folder.
* The malignant fraction is between 51.9% and 53.8% in every training,
  validation and test split. All other breasts are labelled 0 (no lesion);
  these files contain no benign class (1).
* `node_all/` is the union of `node_A/`, `node_B/` and `node_C/`, used to
  train the centralised baseline.

## Files

Each folder contains:

1. `annotation.csv`: `UID` (one breast, for example `Breast_MRI_001_left`),
   `PatientID`, `Age` (in days) and `Lesion` (0, no lesion; 2, malignant).
2. `split.csv`: `UID`, `Fold` (always 0) and `Split` (`train`, `val` or `test`).
