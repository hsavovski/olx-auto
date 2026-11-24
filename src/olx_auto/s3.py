"""Private S3 bucket that serves listing photos to OLX during a submit (spec §9).

OLX copies the images to its own CDN during the create call, so an object lives for seconds:
`submitting` puts the photos, passes presigned GET URLs to OLX, and deletes them right after.
"""
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings

URL_EXPIRY = 3600  # seconds; the create call needs the URLs for a few seconds only
S3Error = (BotoCoreError, ClientError)


class PhotoStore:
    def __init__(self, settings: Settings):
        if not (settings.s3_bucket and settings.aws_access_key_id and settings.aws_secret_access_key):
            raise RuntimeError("S3 isn't configured: set S3_BUCKET and AWS_* in .env (infra/s3-setup.sh)")
        self.bucket = settings.s3_bucket
        # SigV4 + regional endpoint: the default presigns for the global endpoint, which S3 rejects
        # for eu-central-1 (spike-notes.md)
        self.client = boto3.client(
            "s3", region_name=settings.aws_region, aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

    def put(self, key: str, path: Path) -> str:
        """Upload a JPEG and return a presigned GET URL for it."""
        self.client.put_object(Bucket=self.bucket, Key=key, Body=path.read_bytes(),
                               ContentType="image/jpeg")
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                                  ExpiresIn=URL_EXPIRY)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
