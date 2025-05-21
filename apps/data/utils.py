import boto3
from django.conf import settings

def get_s3_client():
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.AWS_S3_ENDPOINT_URL
    )

def list_s3_folder(prefix=""):
    """
    List immediate files and folders under the given prefix.
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
    s3 = get_s3_client()
    s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)

def rename_s3_object(old_key, new_key):
    s3 = get_s3_client()
    s3.copy_object(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        CopySource={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': old_key},
        Key=new_key
    )
    s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=old_key)
    
def make_public_presigned_url(url):
    internal = settings.AWS_S3_ENDPOINT_URL
    public = settings.PUBLIC_URL
    return url.replace(internal, public)

def get_s3_download_url(key, expires=3600):
    s3 = get_s3_client()
    url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.AWS_STORAGE_BUCKET_NAME, 'Key': key},
        ExpiresIn=expires
    )
    return make_public_presigned_url(url)
