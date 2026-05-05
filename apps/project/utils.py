"""Utility functions for the project application.
Handles dynamic path generation for file uploads, processing of project members,
and manual file management for complex directory uploads.
"""

import json
import os
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


def handle_training_code_upload(project, request):
    """Handles complex multi-file uploads preserving directory structure.

    Args:
        project (Project): The project instance.
        request (HttpRequest): The request object containing files and directory mapping.

    Returns:
        bool: True if files were processed and saved, False otherwise.
    """
    # 'training_code_directories' is a JSON string mapping file keys to relative paths.
    training_code_directories_json = request.POST.get(
        "training_code_directories", "{}"
    )
    try:
        directories = json.loads(training_code_directories_json)
    except json.JSONDecodeError:
        directories = {}

    # Get the list of all files uploaded via the 'training_code' input field.
    files = request.FILES.getlist("training_code")

    if files and (directories or len(files) > 1):
        python_files = [
            (idx, file_obj)
            for idx, file_obj in enumerate(files)
            if file_obj.name.lower().endswith(".py")
        ]
        if not python_files:
            return True

        if project.training_code:
            project.training_code.delete(save=False)
        # We handle storage manually for these files, but the model field still
        # needs to point at a representative file so the Hub can detect that
        # training code exists. Prefer a training.py file, falling back to the
        # first saved Python file for legacy projects.
        project.training_code = None

        # If we are updating an existing project, remove old training files
        # first.
        if project.pk:
            clean_folder_path(project.identifier, "code/training/")

        # Define the base storage path for this project's training code.
        root_path = f"{project.identifier}/code/training/"
        first_saved_path = None
        preferred_training_path = None

        for idx, file_obj in python_files:
            # The frontend generates a key using the filename and index.
            key = f"{file_obj.name}_{idx}"

            # Look up the relative path (e.g., 'utils/helper.py') from the mapping.
            # Default to the filename if not found.
            rel_path = directories.get(key, file_obj.name)

            # Security: prevent path traversal and absolute paths.
            clean_rel_path = os.path.normpath(rel_path).lstrip(
                os.path.sep + (os.path.altsep or "")
            )
            if (
                not clean_rel_path
                or clean_rel_path == "."
                or clean_rel_path.startswith("..")
                or os.path.isabs(clean_rel_path)
            ):
                continue

            # Combine the root path with the relative path, ensuring forward
            # slashes.
            save_path = os.path.join(root_path, clean_rel_path).replace(
                "\\", "/"
            )

            # Save the file to the configured storage (Local or S3).
            actual_path = default_storage.save(save_path, file_obj)
            if first_saved_path is None:
                first_saved_path = actual_path
            if os.path.basename(clean_rel_path) == "training.py":
                if clean_rel_path == "training.py":
                    preferred_training_path = actual_path
                elif preferred_training_path is None:
                    preferred_training_path = actual_path

        marker_path = preferred_training_path or first_saved_path
        if marker_path:
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
