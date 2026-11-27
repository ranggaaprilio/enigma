"""S3-compatible object storage for verification artifacts (Verifiable Agent Loop, Plan 6).

Evidence screenshots are stored here, keyed per workspace/run/evidence. The bucket is
private: objects are only ever read back through the authenticated API, never handed out
as public or presigned URLs. Any S3 failure surfaces as ``ArtifactStoreError`` carrying only
a generic reason, so neither credentials nor provider messages reach evidence or responses.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any, Protocol

import aioboto3
import structlog
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError

from app.core.config import Settings

logger = structlog.get_logger(__name__)

# Fail fast against a dead or black-holed store: an upload runs inside a verification run's
# observing step and a download inside a reviewer's request (botocore's defaults are 60 s
# connect + 60 s read, retried ~5 times).
S3_CLIENT_CONFIG = AioConfig(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})


class ArtifactStoreError(Exception):
    """The artifact store could not complete an operation (details are only logged)."""


class ArtifactStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class S3ArtifactStore:
    def __init__(self, *, bucket: str, client_factory: Callable[[], Any]) -> None:
        self.bucket = bucket
        self._client_factory = client_factory

    def __repr__(self) -> str:
        return f"S3ArtifactStore(bucket={self.bucket!r})"

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._call("put", key, lambda s3: s3.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        ))

    async def get(self, key: str) -> bytes:
        async def read(s3) -> bytes:
            response = await s3.get_object(Bucket=self.bucket, Key=key)
            async with response["Body"] as body:
                return await body.read()

        return await self._call("get", key, read)

    async def delete(self, key: str) -> None:
        await self._call("delete", key, lambda s3: s3.delete_object(Bucket=self.bucket, Key=key))

    async def _call(self, operation: str, key: str, action):
        try:
            async with self._client_factory() as s3:
                return await action(s3)
        except Exception as error:  # any client failure (aiohttp, a bad endpoint, botocore)
            code = (
                error.response.get("Error", {}).get("Code", "")
                if isinstance(error, ClientError)
                else type(error).__name__
            )
            logger.warning("artifact_store_failed", operation=operation, key=key, code=code)
            raise ArtifactStoreError(f"artifact store {operation} failed") from None


def get_artifact_store(settings: Settings) -> ArtifactStore | None:
    """The configured store, or ``None`` while no bucket is set (one per configuration)."""
    if not settings.artifact_s3_bucket:
        return None
    return _s3_store(
        settings.artifact_s3_bucket,
        settings.artifact_s3_endpoint_url,
        settings.artifact_s3_region,
        settings.artifact_s3_access_key_id,
        settings.artifact_s3_secret_access_key,
    )


@lru_cache(maxsize=4)
def _s3_store(
    bucket: str, endpoint_url: str, region: str, access_key_id: str, secret_access_key: str
) -> S3ArtifactStore:
    # Configuration only (no connection or in-flight state): a session reads botocore's data
    # files from disk, so it is built once; each operation opens its own client in its task.
    session = aioboto3.Session()

    def client():
        return session.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
            config=S3_CLIENT_CONFIG,
        )

    return S3ArtifactStore(bucket=bucket, client_factory=client)
