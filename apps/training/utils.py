import boto3
from django.conf import settings
import os

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

def download_s3_folder(bucket_name, s3_folder, local_dir):
    """    Download the contents of a folder directory in S3.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket_name, Prefix=s3_folder):
        for obj in page.get('Contents', []):
            target = os.path.join(local_dir, os.path.relpath(obj['Key'], s3_folder))
            if not os.path.exists(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))
            if obj['Key'][-1] == '/':
                continue
            s3.download_file(bucket_name, obj['Key'], target)
