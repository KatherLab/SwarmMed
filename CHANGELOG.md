# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A standby FL server and resuming an interrupted run ([Server failover](docs/failover.md)).
  `swarmed network failover --server-host IPV4_ADDRESS [--start-server]`
  switches a Hub's FL clients and admin connection to another server and, at
  the standby site, starts the server from the server kit in the site's startup
  package. `swarmed training resume` submits a new job that starts at the
  interrupted round from the newest checkpoint at the site and keeps the
  interrupted run's round numbers; `swarmed training checkpoints` lists the
  checkpoints. Only PyTorch jobs can be resumed.
- Every training site saves the model each round starts from
  (`swarmmed_resume_checkpoint.pt`); `SWARMMEDHUB_RESUME_CHECKPOINTS=false`
  turns this off.
- Startup packages also contain the FL server's local settings
  (`server_local/`), so that a participating site can start the server.
- `start_swarm_network_task` accepts a role (`server` or `client`) and a list of
  client participants for one call.
- A test that runs a job and a resumed job in NVIDIA FLARE's simulator
  (`SWARMMEDHUB_RUN_FLARE_SIMULATOR=1`).
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
- NetBird as the recommended overlay network, with Tailscale as an alternative. The
  helper `scripts/overlay_watcher.py` records this host's NetBird or Tailscale
  address in `tmp/overlay_status.json` every 30 s; `make overlay` starts it,
  `make overlay-stop` stops it, and `make start` runs `make overlay`.
  `apps/network/overlay.py` reads the status for the web interface, the CLI and the
  worker.
- Settings: `SWARMMEDHUB_OVERLAY` (`auto`, `netbird`, `tailscale` or `none`; `auto`
  uses NetBird if it is connected, otherwise Tailscale), `SWARMMEDHUB_LOCAL_ADDRESSES`
  (further addresses of this host, for example a WireGuard address) and
  `SWARMMEDHUB_OVERLAY_STALE_SECONDS` (default 120).
- An optional FL server address field in the web form for a new network.
- `docs/networking.md` and `docs/installation.md`: setting up NetBird with a
  self-hosted management server, NetBird Cloud for a trial, Tailscale and Headscale,
  WireGuard, OpenVPN and IPSec, restricting peers to the FL ports, and what has been
  tested.

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
- The Hub recognises itself by every overlay address and by
  `SWARMMEDHUB_LOCAL_ADDRESSES`, not only by its Tailscale address.
- A new network needs a current overlay status: if the status is missing, older than
  the stale limit, dated in the future or disconnected, the web form and
  `swarmed network create` ask for the FL server address instead of guessing it.
- Breaking: an invalid `--server-ip` for `swarmed network create` is now an error
  (exit code 2); it used to be ignored.
- `make tailscale` and `scripts/tailscale_watcher.sh` remain as old names; replace
  cron or systemd entries for the old script with `make overlay`.
- The prerequisites list `python3` (3.8 or later) instead of `jq`.
- The example overlay address in the documentation is `100.64.0.10`.

### Fixed

- Stopping a network from the web interface or with `swarmed network stop`
  failed in 1.0.0 to 1.0.2: the stop task had lost its Celery task decorator.
- New installations: the Celery worker restarted in a loop. kombu 5.7.0 (released on
  3 November 2026) needs redis-py 6.4 or later, SwarmMed pins redis 5.0.1, and the
  Dockerfiles install without `uv.lock`, so every image built since then got kombu
  5.7.0. kombu is now pinned to 5.6.2. This also affects 1.0.2.
- FL clients could fail to train after a startup package with the server's local
  settings (`server_local/`) was imported. The import copied the server's and
  the admin's folders out of each site's kit but also left them in the site's
  folder, which is the site's FLARE workspace. FLARE takes the first
  configuration file of a name anywhere in that workspace, so a client could use
  the server's `comm_config.json`; its job process then looked for its parent
  process at the FL server, and the client never started training. The import
  now moves these folders to `prod_00/server/` and
  `prod_00/admin-<site>@nvidia.com/` instead. A single site's kit imported on its
  own, where `prod_00` itself is the site's workspace, keeps the server's folders
  in the network's folder. Starting a client moves them out of networks imported
  before. The internal host is now set to `localhost` in the `comm_config.json`
  that FLARE reads, not only in `local/comm_config.json`. No release contained
  `server_local/`, which came with the standby server above, but imports by
  1.0.0 to 1.0.2 left `server_startup/` and `admin_startup/` in each site's
  folder; starting a client moves these too.
- Exporting the startup package from a Hub that had imported it wrote some files
  of each site's kit twice: `.network_id`, `.participants.json` and
  `.gossip_token`, and the server's and the admin's kits that imports by 1.0.0
  to 1.0.2 left in the site's folder. Each file is now written once.
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
- `make env`, and so `make start` and `make restart`, failed with a traceback when
  no answer could be read (no terminal, closed input). It now keeps the existing
  values, uses the template's default for a new setting and says so, and stops with
  a message, without changing `.env`, if a placeholder such as the host name still
  needs a value.

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
