import boto3
from django.conf import settings

def get_s3_client():
    """
    Create and return an S3 client using settings credentials.
    """
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.AWS_S3_ENDPOINT_URL
    )

def create_minio_bucket(bucket_name):
    """
    Create a new bucket if it doesn't exist.
    
    Args:
        bucket_name (str): Name of the bucket to create
    """
    s3 = get_s3_client()
    # Check if bucket exists
    buckets = s3.list_buckets()
    if not any(b['Name'] == bucket_name for b in buckets['Buckets']):
        s3.create_bucket(Bucket=bucket_name)

def list_s3_folder(prefix=""):
    """
    List immediate files and folders under the given prefix.
    
    Args:
        prefix (str): S3 prefix to list
        
    Returns:
        tuple: (folders, files) where each is a list of keys
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    result = paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=prefix,
        Delimiter='/'
    )

    folders = []
    files = []
    for page in result:
        # Subfolders
        for cp in page.get('CommonPrefixes', []):
            folders.append(cp['Prefix'])
        # Files
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key != prefix:  # Exclude the folder itself
                files.append(key)
    return folders, files

def delete_s3_object(key):
    """
    Delete a single S3 object.
    
    Args:
        key (str): S3 key to delete
    """
    s3 = get_s3_client()
    s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)

def copy_s3_object(source_key, target_key):
    """
    Copy an S3 object from source to target.
    
    Args:
        source_key (str): Source S3 key
        target_key (str): Target S3 key
    """
    s3 = get_s3_client()
    s3.copy_object(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        CopySource={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': source_key},
        Key=target_key
    )

def rename_s3_object(old_key, new_key):
    """
    Rename an S3 object by copying and deleting.
    
    Args:
        old_key (str): Current key
        new_key (str): New key
    """
    copy_s3_object(old_key, new_key)
    delete_s3_object(old_key)

def delete_s3_folder(prefix):
    """
    Delete all objects under the given prefix (i.e., a folder).
    
    Args:
        prefix (str): Folder prefix to delete
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix):
        objects = [{'Key': obj['Key']} for obj in page.get('Contents', [])]
        if objects:
            s3.delete_objects(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Delete={'Objects': objects})

def rename_s3_folder(old_prefix, new_prefix):
    """
    Rename a folder by copying all objects to new prefix and deleting the old ones.
    
    Args:
        old_prefix (str): Current folder prefix
        new_prefix (str): New folder prefix
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=old_prefix):
        for obj in page.get('Contents', []):
            old_key = obj['Key']
            new_key = new_prefix + old_key[len(old_prefix):]
            copy_s3_object(old_key, new_key)
            delete_s3_object(old_key)
    
def make_public_presigned_url(url):
    """
    Convert internal URLs to public-facing URLs.
    
    Args:
        url (str): Internal URL
    
    Returns:
        str: Public URL
    """
    internal = settings.AWS_S3_ENDPOINT_URL
    public = settings.PUBLIC_URL
    return url.replace(internal, public)

def get_s3_download_url(key, expires=3600):
    """
    Generate a presigned URL for downloading an S3 object.
    
    Args:
        key (str): S3 key
        expires (int): URL expiration in seconds
        
    Returns:
        str: Presigned download URL
    """
    s3 = get_s3_client()
    url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': key},
        ExpiresIn=expires
    )
    return make_public_presigned_url(url)

def get_storage_stats(prefix=""):
    """
    Get storage statistics for a given prefix.
    
    Args:
        prefix (str): S3 prefix to analyze
        
    Returns:
        tuple: (total_size, folder_count, file_count)
            - total_size: Total size in bytes
            - folder_count: Number of unique folders
            - file_count: Number of files
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    
    total_size = 0
    file_count = 0
    folders = set()
    
    for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix):
        for obj in page.get('Contents', []):
            # Count this as a file
            file_count += 1
            
            # Add its size to the total
            total_size += obj.get('Size', 0)
            
            # Extract the folder path from the key
            key = obj['Key']
            parts = key.split('/')
            
            # Add all parent folders to the set
            for i in range(1, len(parts)):
                folder_path = '/'.join(parts[:i]) + '/'
                folders.add(folder_path)
    
    folder_count = len(folders)
    
    return total_size, folder_count, file_count

def format_size(size_bytes):
    """
    Convert bytes to human-readable format.
    
    Args:
        size_bytes (int): Size in bytes
        
    Returns:
        str: Formatted size string (e.g., "1.5 MB")
    """
    if size_bytes == 0:
        return '0 B'
        
    # Define unit prefixes
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    # Start with bytes
    size = size_bytes
    unit_index = 0
    
    # Keep dividing by 1024 until we get a reasonable number
    while size > 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    
    # Format with one decimal place if not bytes
    if unit_index > 0:
        return f"{size:.1f} {units[unit_index]}"
    else:
        return f"{size} {units[unit_index]}"
