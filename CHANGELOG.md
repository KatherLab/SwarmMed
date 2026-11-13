# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
