"""
Utility functions for S3 storage management and data processing.
Provides low-level wrappers around boto3 for common S3 operations
like listing, deleting, renaming, and statistics.
"""

from common.utils import (
    get_s3_client,
)
from django.conf import settings
from django.core.cache import cache


def create_minio_bucket(bucket_name):
    """
    Creates a new bucket in S3/Minio if it does not already exist.
    """
    s3 = get_s3_client()
    buckets = s3.list_buckets()
    if not any(b["Name"] == bucket_name for b in buckets["Buckets"]):
        s3.create_bucket(Bucket=bucket_name)


def list_s3_folder(prefix=""):
    """
    Lists immediate files and folders under the given prefix in S3.
    Uses Redis caching to accelerate UI navigation.

    Args:
        prefix (str): The S3 prefix to list.

    Returns:
        tuple: (folders, files) where each is a list of S3 keys.
    """
    cache_key = f"s3_listing:{prefix}"
    cached_result = cache.get(cache_key)
    if cached_result:
        return cached_result

    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    # Use '/' as delimiter to only get immediate children (not recursive)
    result = paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix, Delimiter="/"
    )

    folders = []
    files = []
    for page in result:
        # CommonPrefixes represent subdirectories
        for cp in page.get("CommonPrefixes", []):
            folders.append(cp["Prefix"])

        # Contents represent files
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Exclude the directory itself if it matches the prefix
            if key != prefix:
                files.append(key)

    res = (folders, files)
    # Cache for 2 minutes to keep UI snappy while reflecting changes reasonably fast
    cache.set(cache_key, res, 120)
    return res


def delete_s3_object(key):
    """
    Deletes a single object from S3.
    """
    s3 = get_s3_client()
    s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
    invalidate_s3_caches(key)


def copy_s3_object(source_key, target_key):
    """
    Copies an object from one key to another within the same bucket.
    """
    s3 = get_s3_client()
    s3.copy_object(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        CopySource={
            "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
            "Key": source_key,
        },
        Key=target_key,
    )


def rename_s3_object(old_key, new_key):
    """
    Renames an object by copying it to the new key and deleting the old one.
    """
    copy_s3_object(old_key, new_key)
    delete_s3_object(old_key)
    invalidate_s3_caches(new_key)


def delete_s3_folder(prefix):
    """
    Deletes all objects that start with the given prefix (effectively deleting a folder).
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    # We must find and delete every object recursively
    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
    ):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            s3.delete_objects(
                Bucket=settings.AWS_STORAGE_BUCKET_NAME,
                Delete={"Objects": objects},
            )
    invalidate_s3_caches(prefix)


def rename_s3_folder(old_prefix, new_prefix):
    """
    Renames a virtual folder by moving all contained objects to the new prefix.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=old_prefix
    ):
        for obj in page.get("Contents", []):
            old_key = obj["Key"]
            # Calculate the new key name based on the new prefix
            new_key = new_prefix + old_key[len(old_prefix):]
            copy_s3_object(old_key, new_key)
            delete_s3_object(old_key)
    invalidate_s3_caches(old_prefix)
    invalidate_s3_caches(new_prefix)


def get_storage_stats(prefix=""):
    """
    Calculates total size, file count, and folder count for a given S3 prefix.
    Uses Redis caching to avoid frequent recursive S3 listings.

    Returns:
        tuple: (total_size_bytes, folder_count, file_count)
    """
    cache_key = f"storage_stats:{prefix}"
    cached_stats = cache.get(cache_key)
    if cached_stats:
        return cached_stats

    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    total_size = 0
    file_count = 0
    folders = set()

    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            file_count += 1
            total_size += obj.get("Size", 0)

            # Identify parent folders by looking at the key segments
            key = obj["Key"]
            parts = key.split("/")

            # Add all parent directory paths to our set
            for i in range(1, len(parts)):
                folder_path = "/".join(parts[:i]) + "/"
                folders.add(folder_path)

    stats = (total_size, len(folders), file_count)
    # Cache for 10 minutes
    cache.set(cache_key, stats, 600)
    return stats


def get_column_prefixes(path):
    """
    Splits a path into a list of nested directory prefixes.
    Example: 'foo/bar/' -> ['', 'foo/', 'foo/bar/']
    Used for breadcrumbs and multi-column navigation.
    """
    if not path:
        return [""]

    parts = path.rstrip("/").split("/")
    prefixes = [""]
    for i in range(len(parts)):
        prefixes.append("/".join(parts[: i + 1]) + "/")
    return prefixes


def invalidate_s3_caches(path_key):
    """
    Clears s3_listing and storage_stats caches for the given key and its parents.
    Used after upload, delete, or rename operations.
    """
    # Extract project ID (first segment of the path)
    parts = path_key.rstrip("/").split("/")
    if not parts:
        return

    project_id = parts[0]
    root_data_prefix = f"{project_id}/data/"
    
    # Invalidate storage stats for the project root
    cache.delete(f"storage_stats:{root_data_prefix}")
    
    # Invalidate s3_listing for all parent directories
    # Get prefixes relative to the project root
    if len(path_key) > len(project_id) + 1:
        rel_path = path_key[len(project_id)+1:]
        prefixes = get_column_prefixes(rel_path)
        for p in prefixes:
            full_p = project_id + "/" + p
            cache.delete(f"s3_listing:{full_p}")
    else:
        # If it's just the project root
        cache.delete(f"s3_listing:{project_id}/")
        cache.delete(f"s3_listing:{project_id}")
