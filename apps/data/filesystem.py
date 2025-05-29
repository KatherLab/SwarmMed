import os
import tempfile
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from django.core.files.storage import default_storage
from .utils import list_s3_folder

class DataFileSystem:
    """
    Virtual filesystem that provides file-like access to S3 data.
    Downloads files on-demand and manages temporary storage.
    """
    
    def __init__(self, project_uuid: str):
        self.project_uuid = project_uuid
        self.root_path = f"{project_uuid}/data/"
        self.temp_dir = tempfile.mkdtemp(prefix=f"validation_{project_uuid}_")
        self._downloaded_files: Dict[str, str] = {}
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
    
    def cleanup(self):
        """Clean up temporary directory"""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def _ensure_file_downloaded(self, relative_path: str) -> str:
        """
        Ensure a file is downloaded to local temp storage.
        Returns the local path to the file.
        """
        if relative_path in self._downloaded_files:
            return self._downloaded_files[relative_path]
        
        # S3 key for the file
        s3_key = f"{self.root_path}{relative_path}"
        
        # Local path in temp directory
        local_path = os.path.join(self.temp_dir, relative_path)
        
        # Create directory structure
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        
        # Download file from S3
        try:
            with default_storage.open(s3_key, 'rb') as s3_file:
                with open(local_path, 'wb') as local_file:
                    shutil.copyfileobj(s3_file, local_file)
            
            self._downloaded_files[relative_path] = local_path
            return local_path
        except Exception as e:
            raise FileNotFoundError(f"Could not download file {relative_path}: {str(e)}")
    
    def open(self, relative_path: str, mode: str = 'r', **kwargs):
        """Open a file with the given mode."""
        local_path = self._ensure_file_downloaded(relative_path)
        return open(local_path, mode, **kwargs)
    
    def exists(self, relative_path: str) -> bool:
        """Check if a file exists in the data directory."""
        s3_key = f"{self.root_path}{relative_path}"
        return default_storage.exists(s3_key)
    
    def listdir(self, relative_path: str = "") -> List[str]:
        """List files and directories in the given path."""
        prefix = f"{self.root_path}{relative_path}"
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        
        folders, files = list_s3_folder(prefix)
        
        # Process results to return relative names
        items = []
        
        # Add folders
        for folder in folders:
            folder_name = folder[len(prefix):].rstrip('/')
            if folder_name:
                items.append(folder_name + '/')
        
        # Add files
        for file in files:
            file_name = file[len(prefix):]
            if file_name:
                items.append(file_name)
        
        return items
    
    def get_path(self, relative_path: str) -> str:
        """Get the local filesystem path for a file (downloads if needed)."""
        return self._ensure_file_downloaded(relative_path)

class ValidationContext:
    """Context providing access to data and utilities for validation scripts."""
    
    def __init__(self, project_uuid: str, validation_run_id: str):
        self.project_uuid = project_uuid
        self.validation_run_id = validation_run_id
        self.filesystem = DataFileSystem(project_uuid)
        self.checks = []
    
    def __enter__(self):
        self.filesystem.__enter__()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)
    
    def add_check(self, name: str, status: str, message: str = "", details: dict = None):
        """Add a validation check result."""
        self.checks.append({
            'name': name,
            'status': status,
            'message': message,
            'details': details or {}
        })
    
    def get_data_path(self, relative_path: str = "") -> str:
        """Get local filesystem path to data (for pandas, etc.)."""
        if relative_path:
            return self.filesystem.get_path(relative_path)
        else:
            return self.filesystem.temp_dir
