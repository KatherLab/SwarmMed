# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Functional namespaced URL routing system across all core applications.
- Project Archiving system allowing authors to hide finished projects from the main dashboard.
- Dynamic colorful letter avatars for users in the header navigation.
- S3 integration for NVFlare jobs with secure credential injection via minimal `.env` files.
- Collaborative Project Board for internal team communication.
- Automated data validation and visualization background tasks via Celery.
- Dark/Light mode theme toggle.

### Changed
- Refactored entire codebase to be PEP 8 compliant.
- Replaced hardcoded portrait images with dynamic initials-based avatars.
- Renamed "Finish Project" action to "Archive Project" for clarity.
- Unified dashboard card links to point directly to respective application modules.

### Fixed
- Resolved multiple `NoReverseMatch` errors in dashboard and network pages.
- Fixed `ValueError` in NVFlare workers caused by missing S3 environment variables.
- Corrected SVG coordinate typos in theme toggle icon.
- Fixed directory structure preservation in S3 file uploads.
