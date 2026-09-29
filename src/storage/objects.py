from functools import lru_cache
from typing import IO, Any, Union

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from src.core.config import SETTINGS
from src.core.errors import StorageError

_RAW_PREFIX = "raw"
_PARENTS_PREFIX = "parents"


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=SETTINGS.storage.s3_endpoint,
        aws_access_key_id=SETTINGS.storage.s3_access_key,
        aws_secret_access_key=SETTINGS.storage.s3_secret_key,
        region_name=SETTINGS.storage.s3_region,
        # Path-style addressing: non-AWS S3 endpoints do not serve virtual-host buckets.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def ensure_bucket() -> None:
    client = _client()
    bucket = SETTINGS.storage.s3_bucket
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)


def put_raw(doc_id: str, filename: str, data: Union[bytes, IO[bytes]]) -> str:
    """Body may be a file object: the API stages a spooled upload without reading it into RAM."""
    key = f"{_RAW_PREFIX}/{doc_id}/{filename}"
    _client().put_object(Bucket=SETTINGS.storage.s3_bucket, Key=key, Body=data)
    return key


def get_raw(doc_id: str, filename: str) -> bytes:
    key = f"{_RAW_PREFIX}/{doc_id}/{filename}"
    try:
        body: bytes = _client().get_object(Bucket=SETTINGS.storage.s3_bucket, Key=key)["Body"].read()
        return body
    except ClientError as exc:
        raise StorageError(f"Staged upload missing for document {doc_id}.") from exc


def put_parents(doc_id: str, ingest_version: str, payload: bytes) -> str:
    """Keyed by build as well as document: a reindex writes the new parents beside the old ones,
    so the index being rebuilt and the index being read never share a blob."""
    key = _parents_key(doc_id, ingest_version)
    _client().put_object(
        Bucket=SETTINGS.storage.s3_bucket, Key=key, Body=payload, ContentType="application/json"
    )
    return key


def get_parents(doc_id: str, ingest_version: str) -> bytes:
    key = _parents_key(doc_id, ingest_version)
    try:
        body: bytes = _client().get_object(Bucket=SETTINGS.storage.s3_bucket, Key=key)["Body"].read()
        return body
    except ClientError as exc:
        raise StorageError(
            f"Parent blob missing for document {doc_id} at build {ingest_version}."
        ) from exc


def _parents_key(doc_id: str, ingest_version: str) -> str:
    return f"{_PARENTS_PREFIX}/{doc_id}/{ingest_version}.json"


def list_doc_keys(doc_id: str) -> list[str]:
    """Every key holding this document's bytes. Used to verify an erasure actually erased."""
    client = _client()
    bucket = SETTINGS.storage.s3_bucket
    listing = client.list_objects_v2(Bucket=bucket, Prefix=f"{_RAW_PREFIX}/{doc_id}/")
    keys = [obj["Key"] for obj in listing.get("Contents", [])]
    parents = client.list_objects_v2(Bucket=bucket, Prefix=f"{_PARENTS_PREFIX}/{doc_id}/")
    return keys + [obj["Key"] for obj in parents.get("Contents", [])]


def delete_doc(doc_id: str) -> None:
    delete_keys(list_doc_keys(doc_id))


# S3's DeleteObjects takes at most 1000 keys per call and rejects the request outright past
# that, so the batching is the API's, not an optimisation.
_DELETE_BATCH = 1000


def delete_keys(keys: list[str]) -> None:
    client = _client()
    bucket = SETTINGS.storage.s3_bucket
    for start in range(0, len(keys), _DELETE_BATCH):
        batch = keys[start : start + _DELETE_BATCH]
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]})


def delete_parents(doc_id: str, ingest_version: str) -> None:
    """Drop one build's parents, leaving the document's other builds and its raw bytes alone."""
    _client().delete_object(
        Bucket=SETTINGS.storage.s3_bucket, Key=_parents_key(doc_id, ingest_version)
    )


def health() -> None:
    _client().head_bucket(Bucket=SETTINGS.storage.s3_bucket)
