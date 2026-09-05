"""Storage adapter selection."""

from __future__ import annotations

from app.config import settings
from app.storage.local import LocalStorage


def build_storage():
    if settings.storage_backend == "s3":
        from app.storage.s3 import S3Storage

        if not settings.s3_bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 but S3_BUCKET is unset")
        return S3Storage(
            bucket=settings.s3_bucket,
            region=settings.aws_region,
            access_key=settings.aws_access_key_id,
            secret_key=settings.aws_secret_access_key,
        )
    return LocalStorage(settings.audio_dir)


__all__ = ["LocalStorage", "build_storage"]
