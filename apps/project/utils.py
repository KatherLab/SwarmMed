"""Utility functions for the project application.
Handles dynamic path generation for file uploads, processing of project members,
and manual file management for complex directory uploads.
"""

import json
import os
import posixpath
import re
import shutil
import uuid

from django.conf import settings
from django.core.files.storage import default_storage

from users.models import Profile


def get_upload_path(instance, filename, subfolder):
    """Generates a standardized storage path for project-related files.

    The path structure is: <project-uuid>/<subfolder>/<filename>

    Args:
        instance (Project): The project model instance.
        filename (str): The original name of the file.
        subfolder (str): The subfolder name (e.g., 'code/training').

    Returns:
        str: The generated storage path.
    """
    return os.path.join(str(instance.identifier), subfolder, filename)


def training_code_path(instance, filename):
    """Specific path generator for training code scripts.

    Args:
        instance (Project): The project model instance.
        filename (str): The original name of the file.

    Returns:
        str: The storage path for training code.
    """
    return get_upload_path(instance, filename, "code/training")


def requirements_path(instance, filename):
    """Specific path generator for requirements.txt files.

    Args:
        instance (Project): The project model instance.
        filename (str): The original name of the file.

    Returns:
        str: The storage path for requirements.
    """
    return get_upload_path(instance, filename, "code/requirements")


def data_validation_path(instance, filename):
    """Specific path generator for data validation scripts.

    Args:
        instance (Project): The project model instance.
        filename (str): The original name of the file.

    Returns:
        str: The storage path for data validation scripts.
    """
    return get_upload_path(instance, filename, "code/data_validation")


def data_visualization_path(instance, filename):
    """Specific path generator for dataset visualization scripts.

    Args:
        instance (Project): The project model instance.
        filename (str): The original name of the file.

    Returns:
        str: The storage path for data visualization scripts.
    """
    return get_upload_path(instance, filename, "code/data_visualization")


def results_visualization_path(instance, filename):
    """Specific path generator for results visualization scripts.

    Args:
        instance (Project): The project model instance.
        filename (str): The original name of the file.

    Returns:
        str: The storage path for results visualization scripts.
    """
    return get_upload_path(instance, filename, "code/results_visualization")


def explainability_path(instance, filename):
    """Specific path generator for explainability scripts.

    Args:
        instance (Project): The project model instance.
        filename (str): The original name of the file.

    Returns:
        str: The storage path for explainability scripts.
    """
    return get_upload_path(instance, filename, "code/explainability")


def process_member_identifiers(project, member_identifiers):
    """Parses a string of UUIDs and adds the corresponding users to the project.

    Args:
        project (Project): The project instance to update.
        member_identifiers (str): A string containing UUIDs separated by commas or newlines.
    """
    # Import logger inside the function to avoid circular import issues.
    from logs import logger

    log = logger.get_logger()

    # Clear all existing members before re-adding them from the provided list.
    # This handles both additions and removals during an update.
    if project.pk:
        project.members.clear()

    if not member_identifiers:
        return

    # Split the input string into a list of individual identifier strings.
    identifiers = re.split(r"[,\n]+", member_identifiers)

    for identifier_str in identifiers:
        identifier_str = identifier_str.strip()
        if not identifier_str:
            continue

        try:
            # Validate that the string is a properly formatted UUID.
            identifier_uuid = uuid.UUID(identifier_str)

            # Attempt to find the user profile associated with this UUID.
            try:
                profile = Profile.objects.get(identifier=identifier_uuid)
                # Ensure we don't add the author as a member (they own the
                # project).
                if profile.user != project.author:
                    project.members.add(profile.user)
            except Profile.DoesNotExist:
                log.project.warning(
                    f"Profile not found for UUID: {identifier_uuid}"
                )

        except ValueError:
            # Log a warning if the string isn't a valid UUID.
            log.project.warning(
                f"Invalid UUID format provided: {identifier_str}"
            )


MISSING_ROOT_TRAINING_MESSAGE = (
    "The training code must contain a root-level training.py file. Select the "
    "folder that contains training.py, or select training.py and the modules "
    "it imports as individual files."
)


