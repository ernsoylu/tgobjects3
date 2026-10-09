"""Workflow matrix tests for the boto3 S3 client (Requirement 20.2).

This suite exercises the gateway through boto3's S3 client against a running
gateway endpoint, covering bucket creation/deletion, object put/get/delete,
listing (ListObjectsV2 with prefix/delimiter), and a multipart upload. It is
the boto3 row of the workflow matrix defined in the design's Testing Strategy
and recorded in ``tests/workflow_matrix/pin.json``.

Collection and execution model
-------------------------------
The suite is always collectable so CI stays green-by-skip when the matrix
environment is not provisioned:

* The whole module is skipped unless ``TGOS3_TEST_ENDPOINT`` names a running
  gateway endpoint.
* boto3 is not a pinned project dependency, so it is imported with
  :func:`pytest.importorskip`; when it is importable we assert the installed
  version matches ``pin.json`` (when the pin is present).

The gateway speaks a standard SigV4-signed S3 surface, so the client is built
with ``endpoint_url`` pointing at the gateway and test credentials pulled from
the environment. No sibling ``conftest.py`` is assumed: the skip guard and all
fixtures are defined locally in this module.
"""

from __future__ import annotations

import io
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = Any

# --- Skip guard: endpoint must be configured --------------------------------

_ENDPOINT_ENV = "TGOS3_TEST_ENDPOINT"
_ENDPOINT = os.environ.get(_ENDPOINT_ENV)

pytestmark = pytest.mark.skipif(
    not _ENDPOINT,
    reason=f"{_ENDPOINT_ENV} is not set; workflow matrix endpoint not provisioned",
)

# boto3 is not a pinned dependency of the project; skip the whole module if it
# cannot be imported in the current environment.
boto3 = pytest.importorskip("boto3", reason="boto3 is not installed")
botocore = pytest.importorskip("botocore", reason="botocore is not installed")

from botocore.config import Config as BotoConfig  # noqa: E402

# --- Pinned-version assertion -----------------------------------------------

_PIN_PATH = Path(__file__).with_name("pin.json")


def _pinned_boto3_version() -> str | None:
    """Return the boto3 version recorded in ``pin.json``, or ``None``."""
    if not _PIN_PATH.is_file():
        return None
    data = json.loads(_PIN_PATH.read_text(encoding="utf-8"))
    pinned = data.get("boto3")
    return str(pinned) if pinned is not None else None


def test_boto3_version_matches_pin() -> None:
    """The installed boto3 version matches ``pin.json`` when a pin is present."""
    pinned = _pinned_boto3_version()
    if pinned is None:
        pytest.skip("no boto3 version pinned in pin.json")
    assert boto3.__version__ == pinned, (
        f"installed boto3 {boto3.__version__} does not match pinned {pinned}"
    )


# --- Client fixtures --------------------------------------------------------


@pytest.fixture(scope="module")
def s3_client() -> S3Client:
    """A boto3 S3 client pointed at the gateway endpoint using SigV4.

    Credentials and region come from the environment, falling back to the
    gateway's defaults so a minimally-provisioned matrix environment that only
    sets ``TGOS3_TEST_ENDPOINT`` still works against a dev gateway.
    """
    access_key = os.environ.get("TGOS3_TEST_ACCESS_KEY", "test-access-key")
    secret_key = os.environ.get("TGOS3_TEST_SECRET_KEY", "test-secret-key")
    region = os.environ.get("TGOS3_TEST_REGION", "us-east-1")

    return boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
        ),
    )


@pytest.fixture
def bucket(s3_client: S3Client) -> Iterator[str]:
    """Create a uniquely-named bucket and tear it down after the test.

    The teardown removes any objects left behind so DeleteBucket (which the
    gateway rejects for non-empty buckets with 409) always succeeds.
    """
    name = f"tgos3-boto3-{uuid.uuid4().hex[:16]}"
    s3_client.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        _drain_and_delete_bucket(s3_client, name)


def _drain_and_delete_bucket(s3_client: S3Client, name: str) -> None:
    """Best-effort removal of all objects then the bucket itself."""
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=name):
            for obj in page.get("Contents", []):
                s3_client.delete_object(Bucket=name, Key=obj["Key"])
        s3_client.delete_bucket(Bucket=name)
    except botocore.exceptions.ClientError:
        # Teardown is best-effort; surfacing cleanup errors would mask the
        # actual test outcome.
        pass


# --- Bucket lifecycle -------------------------------------------------------


def test_create_head_and_list_buckets(s3_client: S3Client, bucket: str) -> None:
    """A freshly created bucket is head-able and appears in ListBuckets."""
    # HeadBucket succeeds (raises ClientError on failure).
    s3_client.head_bucket(Bucket=bucket)

    names = {b["Name"] for b in s3_client.list_buckets().get("Buckets", [])}
    assert bucket in names


