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

### Fixed

- Stopping a network from the web interface or with `swarmed network stop`
  failed in 1.0.0 to 1.0.2: the stop task had lost its Celery task decorator.
- New installations: the Celery worker restarted in a loop. kombu 5.7.0 (released on
  3 November 2026) needs redis-py 6.4 or later, SwarmMed pins redis 5.0.1, and the
  Dockerfiles install without `uv.lock`, so every image built since then got kombu
  5.7.0. kombu is now pinned to 5.6.2.
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
