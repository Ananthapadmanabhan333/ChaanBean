"""S3 storage.

Unused until an AWS account exists; selecting it is STORAGE_BACKEND=s3 plus
credentials. Enable bucket versioning: an audio asset is evidence of what was
played, and an overwrite without versioning destroys that quietly.

`local_path` returns None by design. Object storage has no path, and a caller
that needs a file must stage it explicitly rather than discover at call time
that the "path" was a URL.
"""

from __future__ import annotations

import hashlib


class S3Storage:
    name = "s3"

    def __init__(self, *, bucket: str, region: str, access_key=None, secret_key=None):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("boto3 is not installed") from exc

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )

    def put(self, key: str, data: bytes) -> str:
        # A single PutObject is atomic at the object level: readers see the old
        # version or the new one, never a partial write.
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ChecksumSHA256=None,
            ContentType="application/octet-stream",
        )
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def local_path(self, key: str) -> str | None:
        return None

    @staticmethod
    def checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
