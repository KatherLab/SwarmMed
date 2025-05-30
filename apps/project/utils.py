# utils.py
import json
import os
import re
import uuid
import shutil
from django.core.files.storage import default_storage
from django.conf import settings
from apps.users.models import Profile
from apps.logs import logger

def process_member_identifiers(project, member_identifiers):
    log = logger.get_logger()
    
    # Always clear existing members first if it's an existing project
    if project.pk:
        project.members.clear()
    
    # Only process new members if there are identifiers
    if member_identifiers:
        # Process the identifiers (split by commas or new lines)
        identifiers = re.split(r'[,\n]+', member_identifiers)
        
        for identifier_str in identifiers:
            identifier_str = identifier_str.strip()
            if not identifier_str:
                continue
                
            try:
                # Convert string to UUID to validate format
                identifier = uuid.UUID(identifier_str)
                
                # Find the profile with this UUID
                try:
                    profile = Profile.objects.get(identifier=identifier)
                    # Add the user to project members if not the author
                    if profile.user != project.author:
                        project.members.add(profile.user)
                except Profile.DoesNotExist:
                    log.project.warning(f"UUID {identifier} doesn't match any profile")
                    continue
                    
            except ValueError:
                log.project.warning(f"Invalid UUID format: {identifier_str}")
                continue

def handle_training_code_upload(project, request):
    """
    Process training code files for upload.
    
    Args:
        project: The project instance to associate files with
        request: The HTTP request containing files and directories info
    
    Returns:
        bool: True if files were uploaded, False otherwise
    """
    # Parse directories JSON
    training_code_directories_json = request.POST.get('training_code_directories', '{}')
    try:
        directories = json.loads(training_code_directories_json)
    except json.JSONDecodeError:
        directories = {}
    
    files = request.FILES.getlist('training_code')
    
    if directories and files:
        # Set training_code to None as we're handling files manually
        project.training_code = None
        
        # Clean up existing files if editing
        if project.pk:
            clean_folder_path(project.identifier, 'code/training/')
        
        # Set the root path and save files
        root_path = f"{project.identifier}/code/training/"
        
        for idx, file in enumerate(files):
            key = file.name + '_' + str(idx)
            rel_path = directories.get(key, file.name)
            save_path = os.path.join(root_path, rel_path).replace('\\', '/')
            default_storage.save(save_path, file)
        
        return True
    
    return False

def clean_folder_path(identifier, subfolder):
    """
    Clean up all files in a folder path.
    
    Args:
        identifier: Project identifier
        subfolder: Subfolder path
    """
    folder_path = f"{identifier}/{subfolder}"
    if hasattr(default_storage, 'bucket'):  # For S3
        prefix = folder_path
        s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
        s3_objects.delete()
    else:  # For local storage
        full_path = os.path.join(settings.MEDIA_ROOT, folder_path)
        if os.path.exists(full_path):
            shutil.rmtree(full_path)
            os.makedirs(full_path, exist_ok=True)
