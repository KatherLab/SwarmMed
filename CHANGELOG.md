# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-03-18

### Added
- **Streamlined Workflow:** Introduced a comprehensive `Makefile` for automated setup (`make setup`) and execution (`make start`).
- **TLS-Isolated Sandboxing:** Implemented a dedicated `sandbox-dind` service with mutual TLS and resource limits (1GB RAM, 1 CPU) for secure script execution.
- **Secure Telemetry:** Added a `tailscale-status` sidecar to monitor VPN connectivity without host socket exposure.
- **Automated Configuration:** Created `scripts/setup_env.py` for interactive `.env` generation and secure secret rotation.
- **Enhanced Documentation Suite:** Completely overhauled `@docs/` and root Markdown files with updated installation, security, and developer guides.
- **Extended Sandbox Environment:** Pre-installed a wide range of ML/Data Science libraries (PyTorch, TensorFlow, Transformers, MONAI, etc.) in the sandbox image.
- **PgBouncer Integration:** Added PgBouncer for secure database connection pooling with SCRAM credential support.

### Changed
- Migrated all repository links to point to the `SwarmCloud` GitHub project.
- Standardized platform branding to "SwarmMedHub" across all documentation and templates.
- Updated internal service mesh to use a private Root CA issued via `make setup`.
- Refactored `flare_adapter` to support streaming data filesystem with host-side IP auto-discovery.

### Fixed
- Resolved Postgres and Redis SSL key permission issues via host-side configuration guidelines.
- Fixed sandbox connectivity to the internal MinIO gateway.
- Corrected various `NoReverseMatch` errors in the network and dashboard templates.

## [Unreleased]