def test_delete_bucket_removes_it(s3_client: S3Client) -> None:
    """DeleteBucket removes an empty bucket; it is no longer head-able."""
    name = f"tgos3-boto3-{uuid.uuid4().hex[:16]}"
    s3_client.create_bucket(Bucket=name)
    s3_client.delete_bucket(Bucket=name)

    with pytest.raises(botocore.exceptions.ClientError) as excinfo:
        s3_client.head_bucket(Bucket=name)
    assert excinfo.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


# --- Object put / get / delete ----------------------------------------------


def test_put_get_object_round_trip(s3_client: S3Client, bucket: str) -> None:
    """PutObject then GetObject returns the exact bytes written."""
    key = "round-trip/object.bin"
    payload = b"the quick brown fox jumps over the lazy dog\x00\x01\x02"

    s3_client.put_object(Bucket=bucket, Key=key, Body=payload)

    resp = s3_client.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()
    assert body == payload
    assert resp["ContentLength"] == len(payload)


def test_delete_object(s3_client: S3Client, bucket: str) -> None:
    """DeleteObject removes the key; a subsequent GetObject raises 404."""
    key = "to-delete.txt"
    s3_client.put_object(Bucket=bucket, Key=key, Body=b"ephemeral")

    s3_client.delete_object(Bucket=bucket, Key=key)

    with pytest.raises(botocore.exceptions.ClientError) as excinfo:
        s3_client.get_object(Bucket=bucket, Key=key)
    assert excinfo.value.response["Error"]["Code"] in {"NoSuchKey", "404"}


# --- Listing ----------------------------------------------------------------


def test_list_objects_v2_with_prefix_and_delimiter(
    s3_client: S3Client, bucket: str
) -> None:
    """ListObjectsV2 honours prefix filtering and delimiter grouping.

    Keys under ``docs/`` with a ``/`` delimiter collapse to common prefixes,
    while a top-level key is excluded by the prefix filter.
    """
    keys = [
        "docs/a.txt",
        "docs/sub/b.txt",
        "docs/sub/c.txt",
        "images/x.png",
    ]
    for key in keys:
        s3_client.put_object(Bucket=bucket, Key=key, Body=b"x")

    resp = s3_client.list_objects_v2(Bucket=bucket, Prefix="docs/", Delimiter="/")

    contents = {obj["Key"] for obj in resp.get("Contents", [])}
    common = {cp["Prefix"] for cp in resp.get("CommonPrefixes", [])}

    # Only the immediate child file is a direct content entry.
    assert contents == {"docs/a.txt"}
    # The nested keys collapse into a single common prefix.
    assert common == {"docs/sub/"}
    # Contents and common prefixes are disjoint, and the unrelated prefix is
    # excluded entirely.
    assert contents.isdisjoint(common)
    assert all(not k.startswith("images/") for k in contents)


def test_list_objects_v2_returns_all_keys(s3_client: S3Client, bucket: str) -> None:
    """Without a delimiter, every key under the prefix is listed."""
    expected = {f"flat/{i:03d}.dat" for i in range(5)}
    for key in expected:
        s3_client.put_object(Bucket=bucket, Key=key, Body=b"y")

    resp = s3_client.list_objects_v2(Bucket=bucket, Prefix="flat/")
    listed = {obj["Key"] for obj in resp.get("Contents", [])}
    assert listed == expected
    # Lexicographic ordering is required by the S3 contract.
    returned_order = [obj["Key"] for obj in resp.get("Contents", [])]
    assert returned_order == sorted(returned_order)


# --- Multipart upload -------------------------------------------------------


def test_multipart_upload_round_trip(s3_client: S3Client, bucket: str) -> None:
    """A multipart upload via boto3's managed transfer round-trips correctly.

    ``upload_fileobj`` drives boto3's multipart machinery
    (CreateMultipartUpload / UploadPart / CompleteMultipartUpload) when the
    payload crosses the configured multipart threshold.
    """
    from boto3.s3.transfer import TransferConfig

    key = "multipart/large.bin"
    # 12 MiB of deterministic data, forced through the multipart path with a
    # 5 MiB threshold/chunk size (S3's minimum non-final part size).
    part_size = 5 * 1024 * 1024
    payload = (b"tgobjects3-boto3-multipart" * 4096)[: 12 * 1024 * 1024]
    payload = payload + b"\x00" * (12 * 1024 * 1024 - len(payload))

    config = TransferConfig(
        multipart_threshold=part_size,
        multipart_chunksize=part_size,
        max_concurrency=2,
        use_threads=True,
    )

    s3_client.upload_fileobj(io.BytesIO(payload), bucket, key, Config=config)

    resp = s3_client.get_object(Bucket=bucket, Key=key)
    assert resp["Body"].read() == payload
