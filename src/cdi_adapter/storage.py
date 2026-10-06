"""Object storage for the irreplaceable bytes: original scans + rendered pages (ARCHITECTURE §3.3).

MinIO (S3-compatible) through boto3, pointed at ``CDI_S3_ENDPOINT_URL``. With a MinIO cluster
(backlog E10-S6) that setting lists every node, comma-separated: a node that stops answering is
skipped and the next one takes the call, so losing one node costs a retry, never a request.
Key layout (§5 S1):

    documents/<sha[:2]>/<sha>/original.<ext>
    documents/<sha[:2]>/<sha>/pages/NNNN.png
    documents/<sha[:2]>/<sha>/pages/NNNN.thumb.png
"""

from __future__ import annotations

from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

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


# a node that is gone or cannot serve (lost quorum answers 503): try the next node
_NODE_DOWN = (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, ConnectionClosedError)


def _node_down(exc: Exception) -> bool:
    if isinstance(exc, _NODE_DOWN):
        return True
    return isinstance(exc, ClientError) and exc.response.get("ResponseMetadata", {}).get(
        "HTTPStatusCode"
    ) in (502, 503, 504)


class ObjectStore:
    """Thin wrapper over an S3 client for one bucket, over one or several MinIO nodes."""

    def __init__(self, settings: Settings | None = None, *, client=None) -> None:
        s = settings or get_settings()
        self.bucket = s.s3_bucket
        self.endpoints = [e.strip() for e in s.s3_endpoint_url.split(",") if e.strip()]
        several = len(self.endpoints) > 1
        cfg = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # MinIO
            # one node of several: give up on it quickly, the next node takes the call
            retries=(
                {"total_max_attempts": 1, "mode": "standard"}
                if several
                else {"max_attempts": 3, "mode": "standard"}
            ),
            connect_timeout=2 if several else 60,
        )
        self._clients = [client] if client is not None else [
            boto3.client("s3", endpoint_url=e, aws_access_key_id=s.s3_access_key,
                         aws_secret_access_key=s.s3_secret_key, region_name="us-east-1",
                         config=cfg)
            for e in self.endpoints
        ]  # fmt: skip
        self._current = 0

    @property
    def client(self):  # noqa: ANN201 - a boto3 S3 client
        """The node answering now (the first that did last time)."""
        return self._clients[self._current]

    def _call(self, fn):  # noqa: ANN001, ANN202
        last: Exception | None = None
        for step in range(len(self._clients)):
            i = (self._current + step) % len(self._clients)
            try:
                result = fn(self._clients[i])
            except Exception as exc:
                if not _node_down(exc):
                    raise
                last = exc
                continue
            self._current = i
            return result
        assert last is not None
        raise last

    def ensure_bucket(self) -> None:
        try:
            self._call(lambda c: c.head_bucket(Bucket=self.bucket))
        except ClientError:
            self._call(lambda c: c.create_bucket(Bucket=self.bucket))

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._call(lambda c: c.put_object(Bucket=self.bucket, Key=key, Body=data,
                                          ContentType=content_type))  # fmt: skip

    def get_bytes(self, key: str) -> bytes:
        return self._call(lambda c: c.get_object(Bucket=self.bucket, Key=key)["Body"].read())

    def delete(self, key: str) -> None:
        """Remove one object (a no-op when it is already gone)."""
        self._call(lambda c: c.delete_object(Bucket=self.bucket, Key=key))

    def exists(self, key: str) -> bool:
        try:
            self._call(lambda c: c.head_object(Bucket=self.bucket, Key=key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def healthy(self) -> bool:
        try:
            self._call(lambda c: c.head_bucket(Bucket=self.bucket))
        except Exception:  # noqa: BLE001 - any failure is "not healthy" for /healthz
            return False
        return True


@lru_cache
def get_store() -> ObjectStore:
    return ObjectStore()
