import boto3
from django.conf import settings
from botocore.exceptions import ClientError

def upload_file(file_obj):
    """
    Uploads a file object to MinIO while preserving folder structure.
    Returns a tuple:
        (True, key) on success, or (False, error_message) if an error occurs.
    """
    s3_client = boto3.client(
        's3',
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
    )

    bucket_name = settings.AWS_STORAGE_BUCKET_NAME

    # Check for the relative path provided by the browser in case of directory uploads.
    # Browsers may use either 'webkitRelativePath' or 'webkit_relative_path'.
    relative_path = getattr(file_obj, 'webkit_relative_path', None) or getattr(file_obj, 'webkitRelativePath', None)
    
    # Use the relative path if available (and non-empty), otherwise fall back to just the file name.
    key = relative_path if relative_path and relative_path != "" else file_obj.name

    try:
        s3_client.upload_fileobj(file_obj, bucket_name, key)
        return True, key
    except ClientError as e:
        return False, str(e)