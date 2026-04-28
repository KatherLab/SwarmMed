"""Django bootstrap helpers for the swarmed CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def bootstrap_django() -> None:
    """Initialize Django with the same module path semantics as manage.py."""
    repo_root = Path(__file__).resolve().parent.parent
    apps_path = repo_root / "apps"

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(apps_path) not in sys.path:
        sys.path.insert(0, str(apps_path))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

    import django

    django.setup()
