# Examples

Project code for SwarmMed, in three groups:

1. **Templates** for six kinds of biomedical data, each set up for a public dataset.
   A template folder works unchanged as a project's code directory
   (`swarmed project create --code-dir examples/<template>`).
2. **Framework examples**, one per machine-learning library, on synthetic data.
3. **The breast MRI site scripts** of the worked example.

The documentation describes how to run a template
([Templates](../docs/templates.md)) and how to write your own bundle
([Bring your own task](../docs/bring-your-own-task.md)).

## Templates

| Folder | Data and task | Dataset (licence) |
|---|---|---|
| [`tabular_heart_disease/`](tabular_heart_disease/README.md) | Clinical variables of four hospitals; coronary artery disease present or absent | UCI Heart Disease (CC BY 4.0) |
| [`ehr_eicu_mortality/`](ehr_eicu_mortality/README.md) | First 24 hours of an ICU stay in three US regions; in-hospital death | eICU-CRD Demo 2.0.1 (ODbL 1.0; never publish the prepared files) |
| [`ecg_ptbxl/`](ecg_ptbxl/README.md) | 12-lead ECG at 100 Hz; normal or abnormal | PTB-XL 1.0.3 (CC BY 4.0) |
| [`multimodal_ptbxl/`](multimodal_ptbxl/README.md) | ECG plus age, sex, height and weight; normal or abnormal | PTB-XL 1.0.3 (CC BY 4.0) |
| [`audio_heart_sounds/`](audio_heart_sounds/README.md) | Heart sound recordings as log-mel spectrograms; abnormal or normal | PhysioNet/CinC Challenge 2016 (ODC-By 1.0) |
| [`eeg_sleep_staging/`](eeg_sleep_staging/README.md) | Two EEG channels in 30-s epochs; five sleep stages | Sleep-EDF Expanded 1.0.0 (ODC-By 1.0) |

Each template was run to completion on 11 November 2026 in NVIDIA FLARE's simulator,
with the job SwarmMed builds for it, on one host with one simulated site per data
folder, CPU only. On the same day its `validation.py` and `visualization.py` ran in a
SwarmMedHub sandbox. On 11 and 12 November 2026 each template also trained to completion
on a SwarmMed local-test network (all sites on one host) on a fresh installation of
SwarmMed, and its `validation.py`, `visualization.py`, `results_visualization.py` and
`explainability.py` ran in that Hub's sandbox. The "What was run" section of each README
lists every run with its date, environment and result, and the limitations of the
data. None of the folders contains dataset files; `prepare_data.py` downloads the data
from the dataset's own site.

Every template folder has the same files:

| File | Role |
|---|---|
| `training.py` | Training loop through SwarmMed's data adapter (`flare_adapter`); needs only packages of the runtime image |
| `requirements.txt` | The packages `training.py` imports, pinned to the runtime image's versions |
| `validation.py` | Data checks, run in SwarmMedHub's sandbox |
| `visualization.py` | Up to four data plots, run in the sandbox |
| `results_visualization.py` | Up to four result plots, run in the sandbox |
| `explainability.py` | Per-case explanations, reviewed on the Results page |
| `prepare_data.py` | Run by a site before import: downloads (optional) and converts the dataset into one folder per simulated site (`--layout local-test`) or one site's files (`--layout single --site-index K`); uploaded with `--code-dir` but not used by the platform |
| `evaluate.py` | Run by a site: scores a saved global model on its test split and writes JSON |
| `README.md` | Task, dataset, model, data layout, how to run it, what was run, explainability, limitations |

`--code-dir` uploads every file of a template folder, `prepare_data.py`, `evaluate.py`
and the README included; the platform does not use them. Keep prepared data outside
the template folders. `audio_heart_sounds/splits.csv` holds that template's assignment
of the public record names to the training, validation and test splits.

## Framework examples

Each folder has a `training.py` and a results visualization script, `res_viz.py`
(rename it to `results_visualization.py` in a code bundle). Only the PyTorch example
has been run with SwarmMed: in the [scalability benchmark](../docs/scalability.md),
on local-test networks of 3, 5, 10 and 20 clients (22 September 2026) and of 50 clients
(9 October 2026: the first attempt aborted, the second completed) on one host, CPU
only. The other examples are provided but not
validated.

| Folder | Library | Data it reads |
|---|---|---|
| [`pytorch/`](pytorch/) | PyTorch | Every `.csv` file in the project data; predicts `diagnosis` from the other columns except `patient_id` |
| [`pytorch-lightning/`](pytorch-lightning/) | PyTorch Lightning | As `pytorch/` |
| [`tensorflow/`](tensorflow/) | TensorFlow | As `pytorch/` |
| [`keras/`](keras/) | Keras | As `pytorch/` |
| [`scikit-learn/`](scikit-learn/) | scikit-learn (`SGDClassifier`) | As `pytorch/` |
| [`huggingface/`](huggingface/) | Hugging Face Transformers (`bert-base-uncased`, downloaded when the job starts) | `.csv` files with `text` and `label` columns; the synthetic files have no text, so a fixed dummy text is used, and without files a few dummy sentences |
| [`monai/`](monai/) | MONAI (`DenseNet121`) | NIfTI or PNG images in the site's local data directory, with random labels, or 100 simulated images; it shows the API only |

Files shared by the framework examples:

| File | What it is |
|---|---|
| [`biomed_data/`](biomed_data/) | Five CSV files (`batch_1.csv` to `batch_5.csv`, 1,000 synthetic patients in all) with `patient_id`, `age`, `sex`, `bmi`, `biomarker_A`, `biomarker_B` and the target `diagnosis` |
| [`generate_dataset.py`](generate_dataset.py) | Writes `biomed_data/`; `biomarker_A` correlates with the target, `biomarker_B` is noise |
| [`val.py`](val.py) | Example `validation.py` for `biomed_data/`: checks that the batch files exist and can be read, their columns, missing values, the age values and the total number of rows |
| [`viz.py`](viz.py) | Example `visualization.py` for `biomed_data/`: plots such as the age distribution and the diagnosis balance |
| [`requirements.txt`](requirements.txt) | Packages of the framework examples, most of them pinned |

Import the `biomed_data` folder itself (`swarmed data import ... examples/biomed_data`),
so that its files are stored as `biomed_data/batch_1.csv` and so on, where `val.py` and
`viz.py` look for them.

## Breast MRI worked example

[`breast_mri/`](breast_mri/README.md) holds the site scripts of the breast MRI worked
example: data checks (`val.py`), data plots (`viz.py`), an example training script for
the MST classifier (`training.py`), a results plot for a 3D ResNet checkpoint
(`res_viz.py`), their `requirements.txt` and the worked example's split files
(`data_splits/`). They are not a template: they have to be renamed before use, and
the worked example itself was trained with `experiments/duke_mst_cli/training.py`
([Reproducing the worked example](../docs/reproduce.md)).
