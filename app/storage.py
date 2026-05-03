from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "s3").strip().lower() or "s3"
S3_BUCKET = os.getenv("S3_BUCKET", "wh40k-dev-uploads")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL") or None
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

_MEMORY_OBJECTS: dict[str, tuple[bytes, str | None]] = {}


class ObjectNotFound(FileNotFoundError):
    pass


@dataclass(frozen=True)
class StoredObject:
    content: bytes
    content_type: str | None = None


def storage_label() -> str:
    if STORAGE_BACKEND == "memory":
        return "memory"
    endpoint = f" via {S3_ENDPOINT_URL}" if S3_ENDPOINT_URL else ""
    return f"s3://{S3_BUCKET}{endpoint}"


def _s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        region_name=AWS_REGION,
    )


def _is_missing_object(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
    return code in {"NoSuchKey", "404", "NotFound"} or status == 404


def put_object(key: str, content: bytes, content_type: str | None = None) -> None:
    if STORAGE_BACKEND == "memory":
        _MEMORY_OBJECTS[key] = (bytes(content), content_type)
        return

    _s3_client().put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=content,
        ContentType=content_type or "application/octet-stream",
    )


def get_object(key: str) -> StoredObject:
    if STORAGE_BACKEND == "memory":
        try:
            content, content_type = _MEMORY_OBJECTS[key]
        except KeyError as exc:
            raise ObjectNotFound(key) from exc
        return StoredObject(content=content, content_type=content_type)

    try:
        response = _s3_client().get_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as exc:
        if _is_missing_object(exc):
            raise ObjectNotFound(key) from exc
        raise
    return StoredObject(
        content=response["Body"].read(),
        content_type=response.get("ContentType"),
    )


def delete_object(key: str) -> None:
    if STORAGE_BACKEND == "memory":
        _MEMORY_OBJECTS.pop(key, None)
        return

    try:
        _s3_client().delete_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as exc:
        if not _is_missing_object(exc):
            raise


def clear_memory_storage() -> None:
    _MEMORY_OBJECTS.clear()
