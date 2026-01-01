"""
Utility functions for S3 storage management and data processing.
Provides low-level wrappers around boto3 for common S3 operations
like listing, deleting, renaming, and statistics.
"""

import boto3
from django.conf import settings


def get_s3_client():
    """
    Creates and returns an S3 client using the internal network credentials.
    This client uses the internal endpoint URL (useful for server-to-server).
    """
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.AWS_S3_ENDPOINT_URL
    )


def get_public_s3_client():
    """
    Creates and returns an S3 client using the public URL settings.
    This is used for generating presigned URLs that work in the user's browser.
    """
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.PUBLIC_URL
    )


def create_minio_bucket(bucket_name):
    """
    Creates a new bucket in S3/Minio if it does not already exist.
    """
    s3 = get_s3_client()
    buckets = s3.list_buckets()
    if not any(b['Name'] == bucket_name for b in buckets['Buckets']):
        s3.create_bucket(Bucket=bucket_name)


def list_s3_folder(prefix=""):
    """
    Lists immediate files and folders under the given prefix in S3.

    Args:
        prefix (str): The S3 prefix to list.

    Returns:
        tuple: (folders, files) where each is a list of S3 keys.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')

    # Use '/' as delimiter to only get immediate children (not recursive)
    result = paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=prefix,
        Delimiter='/'
    )

    folders = []
    files = []
    for page in result:
        # CommonPrefixes represent subdirectories
        for cp in page.get('CommonPrefixes', []):
            folders.append(cp['Prefix'])

        # Contents represent files
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Exclude the directory itself if it matches the prefix
            if key != prefix:
                files.append(key)

    return folders, files


def delete_s3_object(key):
    """
    Deletes a single object from S3.
    """
    s3 = get_s3_client()
    s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)


def copy_s3_object(source_key, target_key):
    """
    Copies an object from one key to another within the same bucket.
    """
    s3 = get_s3_client()
    s3.copy_object(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        CopySource={
            'Bucket': settings.AWS_STORAGE_BUCKET_NAME,
            'Key': source_key
        },
        Key=target_key
    )


def rename_s3_object(old_key, new_key):
    """
    Renames an object by copying it to the new key and deleting the old one.
    """
    copy_s3_object(old_key, new_key)
    delete_s3_object(old_key)


def delete_s3_folder(prefix):
    """
    Deletes all objects that start with the given prefix (effectively deleting a folder).
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')

    # We must find and delete every object recursively
    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=prefix
    ):
        objects = [{'Key': obj['Key']} for obj in page.get('Contents', [])]
        if objects:
            s3.delete_objects(
                Bucket=settings.AWS_STORAGE_BUCKET_NAME,
                Delete={'Objects': objects}
            )


def rename_s3_folder(old_prefix, new_prefix):
    """
    Renames a virtual folder by moving all contained objects to the new prefix.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')

    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=old_prefix
    ):
        for obj in page.get('Contents', []):
            old_key = obj['Key']
            # Calculate the new key name based on the new prefix
            new_key = new_prefix + old_key[len(old_prefix):]
            copy_s3_object(old_key, new_key)
            delete_s3_object(old_key)


def get_s3_download_url(key, expires=3600):
    """
    Generates a temporary presigned URL for downloading an S3 object.
    """
    s3 = get_public_s3_client()
    url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': key},
        ExpiresIn=expires
    )
    return url


def get_internal_s3_download_url(key, expires=3600):
    """
    Generates a temporary presigned URL for internal use within the Docker network.
    Ensures that the host in the URL is reachable from other containers (uses 'minio').
    """
    s3 = get_s3_client()
    url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': key},
        ExpiresIn=expires
    )
    
    # If the URL contains localhost or 127.0.0.1, other containers won't be able
    # to reach it. We replace it with the internal service name 'minio'.
    if "localhost" in url:
        url = url.replace("localhost", "minio")
    elif "127.0.0.1" in url:
        url = url.replace("127.0.0.1", "minio")
        
    return url


def get_storage_stats(prefix=""):
    """
    Calculates total size, file count, and folder count for a given S3 prefix.

    Returns:
        tuple: (total_size_bytes, folder_count, file_count)
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')

    total_size = 0
    file_count = 0
    folders = set()

    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=prefix
    ):
        for obj in page.get('Contents', []):
            file_count += 1
            total_size += obj.get('Size', 0)

            # Identify parent folders by looking at the key segments
            key = obj['Key']
            parts = key.split('/')

            # Add all parent directory paths to our set
            for i in range(1, len(parts)):
                folder_path = '/'.join(parts[:i]) + '/'
                folders.add(folder_path)

    return total_size, len(folders), file_count


def format_size(size_bytes):
    """
    Converts a number of bytes into a human-readable string (e.g., '1.2 MB').
    """
    if size_bytes == 0:
        return '0 B'

    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = size_bytes
    unit_index = 0

    while size > 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index > 0:
        return f"{size:.1f} {units[unit_index]}"
    return f"{size} {units[unit_index]}"


def get_column_prefixes(path):
    """
    Splits a path into a list of nested directory prefixes.
    Example: 'foo/bar/' -> ['', 'foo/', 'foo/bar/']
    Used for breadcrumbs and multi-column navigation.
    """
    if not path:
        return [""]

    parts = path.rstrip('/').split('/')
    prefixes = [""]
    for i in range(len(parts)):
        prefixes.append('/'.join(parts[:i + 1]) + '/')
    return prefixes
