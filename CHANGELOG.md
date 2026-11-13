# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Six templates in `examples/` for other kinds of biomedical data, each a complete
  project folder (`training.py`, `prepare_data.py`, `validation.py`,
  `visualization.py`, `results_visualization.py`, `explainability.py`, `evaluate.py`
  and a README that records what was run): `tabular_heart_disease` (UCI Heart
  Disease), `ehr_eicu_mortality` (eICU-CRD Demo), `ecg_ptbxl` (PTB-XL),
  `multimodal_ptbxl` (PTB-XL ECG with age, sex, height and weight),
  `audio_heart_sounds` (PhysioNet/CinC Challenge 2016) and `eeg_sleep_staging`
  (Sleep-EDF Expanded). Each trained to completion on a local-test network of a
  fresh installation. `docs/templates.md` and `examples/README.md` list them;
  `docs/bring-your-own-task.md` describes how to write a template for a new task.
- Explanations in SwarmMedHub. A project's optional `explainability.py` runs in the
  sandbox on a job's results (by default 2 CPUs, 2 GB and 15 minutes:
  `SWARMMEDHUB_EXPLAIN_MEM_LIMIT`, `SWARMMEDHUB_EXPLAIN_CPUS`,
  `SWARMMEDHUB_EXPLAIN_TIMEOUT`). The Results page shows the run information, the
  run-level figures and the panels of each case, and project members review each
  case. In blinded review, which a reviewer starts in, a case shows only its ID, its
  label and its input panels until the reviewer has saved a read of it; cases are
  stored in random order. Migrations `project/0003` and `results/0002`.
- The `swarmmed_explain` package: explanation methods (occlusion, integrated
  gradients, gradient × input, Grad-CAM, feature ablation, permutation importance),
  case selection, agreement between methods, plotting helpers, the bundle format with
  its checks, and `python -m swarmmed_explain.run_local` for running an
  `explainability.py` outside the Hub.
- CLI: `swarmed results explain`, `swarmed results explanations` and
  `swarmed results explain-import`.
- `experiments/duke_mst_cli/explain/export_hub_bundle.py` converts the breast MRI
  explainability outputs into a bundle that the Hub can import.

### Changed

- The README and the documentation describe SwarmMed as a framework for biomedical
  data with templates; the breast MRI study remains the worked example.
- The default `ALLOWED_EXTENSIONS` (`core/settings/base.py` and `.env.template`)
  adds `.wav`, `.flac`, `.edf`, `.bdf`, `.dat`, `.hea`, `.mat` and `.tsv`.
  Installations set up earlier keep their list in `.env`; add the new extensions
  there.
- `swarmed project create --code-dir` and code uploads skip `__pycache__` folders
  and `.pyc` files.
- The personal-data export lists the user's explanation runs (without their script
  output) and reviews.

### Fixed

- New installations: the Celery worker restarted in a loop. kombu 5.7.0 (released on
  3 November 2026) needs redis-py 6.4 or later, SwarmMed pins redis 5.0.1, and the
  Dockerfiles install without `uv.lock`, so every image built since then got kombu
  5.7.0. kombu is now pinned to 5.6.2. This also affects 1.0.2.
- The web interface's "Select project folder" stored the files below the folder's
  own name, so the job found no `training.py` at the root. An upload without a
  root-level `training.py` is now refused.
- The error pages failed when a request had no user, for example with a host name
  that is not in `DJANGO_ALLOWED_HOSTS`, so such a request got HTTP 500 instead of 400.
- The Results page: Sync Results and the result downloads did not work when the
  project had no results visualization script or no job was selected.
- The Results page inserts the visualization status as text, not as HTML.
- The README said that sandbox scripts can reach the Docker socket proxy; they
  cannot, nor PostgreSQL or Redis. `docs/security.md` now says that they can reach
  the internet while they run.

## [1.0.2] - 2026-11-05

Documentation, metadata and analysis scripts. The only change to the platform is
the text of the privacy page.

### Added

- `experiments/duke_mst_cli/train_local_baseline.sh`, which trains the matched
  single-site and centralised baselines of the breast MRI worked example in the
  image and with the settings of the swarm runs.
- `experiments/duke_mst_cli/build_matched_baseline_report.py`, which compares the
  swarm runs with the matched baselines, and the single runs (FedProx, the run
  without node C, the web-interface run and the negative control) with the swarm
  runs, from per-case predictions.

### Changed

- `docs/reproduce.md` describes the matched baselines, the FedProx run and the
  new report, and that the Duke split files are published.
- `docs/os-support.md` states which operating systems have been tested.
- `docs/nvflare-comparison.md`: how the sandboxed site scripts read a site's data.
- `CITATION.cff`: version, release date, affiliations, abstract and message.

### Fixed

- The privacy page no longer claims differential privacy, secure aggregation or
  Byzantine-robust aggregation, gives TLS 1.2 or 1.3 for the web interface, and
  lists startup kits with their private keys among the stored data.
- `examples/breast_mri/README.md` matches the current scripts and uses made-up
  example rows.
- `examples/breast_mri/data_splits/README.md`: the Duke dataset DOI, licence
  (CC BY-NC 4.0) and citations, and the split counts.
