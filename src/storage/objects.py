from functools import lru_cache
from typing import Any

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


def put_raw(doc_id: str, filename: str, data: bytes) -> str:
    key = f"{_RAW_PREFIX}/{doc_id}/{filename}"
    _client().put_object(Bucket=SETTINGS.storage.s3_bucket, Key=key, Body=data)
    return key


def put_parents(doc_id: str, payload: bytes) -> str:
    key = f"{_PARENTS_PREFIX}/{doc_id}.json"
    _client().put_object(
        Bucket=SETTINGS.storage.s3_bucket, Key=key, Body=payload, ContentType="application/json"
    )
    return key


def get_parents(doc_id: str) -> bytes:
    key = f"{_PARENTS_PREFIX}/{doc_id}.json"
    try:
        body: bytes = _client().get_object(Bucket=SETTINGS.storage.s3_bucket, Key=key)["Body"].read()
        return body
    except ClientError as exc:
        raise StorageError(f"Parent blob missing for document {doc_id}.") from exc


def delete_doc(doc_id: str) -> None:
    client = _client()
    bucket = SETTINGS.storage.s3_bucket
    listing = client.list_objects_v2(Bucket=bucket, Prefix=f"{_RAW_PREFIX}/{doc_id}/")
    keys = [{"Key": obj["Key"]} for obj in listing.get("Contents", [])]
    keys.append({"Key": f"{_PARENTS_PREFIX}/{doc_id}.json"})
    client.delete_objects(Bucket=bucket, Delete={"Objects": keys})


def health() -> None:
    _client().head_bucket(Bucket=SETTINGS.storage.s3_bucket)