def _training_upload(post, files):
    """Return the uploaded training-code files and their relative-path map.

    Args:
        post: The submitted form data (``request.POST``).
        files: The uploaded files (``request.FILES``).

    Returns:
        tuple: The files of the 'training_code' field and the mapping from
        '<file name>_<index>' to the file's path inside the upload.
    """
    # 'training_code_directories' is a JSON string mapping file keys to
    # relative paths.
    try:
        directories = json.loads(post.get("training_code_directories") or "{}")
    except json.JSONDecodeError:
        directories = {}
    if not isinstance(directories, dict):
        directories = {}
    uploaded = files.getlist("training_code") if hasattr(files, "getlist") else []
    return uploaded, directories


def _training_upload_paths(uploaded, directories):
    """Return (file, relative path) for each uploaded Python file to store.

    Paths that are empty, absolute or outside the upload are dropped. A
    browser's "Select project folder" gives paths that start with the selected
    folder's own name; when every path starts with the same folder, that folder
    is removed so that its training.py is stored at the root.
    """
    entries = []
    for idx, file_obj in enumerate(uploaded):
        if not file_obj.name.lower().endswith(".py"):
            continue
        # The frontend keys each path by the file name and its index.
        rel_path = directories.get(f"{file_obj.name}_{idx}", file_obj.name)
        if not isinstance(rel_path, str):
            continue
        # Security: prevent path traversal and absolute paths.
        clean_rel_path = posixpath.normpath(rel_path.replace("\\", "/")).lstrip("/")
        if (
            not clean_rel_path
            or clean_rel_path == "."
            or clean_rel_path.startswith("..")
        ):
            continue
        entries.append((file_obj, clean_rel_path))

    top_folders = {path.split("/", 1)[0] for _, path in entries if "/" in path}
    if entries and len(top_folders) == 1 and all("/" in p for _, p in entries):
        entries = [(file_obj, path.split("/", 1)[1]) for file_obj, path in entries]
    return entries


def training_code_upload_error(post, files):
    """Return why an uploaded training code cannot be used, or None.

    Python files were uploaded, but none of them is a root-level training.py
    (after removing a folder that holds every file). Training runs
    custom/training.py, so such code could never be trained.
    """
    entries = _training_upload_paths(*_training_upload(post, files))
    if entries and not any(path == "training.py" for _, path in entries):
        return MISSING_ROOT_TRAINING_MESSAGE
    return None


def handle_training_code_upload(project, request):
    """Handles complex multi-file uploads preserving directory structure.

    Args:
        project (Project): The project instance.
        request (HttpRequest): The request object containing files and directory mapping.

    Returns:
        bool: True if files were processed and saved, False otherwise.

    Raises:
        ValueError: Python files were uploaded without a root-level training.py.
            Nothing is changed in that case.
    """
    files, directories = _training_upload(request.POST, request.FILES)

    if files and (directories or len(files) > 1):
        entries = _training_upload_paths(files, directories)
        if not entries:
            return True
        if not any(path == "training.py" for _, path in entries):
            raise ValueError(MISSING_ROOT_TRAINING_MESSAGE)

        if project.training_code:
            project.training_code.delete(save=False)
        # We handle storage manually for these files, but the model field still
        # needs to point at the root-level training.py so the Hub can detect
        # that training code exists.
        project.training_code = None

        # If we are updating an existing project, remove old training files
        # first.
        if project.pk:
            clean_folder_path(project.identifier, "code/training/")

        # Define the base storage path for this project's training code.
        root_path = f"{project.identifier}/code/training/"
        marker_path = None
        for file_obj, rel_path in entries:
            # Save the file to the configured storage (Local or S3).
            actual_path = default_storage.save(root_path + rel_path, file_obj)
            if rel_path == "training.py" and marker_path is None:
                marker_path = actual_path

        project.training_code.name = marker_path
        return True

    return False


def clean_folder_path(identifier, subfolder):
    """Deletes all files within a specific subfolder of a project.

    Args:
        identifier (uuid): Project's unique identifier.
        subfolder (str): The relative path of the folder to clean.
    """
    folder_path = f"{identifier}/{subfolder}"

    # Check if we're using S3 storage.
    if hasattr(default_storage, "bucket"):
        # In S3, we delete objects by their prefix.
        prefix = folder_path
        s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
        s3_objects.delete()
    else:
        # In local storage, we delete the directory and recreate it.
        full_path = os.path.join(settings.MEDIA_ROOT, folder_path)
        if os.path.exists(full_path):
            shutil.rmtree(full_path)
            os.makedirs(full_path, exist_ok=True)
