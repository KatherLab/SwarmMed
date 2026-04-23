"""Tests for shared common utilities."""

from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from common.utils import get_internal_s3_download_url


class InternalS3UrlTests(SimpleTestCase):
    """Regression tests for internal S3 manifest URL generation."""

    @patch("common.utils.boto3.client")
    def test_internal_url_honors_explicit_local_endpoint(self, mocked_boto_client):
        mocked_s3 = Mock()
        mocked_s3.generate_presigned_url.return_value = (
            "https://127.0.0.1:9100/medswarmhub/path/file.csv?sig=test"
        )
        mocked_boto_client.return_value = mocked_s3

        with patch.dict(
            "os.environ",
            {"MEDSWARMHUB_LOCAL_S3_ENDPOINT": "https://127.0.0.1:9100"},
            clear=False,
        ):
            url = get_internal_s3_download_url("path/file.csv", expires=60)

        self.assertIn("https://127.0.0.1:9100/medswarmhub/path/file.csv", url)
        self.assertIn("sig=test", url)
        mocked_boto_client.assert_called_once()
