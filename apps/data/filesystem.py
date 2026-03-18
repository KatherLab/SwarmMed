"""Data FileSystem Utilities.

Provides a virtual filesystem layer that bridges Django's S3 storage
with local script execution, handling streaming and manifest generation.
"""

import json
import os
import shutil
import tempfile

from django.conf import settings

from logs import logger

from .utils import list_s3_folder


class DataFileSystem:
    """A streaming virtual filesystem powered by fsspec.

    Allows sandboxed scripts to stream data directly from MinIO via
    presigned URLs.

    Attributes:
        project_uuid (str): The UUID of the project.
        root_path (str): The root path in S3 for the project's data.
        log (Logger): The logger instance.
        manifest (dict): A mapping of file paths to presigned URLs.
        temp_dir (str): Path to a temporary directory for local file operations.
    """

    def __init__(self, project_uuid: str):
        """Initializes the DataFileSystem.

        Args:
            project_uuid (str): The UUID of the project.
        """
        self.project_uuid = project_uuid
        self.root_path = f"{project_uuid}/data/"
        self.log = logger.get_logger()

        # Manifest for the sandbox to use fsspec
        self.manifest = {}

        # Temp dir only for script files/plots, not for data storage
        self.temp_dir = tempfile.mkdtemp(
            prefix=f"data_{project_uuid}_", dir=settings.PROJECT_TEMP_DIR
        )

    def __enter__(self):
        """Enter the context manager.

        Returns:
            DataFileSystem: The instance itself.
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager and cleanup temporary directory.

        Args:
            exc_type: The type of the exception.
            exc_val: The exception instance.
            exc_tb: The traceback object.
        """
        self.cleanup()

    def cleanup(self):
        """Removes the temporary directory and its contents."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def build_manifest(self) -> dict:
        """Builds a manifest of file names to internal presigned URLs.

        Returns:
            dict: A dictionary mapping relative file paths to presigned URLs.
        """
        from common.utils import get_internal_s3_download_url, get_s3_client

        s3 = get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")

        manifest = {}
        for page in paginator.paginate(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=self.root_path
        ):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if not key or key.endswith("/"):
                    continue

                # Filter hidden files
                if any(part.startswith(".") for part in key.split("/")):
                    continue

                rel_path = key[len(self.root_path) :]
                # Generate a long-lived internal URL for the duration of the sandbox run
                manifest[rel_path] = get_internal_s3_download_url(
                    key, expires=3600
                )

        self.manifest = manifest
        return manifest

    def save_manifest(self, path: str):
        """Saves the manifest to a JSON file for the sandbox helper.

        Args:
            path (str): The file system path where the manifest JSON will be saved.
        """
        with open(path, "w") as f:
            json.dump(self.manifest, f, indent=2)

    def listdir(self, relative_path: str = "") -> list[str]:
        """Lists files and subdirectories from S3 (metadata only).

        Args:
            relative_path (str): The path relative to the project's data root.

        Returns:
            List[str]: A list of file and directory names.
        """
        prefix = f"{self.root_path}{relative_path}"
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        folders, files = list_s3_folder(prefix)
        items = []
        for folder in folders:
            name = folder[len(prefix) :].rstrip("/")
            if name:
                items.append(name + "/")
        for file in files:
            name = file[len(prefix) :]
            if name:
                items.append(name)
        return items


class ValidationContext:
    """A helper class provided to data validation scripts.

    Encapsulates the filesystem access and check reporting logic.

    Attributes:
        project_uuid (str): The UUID of the project.
        validation_run_id (str): The ID of the validation run.
        filesystem (DataFileSystem): The virtual filesystem instance.
        checks (list): A list of check results recorded by the script.
        log (Logger): The logger instance.
    """

    def __init__(self, project_uuid: str, validation_run_id: str):
        """Initializes the ValidationContext.

        Args:
            project_uuid (str): The UUID of the project.
            validation_run_id (str): The ID of the validation run.
        """
        self.project_uuid = project_uuid
        self.validation_run_id = validation_run_id
        self.filesystem = DataFileSystem(project_uuid)
        self.checks = []
        self.log = logger.get_logger()

    def __enter__(self):
        """Enter the context manager.

        Returns:
            ValidationContext: The instance itself.
        """
        self.filesystem.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager.

        Args:
            exc_type: The type of the exception.
            exc_val: The exception instance.
            exc_tb: The traceback object.
        """
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)

    def add_check(
        self, name: str, status: str, message: str = "", details: dict = None
    ):
        """Records a check result (OK, Warning, or Error).

        This will be saved to the database once the script finishes.

        Args:
            name (str): The name of the check.
            status (str): The status of the check (e.g., 'OK', 'Warning', 'Error').
            message (str): A descriptive message for the check result.
            details (dict, optional): Optional dictionary containing additional
                check details.
        """
        self.checks.append(
            {
                "name": name,
                "status": status,
                "message": message,
                "details": details or {},
            }
        )

    def get_data_path(self, relative_path: str = "") -> str:
        """Returns a URL or path that can be used to access the data.

        In streaming mode, this returns the presigned URL from the manifest.

        Args:
            relative_path (str): The path relative to the project's data root.

        Returns:
            str: The presigned URL or temporary directory path.
        """
        if not relative_path:
            return self.filesystem.temp_dir

        clean_path = relative_path.lstrip("/")
        if not self.filesystem.manifest:
            self.filesystem.build_manifest()

        return self.filesystem.manifest.get(clean_path, "")
