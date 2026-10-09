"""Tests for what a locally built application image carries."""

import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from docker.utils import exclude_paths


def _build_context(paths):
    """Return the subset of ``paths`` that the project's .dockerignore keeps.

    ``exclude_paths`` is the docker SDK's implementation of the Docker client's
    .dockerignore rules, which the Hub also uses to build the sandbox image.
    """
    text = (Path(settings.BASE_DIR) / ".dockerignore").read_text()
    lines = (line.strip() for line in text.splitlines())
    patterns = [line for line in lines if line and not line.startswith("#")]
    with tempfile.TemporaryDirectory() as root:
        for path in paths:
            target = Path(root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        return exclude_paths(root, patterns) & set(paths)


class DockerignoreTests(SimpleTestCase):
    """The Dockerfile copies the whole build context into the image (``COPY . .``).

    The application and worker containers bind-mount the project directory and
    get their environment from ``env_file``, so the image needs none of the
    deployment's secrets or state.
    """

    def test_deployment_secrets_and_state_stay_out_of_the_image(self):
        deployment_files = [
            ".env",
            "workspaces/project/network/workspace/study/state/cert.json",
            "workspaces/project/network/workspace/study/prod_00/.env",
            "results/run/model.pt",
            "docker-compose.override.yml",
        ]
        self.assertEqual(_build_context(deployment_files), set())

    def test_files_the_image_build_uses_stay_in_the_context(self):
        build_inputs = [
            "pyproject.toml",
            "README.md",
            "entrypoint.sh",
            "package.json",
            "static/src/input.css",
            "templates/base.html",
            "manage.py",
            "scripts/setup_pgbouncer.py",
            "apps/results/views.py",
        ]
        self.assertEqual(_build_context(build_inputs), set(build_inputs))
