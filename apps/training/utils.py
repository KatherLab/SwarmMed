"""
Utility functions for the training application.
Handles communication with S3 for downloading training code
and uploading results from the training workspace.
"""

import os

from common.utils import get_s3_client


def download_s3_folder(bucket_name, s3_folder, local_dir):
    """
    Recursively downloads all contents from a specific folder (prefix) in S3
    to a local directory. This is used to fetch the user's training code.

    Args:
        bucket_name (str): The name of the S3 bucket.
        s3_folder (str): The prefix/folder path in S3.
        local_dir (str): The local path where files will be saved.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    # Iterate through all objects in S3 that start with the given folder
    # prefix.
    for page in paginator.paginate(Bucket=bucket_name, Prefix=s3_folder):
        for obj in page.get("Contents", []):
            # Calculate the relative path from the S3 folder to the file.
            rel_path = os.path.relpath(obj["Key"], s3_folder)
            target = os.path.join(local_dir, rel_path)

            # Ensure the local subdirectory exists.
            if not os.path.exists(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))

            # If the object is a file (not a directory marker), download it.
            if not obj["Key"].endswith("/"):
                s3.download_file(bucket_name, obj["Key"], target)


def upload_file_to_s3(bucket_name, key, local_path):
    """
    Uploads a single file to S3.
    """
    s3 = get_s3_client()
    s3.upload_file(local_path, bucket_name, key)


def upload_folder_to_s3(bucket_name, local_dir, prefix):
    """
    Uploads all relevant files from a local directory to S3.
    Specifically used to sync training results (weights, logs) while
    ignoring code and hidden files.

    Args:
        bucket_name (str): Target S3 bucket.
        local_dir (str): Source local directory.
        prefix (str): Target prefix in S3.
    """
    for root, dirs, files in os.walk(local_dir):
        # Modify 'dirs' in-place to skip hidden directories during the walk.
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for filename in files:
            # Security & Efficiency: Skip hidden files and Python source/byte code.
            # We only want to upload data results (CSV, .pt, .npy, etc.)
            if (
                filename.startswith(".")
                or filename.endswith(".py")
                or filename.endswith(".pyc")
            ):
                continue

            full_path = os.path.join(root, filename)
            # Create a relative path to maintain the folder structure in S3.
            rel_path = os.path.relpath(full_path, local_dir)
            s3_key = f"{prefix.rstrip('/')}/{rel_path}"

            upload_file_to_s3(bucket_name, s3_key, full_path)
