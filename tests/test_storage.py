from dataclasses import dataclass, field

import pytest
from minio.error import S3Error

from workflows.settings import Settings
from workflows.storage import MinioStorage, build_storage, object_location

ENDPOINT = "s3-api.t3ks.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{ENDPOINT}/workflows/song.mp3", ("workflows", "song.mp3")),
        (f"https://{ENDPOINT}/suno/a/b.json", ("suno", "a/b.json")),
        ("https://other.example/workflows/song.mp3", None),
        (f"https://{ENDPOINT}/not-a-bucket/song.mp3", None),
        (f"https://{ENDPOINT}/workflows/", None),
    ],
)
def test_object_location(url: str, expected: tuple[str, str] | None) -> None:
    assert object_location(ENDPOINT, url) == expected


def test_build_storage_is_none_when_unconfigured() -> None:
    settings = Settings(session_secret="x")
    assert build_storage(settings) is None


def test_build_storage_returns_minio_storage_when_configured() -> None:
    settings = Settings(
        session_secret="x",
        minio_endpoint=ENDPOINT,
        minio_access_key="key",
        minio_secret_key="secret",
    )
    assert isinstance(build_storage(settings), MinioStorage)


def s3_error(code: str) -> S3Error:
    error = S3Error.__new__(S3Error)
    error.code = code
    return error


@dataclass
class FakeMinioClient:
    raises: S3Error | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def remove_object(self, bucket: str, key: str) -> None:
        self.calls.append((bucket, key))
        if self.raises is not None:
            raise self.raises


async def test_minio_storage_removes_the_object() -> None:
    client = FakeMinioClient()
    await MinioStorage(client).remove("workflows", "song.mp3")
    assert client.calls == [("workflows", "song.mp3")]


async def test_minio_storage_treats_a_missing_object_as_removed() -> None:
    client = FakeMinioClient(raises=s3_error("NoSuchKey"))
    await MinioStorage(client).remove("workflows", "song.mp3")
    assert client.calls == [("workflows", "song.mp3")]


async def test_minio_storage_reraises_other_s3_errors() -> None:
    client = FakeMinioClient(raises=s3_error("AccessDenied"))
    with pytest.raises(S3Error):
        await MinioStorage(client).remove("workflows", "song.mp3")
