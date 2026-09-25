import asyncio
from typing import Protocol
from urllib.parse import unquote
from urllib.parse import urlsplit

from minio import Minio
from minio.error import S3Error

from workflows.settings import Settings

MISSING_KEY_CODE = "NoSuchKey"


class Storage(Protocol):
    async def remove(self, bucket: str, key: str) -> None: ...


class RemovableClient(Protocol):
    def remove_object(self, bucket: str, key: str) -> None: ...


class MinioStorage:
    def __init__(self, client: RemovableClient) -> None:
        self._client = client

    async def remove(self, bucket: str, key: str) -> None:
        await asyncio.to_thread(self._remove, bucket, key)

    def _remove(self, bucket: str, key: str) -> None:
        try:
            self._client.remove_object(bucket, key)
        except S3Error as error:
            if error.code != MISSING_KEY_CODE:
                raise


def object_location(endpoint: str, url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.netloc != endpoint:
        return None
    bucket, _, key = unquote(parsed.path).lstrip("/").partition("/")
    if not bucket or not key:
        return None
    return bucket, key


def build_storage(settings: Settings) -> Storage | None:
    if not (settings.minio_endpoint and settings.minio_access_key and settings.minio_secret_key):
        return None
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_use_ssl,
    )
    return MinioStorage(client)
