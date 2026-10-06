"""Object storage for the irreplaceable bytes: original scans + rendered pages (ARCHITECTURE §3.3).

MinIO (S3-compatible) through boto3, pointed at ``CDI_S3_ENDPOINT_URL``. Key layout (§5 S1):

    documents/<sha[:2]>/<sha>/original.<ext>
    documents/<sha[:2]>/<sha>/pages/NNNN.png
    documents/<sha[:2]>/<sha>/pages/NNNN.thumb.png
"""

from __future__ import annotations

from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from cdi_adapter.config import Settings, get_settings


def _prefix(sha256: str) -> str:
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    return f"documents/{sha256[:2]}/{sha256}"


def original_key(sha256: str, ext: str) -> str:
    return f"{_prefix(sha256)}/original.{ext.lstrip('.').lower()}"


def page_key(sha256: str, page_no: int) -> str:
    return f"{_prefix(sha256)}/pages/{page_no:04d}.png"


def thumb_key(sha256: str, page_no: int) -> str:
    return f"{_prefix(sha256)}/pages/{page_no:04d}.thumb.png"


class ObjectStore:
    """Thin wrapper over an S3 client for one bucket."""

    def __init__(self, settings: Settings | None = None, *, client=None) -> None:
        s = settings or get_settings()
        self.bucket = s.s3_bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url,
            aws_access_key_id=s.s3_access_key,
            aws_secret_access_key=s.s3_secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},  # MinIO
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def healthy(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:  # noqa: BLE001 - any failure is "not healthy" for /healthz
            return False
        return True


@lru_cache
def get_store() -> ObjectStore:
    return ObjectStore()
