# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.4] - 2026-11-19

### Added

- A warning in SwarmMedHub and the CLI when a running training job has stalled
  ([Server failover](docs/failover.md#when-a-site-drops-out)). The job monitor
  flags a running job when the FL server reports a site as dropped out (its
  training job as missing in reports that span a minute, or its FL client as dead
  or disconnected) and the site has not taken part in the job since, which only
  the Hub that runs the FL server can read in the server's logs, or when no round
  has finished for `SWARMMEDHUB_STALL_WARNING_MINUTES` (default 60; `0` turns this
  check off; an invalid value means 60 and a warning in the log). The warning names
  the site and what to do: check the site, then stop the job and resume it
  (`swarmed training resume`). It does not change the job's status. A warning
  about a site disappears when the site takes part in the job again, one about
  rounds when a round finishes; both disappear when the job ends.
  `.env.template` sets the variable to 60.
- The warning appears under the progress bar on the Training page, as a "Stalled"
  badge on the dashboard, in the admin's list and page of training jobs, on the
  Logs page, and in `swarmed training status`, `list` and `watch` (a `warning:`
  line; with `--json` in `warnings` and in the job's `stall_warning`). The
  training status API and the CLI's job output also give `last_round_at`, the time
  a round last finished or the job started running. The training-state endpoint
  that Hubs read from each other gives both fields; the Hubs that read it do not
  show them yet.
- `TrainingJob.last_round_at` and `TrainingJob.stall_warning` (migration
  `training/0003`). `stall_warning` has a database default, so that code without
  the field, such as 1.0.3, can still create training jobs after the migration.
- `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` (seconds) sets the FL server's
  `admin_timeout`: how long the server waits for each site's reply, for example
  when it deploys a job (10 s as provisioned). Before SwarmMed starts an FL
  server, the standby server included, it writes the value into the server's
  `local/resources.json`, which FLARE reads before `resources.json.default`. The
  file is made from itself, from `resources.json.default` or from the
  provisioning defaults, and keeps every other setting. Set the variable for the
  process that starts the FL server. For a start from the CLI or the web
  interface, that is the Hub's Celery worker: put the variable in
  `docker-compose.override.yml` under `celery_worker`, not in `.env`, from which
  `make env` drops variables that `.env.template` does not list. It applies from
  the next start of the server. Start the server the same way as before, or it
  can come back in another image with other time limits
  ([Server failover](docs/failover.md#resuming-over-a-slow-link)).
- `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` (whole seconds) sets how long the FL
  server waits for the starting site to start the swarm workflow
  (`start_task_timeout`, 10 s in NVIDIA FLARE 2.7.1). Like the other time limits,
  it is read by the process that submits the job.
- `swarmed training resume` warns that a resumed job ships its checkpoint and,
  should the job fail over a slow link, names the two settings to change before
  resuming the interrupted job again. The warning is left out when
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` is set in the environment of the command
  itself.
- `experiments/duke_mst_cli/explain/run_explainability.py` takes `--split` and
  `--site-institution`, so that a training site can explain cases of its own share,
  for example its validation cases, with the global model it holds
  (`--site-institution node_A --split val --cohorts site`). These cases have no
  published AUROC, so the AUROC check is skipped with a warning. A test across
  three hospitals used these options to explain each site's validation cases. The
  script checks the cohorts before it loads the model (`site` needs
  `--site-institution`), and stops with a message that names the split when a
  cohort has no cases in it.

### Changed

- A resumed job waits 600 s for the swarm workflow to start instead of the default
  of the FL server's NVIDIA FLARE build. That is longer than NVIDIA FLARE 2.7.1's
  10 s but shorter than the 360 000 s of the worked example's image; set
  `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` to keep a longer limit. The FL server
  also checks a resumed job's status reports every 10 s instead of every 2 s
  (`job_status_check_interval`), so that after a long start every site has
  reported before the first check: the status-report limit (90 s in NVIDIA FLARE
  2.7.1) counts from the start of the job, a site reports only once its first
  learn task has started, and in a test across three hospitals the starting
  site's first report arrived 0.26 s before FLARE's default first check, 2 s after
  the start.
- `docs/nvflare-comparison.md` lists `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` and
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` with the other time limits and states that the
  defaults of the workflow's deadlines come from the NVIDIA FLARE build in the
  image the FL server runs in: 360 000 s (100 h) for the status-report and
  progress limits in the worked example's image, and NVIDIA FLARE 2.7.1's 90 s and
  3 600 s in the image SwarmMed builds.
- `swarmed training watch` reads the job again before every poll, so that it shows
  what the worker's job monitor records, the stall warning included.
- The breast-MRI worked example's scripts (`evaluate_global_model.py`,
  `explain/run_explainability.py`, `explain/recompute_cam.py` and
  `explain/render_from_npy.py`) default to `/mnt/data/DUKE_iid` and
  `/mnt/data/medswarmdata` instead of folders of the host they were first run on,
  and `run_explainability.py --ref-json-dir` defaults to
  `results/duke_mst_cli/repetition_1` in the working directory.
  `experiments/duke_mst_cli/explain/README.md` describes the inputs with
  placeholders. Tests use an address from the documentation range (`192.0.2.10`)
  for example hosts.

### Fixed

- Resuming a run could fail over a slow link, because a resumed job carries its
  checkpoint (94 MB for the breast MRI model). In a test across three hospitals,
  the starting site did not accept the job within the FL server's 10 s
  `admin_timeout`, so the job started without it and failed (`invalid value ...
  in 'participating_clients'`); 300 s fixed this. The server, restarted through
  the CLI for that setting, now ran in SwarmMed's own image with NVIDIA FLARE
  2.7.1's limits (the worked example's image waits 360 000 s), and the workflow
  did not start within its 10 s `start_task_timeout`: the starting site confirms
  the start only once it has sent the first learn task to the other sites, and in
  a resumed run that task carries the checkpoint (110 s there; new runs take 1 to
  2.4 s). A resumed job now waits 600 s for the workflow to start unless
  `SWARMMEDHUB_SWARM_START_TASK_TIMEOUT` is set; for the deployment, set
  `SWARMMEDHUB_FLARE_ADMIN_TIMEOUT` and restart the server, with the settings it
  was started with, before resuming
  ([Server failover](docs/failover.md#resuming-over-a-slow-link)).
- A time limit set to NVIDIA FLARE 2.7.1's default
  (`SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL=90` or
  `SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT=3600`) did not reach the job: FLARE's job
  export leaves out arguments equal to the Hub's defaults, so the FL server used
  the default of its own image, 360 000 s in the worked example's image. SwarmMed
  now writes every time limit it sets, the new start timeout included, into the
  job's server configuration.
- `SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL` and
  `SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT` accepted `nan` and `inf`, which NVIDIA
  FLARE takes as limits that never expire. Such values are now ignored with a
  warning, like other invalid values.
- A running job that a site had dropped out of showed no sign of it. In a test
  across three hospitals, one site's training process was killed and the other
  sites waited for it; the FL server reported the site's job as missing every
  10 s, its time limits did not end the job, and SwarmMedHub showed the job as
  "Running" for five hours. Such a job now carries the warning above.
- On the Training page and in the CLI, a running job's progress counted its first
  round as finished before it had finished: a job of 10 rounds showed 10 % from
  the start, with an ETA based on that. Its progress now stays at 0 %, with the
  ETA "Calculating...", until the first round has finished. `rounds_finished` in
  the CLI's `--json` job output and the training-state endpoint stays empty
  (null) until the job's first round has finished; the job monitor used to set it
  to 0 on its first pass.
- The breast-MRI worked example could run out of memory on hosts with many CPUs.
  MediSwarm's data module in the training image starts one data-loader worker per
  CPU of the host, and in a test across three hospitals, on servers with 20 to 36
  CPUs and an installation limited to 16 CPUs and 28 GiB, a training process was
  stopped for running out of memory. `experiments/duke_mst_cli/training.py` now
  starts no more workers than the CPUs available to the process and at most
  `SWARMMED_MAX_LOADER_WORKERS` (default 16), and logs the count when it starts
  (`Data-loader workers: 16 (host CPUs: 36, available: 36, cap 16)`). The CPUs
  available are the host's CPUs, limited by the process's CPU affinity and the
  client container's own CPU quota as Docker shows it by default (with a private
  cgroup namespace on cgroup v2, or on cgroup v1); a quota on an enclosing
  container is not seen. The paper's runs used hosts with 16 CPUs and so 16
  workers, which the default reproduces. The runs in that test used an earlier
  form of the cap, which gave 16 workers on those hosts too. SwarmMed passes
  `SWARMMED_MAX_LOADER_WORKERS` to the FL client containers like the example's
  other site settings. The helper that does this is `swarmmed_loader_workers.py`,
  named so that it does not replace the `loader_workers` module of MediSwarm 1.8.0
  and later. Projects created from `experiments/duke_mst_cli` before 1.0.4 keep the
  code stored with them. Replace it, as the project's author, with
  `swarmed project update --user <hub-user> <PROJECT_UUID>
  --code-dir ./experiments/duke_mst_cli`, which uploads `swarmmed_loader_workers.py`
  together with `training.py`; `training.py` no longer runs without it.
- `experiments/duke_mst_cli/explain/README.md` gave the occlusion settings of the
  first round of explanations (48×48 patches at stride 32 on every slice) and
  about 20 minutes for a run, and said that a failed AUROC check stops the run
  before any figures are produced. Like the explainability guide, it now gives the
  defaults (16×16 patches at stride 8 on every fourth slice), the 83 minutes of the
  recorded run of 16 cases, and that the maps and figures of the cohorts checked
  before a failed check are already written. It also says that
  `recompute_cam.py` and `render_from_npy.py` cannot process a run of a site's own
  cases, its output tree lists the files the script writes, and it names its
  checkpoint's FLARE job instead of linking to a file outside the repository.

## [1.0.3] - 2026-11-16

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
- `examples/ecg_ptbxl/README.md` records runs of the template across three hospitals
  and a coordinating host, each on its own institutional network (13 November 2026,
  and three 20-round runs with seeds 0 to 2 on 16 November 2026), with deployment
  notes; `docs/networking.md` lists that deployment in its validation status.

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
