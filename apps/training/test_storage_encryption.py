"""Direct object-storage writes request the same encryption as django-storages.

Training results are uploaded and data files are copied with plain boto3
calls instead of the storage backend, so they must pass
settings.AWS_S3_OBJECT_PARAMETERS themselves.
"""

import os
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from data import utils as data_utils

from . import utils as training_utils

KMS_PARAMETERS = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "hub-key"}


class ResultUploadEncryptionTests(SimpleTestCase):
    """Result uploads carry the configured server-side encryption."""

    def test_upload_requests_sse_s3(self):
        with patch.object(training_utils, "get_s3_client") as get_client:
            training_utils.upload_file_to_s3(
                "bucket", "p/results/job/site/best.pt", "/tmp/best.pt"
            )

        upload = get_client.return_value.upload_file
        upload.assert_called_once()
        self.assertEqual(
            upload.call_args.args,
            ("/tmp/best.pt", "bucket", "p/results/job/site/best.pt"),
        )
        extra_args = upload.call_args.kwargs.get("ExtraArgs") or {}
        self.assertEqual(extra_args.get("ServerSideEncryption"), "AES256")

    @override_settings(AWS_S3_OBJECT_PARAMETERS=KMS_PARAMETERS)
    def test_upload_takes_parameters_from_settings(self):
        with patch.object(training_utils, "get_s3_client") as get_client:
            training_utils.upload_file_to_s3("bucket", "key", "/tmp/best.pt")

        upload = get_client.return_value.upload_file
        self.assertEqual(upload.call_args.kwargs.get("ExtraArgs"), KMS_PARAMETERS)

    @override_settings(AWS_S3_OBJECT_PARAMETERS={"ServerSideEncryption": "AES256"})
    def test_upload_does_not_hand_out_the_settings_dict(self):
        # s3transfer adds defaults to ExtraArgs in place; they must not leak
        # into the parameters django-storages uses for every other write.
        def add_default(*args, ExtraArgs=None, **kwargs):
            if ExtraArgs is not None:
                ExtraArgs["ChecksumAlgorithm"] = "CRC32"

        with patch.object(training_utils, "get_s3_client") as get_client:
            get_client.return_value.upload_file.side_effect = add_default
            training_utils.upload_file_to_s3("bucket", "key", "/tmp/best.pt")

        self.assertEqual(
            settings.AWS_S3_OBJECT_PARAMETERS, {"ServerSideEncryption": "AES256"}
        )

    def test_folder_upload_encrypts_every_result_file(self):
        with tempfile.TemporaryDirectory() as local_dir:
            os.makedirs(os.path.join(local_dir, "models"))
            for name in ("models/best.pt", "log.txt", "train.py"):
                with open(os.path.join(local_dir, name), "w") as handle:
                    handle.write("x")
            with patch.object(training_utils, "get_s3_client") as get_client:
                training_utils.upload_folder_to_s3(
                    "bucket", local_dir, "p/results/job/site"
                )

        uploads = get_client.return_value.upload_file.call_args_list
        self.assertEqual(
            sorted(call.args[2] for call in uploads),
            ["p/results/job/site/log.txt", "p/results/job/site/models/best.pt"],
        )
        for call in uploads:
            extra_args = call.kwargs.get("ExtraArgs") or {}
            self.assertEqual(extra_args.get("ServerSideEncryption"), "AES256")


class DataStorageTestCase(SimpleTestCase):
    """Runs data.utils against a mocked S3 client and cache."""

    def setUp(self):
        client_patch = patch.object(data_utils, "get_s3_client")
        self.s3 = client_patch.start().return_value
        self.addCleanup(client_patch.stop)
        # invalidate_s3_caches would otherwise reach the configured Redis.
        cache_patch = patch.object(data_utils, "cache")
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def list_objects(self, *keys):
        self.s3.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": key} for key in keys]}
        ]


class DataCopyEncryptionTests(DataStorageTestCase):
    """Copies made when renaming data carry the configured encryption."""

    def test_copy_requests_sse_s3(self):
        data_utils.copy_s3_object("p/data/a.csv", "p/data/b.csv")

        self.s3.copy_object.assert_called_once()
        kwargs = self.s3.copy_object.call_args.kwargs
        self.assertEqual(kwargs["Key"], "p/data/b.csv")
        self.assertEqual(kwargs.get("ServerSideEncryption"), "AES256")

    @override_settings(AWS_S3_OBJECT_PARAMETERS=KMS_PARAMETERS)
    def test_copy_takes_parameters_from_settings(self):
        data_utils.copy_s3_object("p/data/a.csv", "p/data/b.csv")

        bucket = settings.AWS_STORAGE_BUCKET_NAME
        self.s3.copy_object.assert_called_once_with(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": "p/data/a.csv"},
            Key="p/data/b.csv",
            **KMS_PARAMETERS,
        )

    def test_folder_rename_encrypts_every_copy(self):
        self.list_objects("p/data/old/a.csv", "p/data/old/sub/b.csv")

        data_utils.rename_s3_folder("p/data/old/", "p/data/new/")

        copies = self.s3.copy_object.call_args_list
        self.assertEqual(
            [call.kwargs["Key"] for call in copies],
            ["p/data/new/a.csv", "p/data/new/sub/b.csv"],
        )
        for call in copies:
            self.assertEqual(call.kwargs.get("ServerSideEncryption"), "AES256")
        self.assertEqual(self.s3.delete_object.call_count, 2)


class SameKeyRenameTests(DataStorageTestCase):
    """Renaming data to its current name leaves it in place.

    S3 rejects copying an object onto itself unless something changes, so an
    unencrypted self-copy failed before the source was deleted. A copy that
    requests encryption counts as a change and succeeds, so the rename must
    stop before it deletes the only copy. The rename dialog is pre-filled with
    the current name, which makes this an easy accident.
    """

    def test_file_renamed_to_itself_is_kept(self):
        data_utils.rename_s3_object("p/data/a.csv", "p/data/a.csv")

        self.s3.copy_object.assert_not_called()
        self.s3.delete_object.assert_not_called()

    def test_folder_renamed_to_itself_is_kept(self):
        self.list_objects("p/data/old/a.csv", "p/data/old/sub/b.csv")

        data_utils.rename_s3_folder("p/data/old/", "p/data/old/")

        self.s3.copy_object.assert_not_called()
        self.s3.delete_object.assert_not_called()
