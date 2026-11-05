# Breast MRI example

Site scripts for a breast MRI cohort stored in the unilateral layout used by
ODELIA and by the Duke worked example (one folder per breast). To use them in a
project, put them in its code bundle as `validation.py` (`val.py`),
`visualization.py` (`viz.py`) and `results_visualization.py` (`res_viz.py`).
SwarmMedHub runs the first two in its sandbox against a site's registered data
and keeps only their check results and plots.

| File | What it does |
|---|---|
| `val.py` | Checks a site's data before it joins a study (see below) |
| `viz.py` | Plots class balance by split, exams per patient, age by class and split composition |
| `training.py` | Example training script: the MST classifier, reading the site's data through SwarmMed's data adapter |
| `res_viz.py` | Example results visualization for a 3D ResNet-101 checkpoint; it does not load the MST model of `training.py` |
| `data_splits/` | The split and annotation files of the Duke worked example ([README](data_splits/README.md)) |

The worked example of the protocol was trained with
`experiments/duke_mst_cli/training.py`; see
[Reproducing the worked example](../../docs/reproduce.md).

## Data layout

The scripts look for the two folders anywhere in the registered data, so a site
may nest them in a folder named after the centre (for example
`CAM/metadata_unilateral/`).

```
.
├── metadata_unilateral/
│   ├── annotation.csv
│   └── split.csv
└── data_unilateral/
    ├── <UID>/
    │   ├── Pre.nii.gz
    │   ├── Post_1.nii.gz      (Post_2 to Post_4 optional)
    │   └── Sub_1.nii.gz       first post-contrast subtraction, read by training.py and res_viz.py
    └── ...
```

`annotation.csv` has one row per breast: `UID` (the folder name in
`data_unilateral/`, ending in `_left` or `_right`), `PatientID`, `Age` (in days)
and `Lesion` (0, no lesion; 1, benign; 2, malignant). `split.csv` assigns each
`UID` to a `Fold` and a `Split` (`train`, `val` or `test`).

Example (made-up identifiers):

```csv
UID,PatientID,Age,Lesion
SITE_0001_left,SITE_0001,18263,0
SITE_0001_right,SITE_0001,18263,2
```

```csv
UID,Fold,Split
SITE_0001_left,0,train
SITE_0001_right,0,train
```

## Checks in `val.py`

1. Both folders are found, and `annotation.csv` and `split.csv` exist and can be read.
2. The required columns are present (`UID`, `PatientID`, `Lesion`; `UID`, `Fold`, `Split`).
3. Cohort size and class balance (warnings below 20 exams or below 5% malignant).
4. `Lesion` values are 0, 1 or 2.
5. Every `UID` is unique and ends in a laterality suffix.
6. Every annotated breast is assigned to exactly one split, and every assigned breast is annotated.
7. No patient appears in more than one split.
8. For up to 40 breasts, the folder exists and holds `Pre.nii.gz` and `Post_1.nii.gz`; it also counts how many have `Post_2` to `Post_4`.

Adapt the constants at the top of `val.py` (folder names, required series,
label coding, minimum sizes) to your own cohort.
