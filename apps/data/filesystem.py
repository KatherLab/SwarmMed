"""
Data FileSystem Utilities.
Provides a virtual filesystem layer that bridges Django's S3 storage
with local script execution, handling on-demand downloads and cleanup.
"""

import os
import tempfile
import shutil
import concurrent.futures
from typing import Dict, List

from django.core.files.storage import default_storage
from logs import logger
from .utils import list_s3_folder


class DataFileSystem:
    """
    A virtual filesystem that provides file-like access to data stored in S3.
    It downloads files on-demand to a local temporary directory so that
    libraries like Pandas can read them as standard local files.
    """

    def __init__(self, project_uuid: str):
        """
        Initialize the filesystem for a specific project.
        """
        from django.conf import settings

        self.project_uuid = project_uuid
        # The base path in S3 for this project's data
        self.root_path = f"{project_uuid}/data/"
        # Use a project-local temporary directory so it can be mounted by Docker
        self.temp_dir = tempfile.mkdtemp(
            prefix=f"validation_{project_uuid}_", dir=settings.PROJECT_TEMP_DIR
        )
        # Cache of files already downloaded to avoid redundant network calls
        self._downloaded_files: Dict[str, str] = {}
        self.log = logger.get_logger()

    def __enter__(self):
        """Allows usage as a context manager: 'with DataFileSystem(...) as fs:'"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Automatically cleans up temporary files when the context finishes."""
        if exc_type:
            self.log.data.error(
                f"DataFileSystem context exited with error: {str(exc_val)}"
            )
        self.cleanup()

    def cleanup(self):
        """
        Removes the local temporary directory and all downloaded files.
        """
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _ensure_file_downloaded(self, relative_path: str) -> str:
        """
        Checks if a file exists locally; if not, downloads it from S3.
        Returns the absolute local path to the file.
        """
        # Security: Sanitize path to prevent traversal
        # 1. Remove leading slashes and redundant dots
        clean_rel_path = os.path.normpath(relative_path).lstrip(
            os.path.sep + (os.path.altsep or "")
        )

        # 2. Prevent escaping the temp directory
        if clean_rel_path.startswith("..") or os.path.isabs(clean_rel_path):
            self.log.data.warning(
                f"Blocked path traversal attempt in DataFileSystem: {relative_path}"
            )
            raise ValueError(f"Invalid relative path: {relative_path}")

        if clean_rel_path in self._downloaded_files:
            # HIPAA Compliance: Log access to specific PHI file even on cache hit
            self.log.access.info(f"Accessed PHI file (cached): {clean_rel_path}")
            return self._downloaded_files[clean_rel_path]

        # The full key in S3
        s3_key = f"{self.root_path}{clean_rel_path}"

        # The full path on the local machine
        local_path = os.path.join(self.temp_dir, clean_rel_path)

        # Ensure the local subdirectories exist
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        # Download the file from S3 to the local path
        try:
            with default_storage.open(s3_key, "rb") as s3_file:
                with open(local_path, "wb") as local_file:
                    # Use a larger buffer to improve throughput for large datasets.
                    shutil.copyfileobj(s3_file, local_file, length=1024 * 1024)

            self._downloaded_files[clean_rel_path] = local_path

            # HIPAA Compliance: Log access to specific PHI file
            self.log.access.info(f"Accessed PHI file (downloaded): {clean_rel_path}")

            return local_path
        except Exception as e:
            self.log.data.error(f"Failed to download file {clean_rel_path}: {str(e)}")
            raise FileNotFoundError(
                f"Could not download file {clean_rel_path}: {str(e)}"
            )

    def open(self, relative_path: str, mode: str = "r", **kwargs):
        """
        Opens a file from S3 as if it were local.
        Downloads the file first if necessary.
        """
        local_path = self._ensure_file_downloaded(relative_path)
        return open(local_path, mode, **kwargs)

    def exists(self, relative_path: str) -> bool:
        """
        Checks if a specific file exists in the project's S3 data directory.
        """
        s3_key = f"{self.root_path}{relative_path}"
        return default_storage.exists(s3_key)

    def listdir(self, relative_path: str = "") -> List[str]:
        """
        Lists files and subdirectories in the given relative path.
        Returns names ending in '/' for directories.
        """
        prefix = f"{self.root_path}{relative_path}"
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        folders, files = list_s3_folder(prefix)

        items = []

        # Add subfolders, removing the long S3 prefix for the user
        for folder in folders:
            folder_name = folder[len(prefix) :].rstrip("/")
            if folder_name:
                items.append(folder_name + "/")

        # Add filenames, removing the long S3 prefix
        for file in files:
            file_name = file[len(prefix) :]
            if file_name:
                items.append(file_name)

        return items

    def get_path(self, relative_path: str) -> str:
        """
        Returns the local filesystem path for a file.
        Forces a download if the file isn't local yet.
        """
        return self._ensure_file_downloaded(relative_path)

    def download_all(self):
        """
        Recursively downloads ALL files from the project's S3 data directory
        to the local temporary directory using parallel threads.
        """
        from django.conf import settings
        from common.utils import get_s3_client

        s3 = get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")

        self.log.data.info(
            f"Starting full data download for project {self.project_uuid}..."
        )

        # 1. Collect all files to download first
        files_to_download = []
        for page in paginator.paginate(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=self.root_path
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue

                # Check for hidden files or directories (e.g. .DS_Store, .ipynb_checkpoints)
                # We check if any segment of the path starts with '.'
                if any(part.startswith(".") for part in key.split("/")):
                    continue

                # Calculate relative path within the data directory
                rel_path = key[len(self.root_path) :]
                files_to_download.append(rel_path)

        # 2. Download in parallel
        count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Map the download function to the files
            future_to_file = {
                executor.submit(self._ensure_file_downloaded, rel_path): rel_path
                for rel_path in files_to_download
            }

            for future in concurrent.futures.as_completed(future_to_file):
                rel_path = future_to_file[future]
                try:
                    future.result()
                    count += 1
                except Exception as e:
                    self.log.data.warning(
                        f"Failed to download {rel_path} during sync: {e}"
                    )

        self.log.data.info(f"Full download complete. Synced {count} files.")


class ValidationContext:
    """
    A helper class provided to data validation scripts.
    It encapsulates the filesystem access and check reporting logic.
    """

    def __init__(self, project_uuid: str, validation_run_id: str):
        self.project_uuid = project_uuid
        self.validation_run_id = validation_run_id
        self.filesystem = DataFileSystem(project_uuid)
        self.checks = []
        self.log = logger.get_logger()

    def __enter__(self):
        self.filesystem.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)

    def add_check(
        self, name: str, status: str, message: str = "", details: dict = None
    ):
        """
        Records a check result (OK, Warning, or Error).
        This will be saved to the database once the script finishes.
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
        """
        Returns a local path that libraries like Pandas can use directly.
        If no path is provided, returns the root temporary directory.
        """
        if relative_path:
            return self.filesystem.get_path(relative_path)
        else:
            return self.filesystem.temp_dir
