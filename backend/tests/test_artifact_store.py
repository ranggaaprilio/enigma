"""Plan 6 Task 2: the S3-compatible artifact store behind evidence screenshots."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.config import Settings
from app.services.artifact_store import (
    ArtifactStoreError,
    S3ArtifactStore,
    get_artifact_store,
)

SECRET = "super-secret-key"


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self) -> bytes:
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FakeS3:
    def __init__(self, error: Exception | None = None) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.error = error

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803 (boto names)
        self._maybe_fail()
        self.objects[(Bucket, Key)] = (Body, ContentType)

    async def get_object(self, *, Bucket, Key):  # noqa: N803
        self._maybe_fail()
        data, _content_type = self.objects[(Bucket, Key)]
        return {"Body": FakeBody(data)}

    async def delete_object(self, *, Bucket, Key):  # noqa: N803
        self._maybe_fail()
        self.objects.pop((Bucket, Key), None)


def _store(fake: FakeS3) -> S3ArtifactStore:
    class _Client:
        async def __aenter__(self):
            return fake

        async def __aexit__(self, *_exc):
            return False

    return S3ArtifactStore(bucket="evidence", client_factory=lambda: _Client())


def _settings(**overrides) -> Settings:
    base = dict(
        artifact_s3_bucket="",
        artifact_s3_endpoint_url="",
        artifact_s3_access_key_id="",
        artifact_s3_secret_access_key="",
    )
    return Settings(**{**base, **overrides})


def test_no_bucket_means_no_store() -> None:
    assert get_artifact_store(_settings()) is None


def test_a_bucket_configures_an_s3_store() -> None:
    store = get_artifact_store(
        _settings(
            artifact_s3_bucket="evidence",
            artifact_s3_endpoint_url="http://minio:9000",
            artifact_s3_access_key_id="key",
            artifact_s3_secret_access_key=SECRET,
        )
    )
    assert isinstance(store, S3ArtifactStore)
    assert store.bucket == "evidence"
    assert SECRET not in repr(store)


async def test_put_get_delete_round_trip() -> None:
    fake = FakeS3()
    store = _store(fake)

    await store.put("verification/a.png", b"\x89PNG data", "image/png")
    assert fake.objects[("evidence", "verification/a.png")] == (b"\x89PNG data", "image/png")
    assert await store.get("verification/a.png") == b"\x89PNG data"
    await store.delete("verification/a.png")
    assert fake.objects == {}


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "AccessDenied", "Message": SECRET}}, "PutObject"),
        EndpointConnectionError(endpoint_url="http://minio:9000"),
    ],
)
async def test_s3_errors_become_artifact_store_errors_without_details(error) -> None:
    store = _store(FakeS3(error=error))
    with pytest.raises(ArtifactStoreError) as raised:
        await store.put("k", b"x", "image/png")
    assert SECRET not in str(raised.value)
    with pytest.raises(ArtifactStoreError):
        await store.get("k")


async def test_any_client_failure_becomes_an_artifact_store_error() -> None:
    """Review: a dropped connection (aiohttp) or a bad endpoint must not escape as a raw
    exception -- that would turn an optional screenshot into a failed verification run."""
    store = _store(FakeS3(error=OSError("connection reset by peer")))
    with pytest.raises(ArtifactStoreError):
        await store.put("k", b"x", "image/png")


def test_the_store_is_built_once_per_configuration() -> None:
    """Review: building an aioboto3 session reads botocore's data files from disk; do it once."""
    settings = _settings(artifact_s3_bucket="evidence", artifact_s3_endpoint_url="http://s3:9000")
    assert get_artifact_store(settings) is get_artifact_store(settings)


def test_the_client_fails_fast_against_a_dead_store() -> None:
    from app.services.artifact_store import S3_CLIENT_CONFIG

    assert S3_CLIENT_CONFIG.connect_timeout <= 5
    assert S3_CLIENT_CONFIG.read_timeout <= 15
    assert S3_CLIENT_CONFIG.retries["max_attempts"] <= 2
