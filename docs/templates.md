---
title: Templates
description: Project folders for several kinds of biomedical data, how to run them, and what was run with each.
---

# Templates

A template is a folder in `examples/` that works unchanged as a project's code
directory (`swarmed project create --code-dir`). It holds what a study of one kind
of data needs, set up for a public dataset, so that you can run SwarmMed on real
data first and then adapt the code to your own. To write a bundle from scratch, see
[Bring your own task](bring-your-own-task.md); the
[code bundle reference](developer.md) documents each script's interface.

## Catalogue

All six templates train a small PyTorch model; every run so far used CPUs only. Each was
run to completion on 11 November 2026 in NVIDIA FLARE's simulator, with the job SwarmMed
builds for the project, one simulated site per folder, on one host. On the same day,
each template's `validation.py` and `visualization.py` ran in a SwarmMedHub sandbox on
the prepared data. On 11 and 12 November 2026 each template also trained to completion
on a SwarmMed local-test network, all sites as containers on one host, on a fresh
installation of SwarmMed, through the CLI steps in its README; its `validation.py`,
`visualization.py`, `results_visualization.py` and `explainability.py` ran in that Hub's
sandbox. On 13 and 16 November 2026, with a development version of SwarmMed (changes now
in 1.0.3), `ecg_ptbxl` also trained across three hospitals, each on its own server and
institutional network, on public data placed at the hospitals, in the end three times
with different seeds (see
[its README](https://github.com/KatherLab/SwarmMed/blob/main/examples/ecg_ptbxl/README.md#what-was-run)).
Apart from the three-hospital runs of `ecg_ptbxl`, each result below comes from one run
with one seed and one data split; the results show that training completes with natural
or subject-level site splits, not how well the models perform. Each README gives the
per-site results, the run times, the environment and the caveats.

| Template | Data and task | Dataset (licence) | Model | Simulator run, 11 Nov 2026 | Run on SwarmMed |
|---|---|---|---|---|---|
| [`tabular_heart_disease`](https://github.com/KatherLab/SwarmMed/tree/main/examples/tabular_heart_disease) | Clinical variables; coronary artery disease present or absent | [UCI Heart Disease](https://archive.ics.uci.edu/dataset/45/heart+disease), 4 hospitals (CC BY 4.0) | MLP, 545 parameters | 4 sites, 10 rounds of 10 epochs; pooled test AUROC 0.861 (95% CI 0.807-0.906) | 11 Nov 2026, local-test network, 4 sites on one host: pooled test AUROC 0.861 (0.807-0.906), the same as the simulator |
| [`ehr_eicu_mortality`](https://github.com/KatherLab/SwarmMed/tree/main/examples/ehr_eicu_mortality) | First 24 hours of an ICU stay; in-hospital death | [eICU-CRD Demo 2.0.1](https://physionet.org/content/eicu-crd-demo/2.0.1/), 3 US regions (ODbL 1.0, share-alike: never publish the prepared files) | MLP, 1,377 parameters | 3 sites, 20 rounds of 5 epochs; pooled test AUROC 0.815 (0.681-0.930), 18 deaths among 199 test patients | 12 Nov 2026, local-test network, 3 sites on one host (second attempt): pooled test AUROC 0.815 (0.681-0.930), the same as the simulator |
| [`ecg_ptbxl`](https://github.com/KatherLab/SwarmMed/tree/main/examples/ecg_ptbxl) | 12-lead ECG at 100 Hz; normal or abnormal | [PTB-XL 1.0.3](https://physionet.org/content/ptb-xl/1.0.3/), sites by recording site (CC BY 4.0) | 1-D CNN, 79,169 parameters | 4 sites, 5 rounds of 1 epoch; pooled test AUROC 0.922 (0.911-0.932) | 11 Nov 2026, local-test network, 4 sites on one host: pooled test AUROC 0.922 (0.910-0.932); 16 Nov 2026, 3 hospitals on separate hosts joined by a VPN, one site each, 20 rounds, three runs (seeds 0 to 2): pooled test AUROC 0.928 (SD 0.005) over the 3 sites, 0.954 (SD 0.008) on `fl-client-4` (PTB-XL's other recording sites and records without one), which none of them trained on |
| [`multimodal_ptbxl`](https://github.com/KatherLab/SwarmMed/tree/main/examples/multimodal_ptbxl) | ECG plus age, sex, height and weight; normal or abnormal | PTB-XL 1.0.3, as above (CC BY 4.0) | 1-D CNN with a metadata branch, 79,297 parameters | 4 sites, 5 rounds of 1 epoch; pooled test AUROC 0.925 (0.915-0.935); the metadata add little | 11 Nov 2026, local-test network, 4 sites on one host: pooled test AUROC 0.925 (0.914-0.935) |
| [`audio_heart_sounds`](https://github.com/KatherLab/SwarmMed/tree/main/examples/audio_heart_sounds) | Heart sound recordings as log-mel spectrograms; abnormal or normal | [PhysioNet/CinC Challenge 2016](https://physionet.org/content/challenge-2016/1.0.0/), 4 source databases (ODC-By 1.0) | 2-D CNN, 60,641 parameters | 4 sites, 12 rounds of 1 epoch; pooled test AUROC 0.807 (0.758-0.856), carried by one source whose label is confounded with the recording conditions; weak at source a (0.71, 0.58-0.83), near chance at b and f | 12 Nov 2026, local-test network, 4 sites on one host: pooled test AUROC 0.801 (0.752-0.851) |
| [`eeg_sleep_staging`](https://github.com/KatherLab/SwarmMed/tree/main/examples/eeg_sleep_staging) | Two EEG channels in 30-s epochs; five sleep stages | [Sleep-EDF Expanded 1.0.0](https://physionet.org/content/sleep-edfx/1.0.0/), 36 subjects in 4 groups of 9 (ODC-By 1.0) | 1-D CNN, 52,997 parameters | 4 sites, 10 rounds of 2 epochs; pooled test accuracy 0.754, macro-F1 0.707, Cohen's kappa 0.665 (4 test subjects) | 12 Nov 2026, local-test network, 4 sites on one host: pooled accuracy 0.751, macro-F1 0.705, kappa 0.661 |

The [`examples/` catalogue](https://github.com/KatherLab/SwarmMed/tree/main/examples)
also lists the framework examples and the breast MRI site scripts:

* **Framework examples** (`pytorch`, `pytorch-lightning`, `tensorflow`, `keras`,
  `scikit-learn`, `huggingface`, `monai`): a `training.py` and a `res_viz.py` each,
  for synthetic data. Only the PyTorch example has been run with SwarmMed, in the
  [scalability benchmark](scalability.md); the others are provided but not validated.
* **`breast_mri`**: the site scripts (data checks, data plots, an example training
  script and a results plot) of the [breast MRI worked example](reproduce.md). They
  are not a template: they have to be renamed before use, and the worked example was
  trained with `experiments/duke_mst_cli/training.py`.

## What a template contains

| File | Role |
|---|---|
| `training.py` | Required. The training loop through `flare_adapter`: receives the global model, scores it on the site's validation split, trains it on the site's training split and sends the weights back. Uses only packages of the runtime image (numpy, pandas, scikit-learn, scipy, torch). |
| `requirements.txt` | The packages `training.py` needs, pinned to the runtime image's versions. |
| `validation.py` | Data checks, run in the Hub's sandbox. |
| `visualization.py` | Up to four data plots, run in the sandbox. |
| `results_visualization.py` | Up to four result plots, run in the sandbox. |
| `explainability.py` | Per-case explanations, reviewed on the **Results** page ([Explainability](explainability.md)). |
| `prepare_data.py` | Run at a site before import: downloads the public dataset if asked and converts it into the layout below. Needs only numpy, pandas, scipy and the standard library. |
| `evaluate.py` | Run at a site: scores a saved global model on the site's test split. |
| `README.md` | Task, dataset (citation, licence, URL), model, data layout, how to run it, what was run with what result, and limitations. |

`--code-dir` uploads `prepare_data.py`, `evaluate.py` and the README as training
code too; the platform does not use them. Keep prepared data outside the template
folder, because every file in it is uploaded.

## Data layout

A project's registered data holds either:

* **one site's files at the root** (`train.npz`, `val.npz`, `test.npz`,
  `site_info.json`): a real deployment, in which each site registers only its own
  data; or
* **one folder per FLARE site name** (`fl-client-1/`, `fl-client-2/`, ...): each
  client reads only the folder named after it, so that a local-test network on one
  host gives each simulated site its own share.

`training.py` takes the site name from NVIDIA FLARE (`nvflare.client.get_site_name()`,
falling back to `SWARMMED_SITE_NAME`), reads the keys that start with `<site>/`, falls
back to files at the root, and stops with an error that lists the folders it found
when neither exists. `prepare_data.py --layout local-test` writes the `fl-client-k/`
folders; `--layout single --site-index k` writes one site's files. `site_info.json`
records the source, the counts, the label map and the preprocessing.

## Metrics in the logs

Each round, every site scores the global model it received on its validation split
before it trains, sends these metrics with its weights, and prints one line, so that a
run's learning curve can be read from the client logs:

```text
SWARMMED_METRIC site=fl-client-1 round=9 split=val auroc=0.9329 auprc=0.9333 accuracy=0.8525 ...
```

## Running a template

Each README gives the exact commands. The common shape, with the tabular template:

### Command line

The CLI runs in the application container, where the repository is `/app`, so the
code and the data must lie inside the repository; its `tmp/` folder is ignored by git.
Run the commands from the repository root ([CLI](cli.md)). `prepare_data.py` needs
Python 3.11 or later with numpy, pandas and scipy.

```bash
python examples/tabular_heart_disease/prepare_data.py --download \
  --raw-dir tmp/heart_disease/raw --out tmp/heart_disease/local-test

swarmed project create --user alice --title "Heart disease" \
  --code-dir examples/tabular_heart_disease
swarmed data import --user alice --project <PROJECT_UUID> tmp/heart_disease/local-test/fl-client-*
swarmed data validate  --user alice --project <PROJECT_UUID> --wait
swarmed data visualize --user alice --project <PROJECT_UUID> --wait
swarmed network create --user alice --project <PROJECT_UUID> --name "Heart local" --local-test --clients 4
swarmed network start  --user alice <NETWORK_UUID> --wait
swarmed training start --user alice --network <NETWORK_UUID>
swarmed training watch --user alice <JOB_UUID>
swarmed results sync   --user alice --project <PROJECT_UUID>
swarmed results visualize --user alice --project <PROJECT_UUID> --job <JOB_UUID> --wait
swarmed results explain   --user alice --project <PROJECT_UUID> --job <JOB_UUID> --wait
swarmed network stop   --user alice <NETWORK_UUID> --wait
```

* Import the `fl-client-k` folders themselves and do not use `--dest`. Each folder is
  stored under its own name (`fl-client-1/train.npz`, ...), which is where the
  clients look. Importing their parent folder, or giving `--dest`, puts a prefix in
  front of every key (`local-test/fl-client-1/train.npz`), and the clients find no
  data.
* For one site's data, import the files themselves
  (`tmp/heart_disease/site-2/*`); they are stored at the data root.
* `results explanations` lists the explained cases and their reviews.

### Web interface

1. **New project:** under **Training Code**, choose **Select individual files** and
   select the template's `training.py`. Put `requirements.txt`, `validation.py`,
   `visualization.py`, `results_visualization.py` and `explainability.py` into their
   own fields (**Requirements File**, **Data Validation Script**, and so on).
   **Select project folder** drops the selected folder's own name, so the template's
   `training.py` is stored at the root, but it uploads only the `.py` files and
   stores all of them as training code: the other scripts and `requirements.txt`
   still have to go into their own fields. The Hub refuses training code that has no
   `training.py` at its root.
2. **Data**, **Import Data**: with **Destination folder** (root), select one
   `fl-client-k` folder at a time. The page stores the files below the folder's name
   (`fl-client-1/train.npz`), as the clients expect.
3. On the **Data** page, run the validation and the visualization.
4. Create a network with **Test in local environment**, start it, and start the
   training. Such a network has two clients, `fl-client-1` and `fl-client-2`, so only
   the first two folders are used; for every site, create the network with the CLI's
   `--clients`.
5. On the **Results** page, select the job, click **Run Visualization**, and click
   **Run Explainability** in the **Explainability** section.

For one site's data in a real deployment, the files must lie at the data root or in
a folder named after the site's name in the network. The web import always keeps the
folder's name, so either name the folder after the site or import the files with the
CLI.
