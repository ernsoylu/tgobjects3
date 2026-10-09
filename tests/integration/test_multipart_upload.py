"""Multipart upload round-trip integration test (Requirements 10.2, 10.4, 10.9).

This integration test exercises the full S3 multipart upload surface end to end
against a *real* PostgreSQL 17 instance (started with ``testcontainers[postgres]``)
and the in-memory Telegram stub that stands in for a bucket channel through the
:class:`~gateway.app.telegram.port.TelegramPort` boundary.

The behaviour under test is the design's multipart contract
(``gateway/app/s3/operations/multipart.py``):

* **UploadPart (Requirement 10.2)** stages each part body durably (fsync +
  MD5 checksum) and returns the part ETag; the 5 MiB minimum applies to every
  part except the highest-numbered one, so the test uploads two 5 MiB parts and
  a smaller final part.
* **CompleteMultipartUpload (Requirement 10.4)** concatenates the staged parts
  in ascending part-number order and drives the assembled object to COMMITTED
  through the very same Upload Worker path PutObject uses — here the production
  :class:`~gateway.app.main.WorkerCommitDriver` wrapping a real
  :class:`~gateway.app.jobs.upload_worker.UploadWorker`.
* **Composite ETag (Requirement 10.9)** returned by Complete has the form
  ``<md5-of-concatenated-part-md5s>-<part-count>`` — for a three-part upload it
  ends in ``-3`` and its hex prefix is the MD5 of the three raw part MD5 digests
  concatenated in ascending part-number order.

A subsequent GetObject reads the committed object back through Telegram and must
return byte-for-byte the concatenation of the three parts in order, proving the
multipart object is durable and readable, not merely a committed index row.

The write/read collaborators are the production objects, not fakes: the bucket is
created through the real :class:`~gateway.app.s3.operations.bucket_ops.CreateBucketHandler`,
parts are staged and completed through the real :class:`MultipartHandler`, the
assembled blob is sealed and sent to the stub channel by the real
:class:`UploadWorker`, and :class:`~gateway.app.s3.operations.get_object.GetObjectHandler`
downloads those same chunk documents back and unseals them. Only the Telegram
transport is the in-memory :class:`FaultInjectingTelegramPort`, driven here with
no faults so it behaves like a faithful channel store.

The suite needs a Docker daemon to launch PostgreSQL. When no container runtime
is available (CI without Docker, or a sandbox) the module skips cleanly rather
than failing, so ``make lint`` / ``make typecheck`` stay meaningful without a
daemon present. The Telegram side never touches the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest
from gateway.app.cache.store import RecordCache
from gateway.app.config import Settings
from gateway.app.db.session import AsyncSessionFactory
from gateway.app.jobs.upload_worker import UploadWorker
from gateway.app.main import WorkerCommitDriver
from gateway.app.s3 import router as s3_router
from gateway.app.s3.operations.bucket_ops import CreateBucketHandler
from gateway.app.s3.operations.get_object import GetObjectHandler
from gateway.app.s3.operations.multipart import MultipartHandler
from gateway.app.storage.staging import StagingReservation
from gateway.app.telegram.port import TelegramPort
from starlette.requests import Request
from starlette.responses import StreamingResponse

from tests.integration.fault_injection import FaultInjectingTelegramPort

# ``testcontainers`` is an integration-only dependency and starting a container
# requires a Docker daemon. Guard the import so the module can be collected
# everywhere and skip with a clear reason when the package is absent.
try:
    from testcontainers.postgres import PostgresContainer

    _TESTCONTAINERS_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only without the extra
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_IMPORT_ERROR = str(exc)


# Repository root: tests/integration/<file>.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"

#: Postgres image pinned to the major version the design mandates (17+).
_POSTGRES_IMAGE = "postgres:17-alpine"

#: Master key used to derive the root key that wraps the bucket KEK and seals
#: records. Satisfies the >=16-char ``TGOS3_MASTER_KEY`` contract.
_MASTER_KEY = "integration-test-master-key-0001"

#: The bucket the multipart object is written to; its channel is backed by the
#: stub.
_BUCKET_NAME = "multipart-upload-bucket"
_OBJECT_KEY = "nested/path/large-object.bin"

#: The S3 minimum part size (5 MiB). Parts 1 and 2 sit exactly at this floor so
#: the Complete-time minimum-size check (Requirement 10.2) passes; the final
#: part is deliberately smaller, which is allowed only for the highest part.
_MIN_PART = 5 * 1024 * 1024

#: The three part bodies, in ascending part-number order. Distinct, repeating
#: patterns make an out-of-order concatenation or a dropped part obvious in the
#: GET round-trip. Parts 1 and 2 are >= 5 MiB; part 3 is the small final part.
_PART_BODIES: tuple[bytes, ...] = (
    (b"tgobjects3-multipart-part-one!!!" * 200_000)[:_MIN_PART],
    (b"tgobjects3-multipart-part-two@@@" * 200_000)[:_MIN_PART],
    (b"tgobjects3-multipart-part-three#" * 1_000)[:17_000],
)


# ---------------------------------------------------------------------------
# A minimal in-process request object for driving operation handlers
# ---------------------------------------------------------------------------


def _build_asgi_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: bytes,
    query_string: bytes = b"",
) -> Request:
    """Build a real Starlette :class:`Request` streaming ``body``.

    The multipart handlers read the body through ``request.stream()`` /
    ``request.body()``, the request headers through ``request.headers`` and the
    ``uploadId`` / ``partNumber`` through ``request.query_params`` — all genuine
    Starlette surfaces, so constructing a real ``Request`` over an ASGI scope
    exercises the same code paths a live uvicorn request would, without standing
    up a server. ``query_string`` carries the already-encoded query so the
    multipart dispatch sees the parameters it keys on.
    """
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in headers.items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query_string,
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 9000),
        "scheme": "http",
    }

    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def _make_context(
    operation: s3_router.S3Operation,
    *,
    method: str,
    settings: Settings,
    bucket: str,
    key: str | None = None,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query_string: bytes = b"",
) -> s3_router.S3HandlerContext:
    """Assemble the handler context the router would hand an operation handler.

    SigV4 has already run by the time a handler is invoked, so the test supplies
    a throwaway sentinel for ``auth``; the bucket, multipart and GET handlers
    never consult it (authentication is the router's concern, Requirement 6.1).
    """
    path = f"/{bucket}" if key is None else f"/{bucket}/{key}"
    request = _build_asgi_request(
        method, path, headers=headers or {}, body=body, query_string=query_string
    )
    return s3_router.S3HandlerContext(
        request=request,
        operation=operation,
        target=s3_router.ResolvedTarget(bucket=bucket, key=key),
        auth=cast("Any", object()),
        settings=settings,
    )


async def _drain_response_body(response: StreamingResponse) -> bytes:
    """Collect the full body of a :class:`StreamingResponse` into bytes.

    GetObject and the keep-alive CompleteMultipartUpload both return streaming
    responses; iterating to exhaustion reproduces exactly the bytes a client
    would receive on the wire (including Complete's leading whitespace frames).
    """
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# PostgreSQL helpers (shared pattern with test_put_object_sync_durability)
# ---------------------------------------------------------------------------


def _async_url(sync_url: str) -> str:
    """Normalise a container connection URL to the ``asyncpg`` driver."""
    bare = sync_url.split("://", 1)[1]
    return "postgresql+asyncpg://" + bare


def _run_alembic_upgrade(async_url: str) -> None:
    """Run ``alembic upgrade head`` through the project's real ``env.py``.

    Creates the schema the multipart/write/read path uses, exactly as the gateway
    does at startup. Runs from the repo root so the relative ``script_location``
    resolves.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", "gateway/app/db/migrations")

    previous_url = os.environ.get("DATABASE_URL")
    previous_cwd = Path.cwd()
    os.environ["DATABASE_URL"] = async_url
    try:
        os.chdir(_REPO_ROOT)
        command.upgrade(config, "head")
    finally:
        os.chdir(previous_cwd)
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url


def _build_settings(async_url: str) -> Settings:
    """Construct a frozen :class:`Settings` for the multipart round-trip.

    Durability defaults to ``sync`` so Complete only returns its success body
    once the assembled generation is COMMITTED; the staging/object ceilings sit
    well above the ~10 MiB object. Configuration is consumed only through this
    object, per the centralised-config convention.
    """
    return Settings(
        TGOS3_MASTER_KEY=_MASTER_KEY,
        DATABASE_URL=async_url,
        DEFAULT_DURABILITY="sync",
        STAGING_MAX_SIZE=128 * 1024 * 1024,
        MAX_OBJECT_SIZE=128 * 1024 * 1024,
        CHUNK_RECORDS=512,
        UPLOAD_CONCURRENCY=2,
        READ_AHEAD=4,
        CACHE_ENCRYPT=False,
    )


# ---------------------------------------------------------------------------
# Round-trip driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MultipartResult:
    """The observable outcome of one full multipart upload round-trip."""

    create_bucket_status: int
    initiate_status: int
    upload_part_statuses: tuple[int, ...]
    part_etags: tuple[str, ...]
    complete_etag: str
    get_status: int
    get_body: bytes


def _expected_composite_etag(part_md5_hex: tuple[str, ...]) -> str:
    """Compute the expected composite multipart ETag (Requirement 10.9).

    The raw MD5 digests of the parts (in ascending part-number order) are
    concatenated, hashed with MD5, and the hex digest is suffixed with
    ``-<part-count>`` — the same construction the handler performs, recomputed
    here independently so the assertion checks behaviour rather than echoing the
    implementation.
    """
    digest = hashlib.md5()
    for md5_hex in part_md5_hex:
        digest.update(bytes.fromhex(md5_hex))
    return f"{digest.hexdigest()}-{len(part_md5_hex)}"


def _parse_upload_id(body: bytes) -> str:
    """Extract the ``UploadId`` from an InitiateMultipartUploadResult body."""
    root = ET.fromstring(body)
    for child in root:
        if child.tag.rsplit("}", 1)[-1] == "UploadId":
            assert child.text is not None
            return child.text
    raise AssertionError("InitiateMultipartUploadResult carried no <UploadId>")


def _parse_complete_etag(body: bytes) -> str:
    """Extract the ``ETag`` from a CompleteMultipartUploadResult body.

    Complete streams keep-alive whitespace before the real document, so the body
    is stripped of leading insignificant whitespace before parsing. An inlined
    ``<Error>`` document (a late failure) raises so the test fails loudly rather
    than silently matching nothing.
    """
    root = ET.fromstring(body.strip())
    local = root.tag.rsplit("}", 1)[-1]
    if local == "Error":
        raise AssertionError(
            f"CompleteMultipartUpload returned an Error document: {body!r}"
        )
    for child in root:
        if child.tag.rsplit("}", 1)[-1] == "ETag":
            assert child.text is not None
            return child.text
    raise AssertionError("CompleteMultipartUploadResult carried no <ETag>")


def _complete_request_body(parts: list[tuple[int, str]]) -> bytes:
    """Serialise a CompleteMultipartUpload request naming ``(part_no, etag)``.

    The handler parses the ``Part`` list and validates ascending order against
    the staged rows, so the parts are emitted in ascending part-number order.
    """
    root = ET.Element("CompleteMultipartUpload")
    for part_no, etag in parts:
        part = ET.SubElement(root, "Part")
        ET.SubElement(part, "PartNumber").text = str(part_no)
        ET.SubElement(part, "ETag").text = etag
    return ET.tostring(root, encoding="utf-8")


async def _run_round_trip(async_url: str, tmp_path: Path) -> _MultipartResult:
    """Create the bucket, run a three-part multipart upload, then GET it back.

    Wires the production bucket/multipart/read collaborators around a real
    session factory and the in-memory Telegram stub, then drives
    CreateBucket -> CreateMultipartUpload -> UploadPart x3 ->
    CompleteMultipartUpload -> GetObject through the real handlers. The commit
    boundary is advanced by the production :class:`WorkerCommitDriver`, which
    pumps the real :class:`UploadWorker` until the assembled generation reaches a
    terminal state.
    """
    settings = _build_settings(async_url)
    telegram = FaultInjectingTelegramPort()

    staging_dir = tmp_path / "staging"
    cache_dir = tmp_path / "cache"

    factory = AsyncSessionFactory.create(async_url)
    try:
        # ---- Bucket creation (real handler + stub channel) ----------------
        create_bucket = CreateBucketHandler(factory, cast(TelegramPort, telegram))
        create_bucket_response = await create_bucket(
            _make_context(
                s3_router.S3Operation.CREATE_BUCKET,
                method="PUT",
                settings=settings,
                bucket=_BUCKET_NAME,
            )
        )

        worker = UploadWorker(
            factory.sessionmaker,
            cast(TelegramPort, telegram),
            settings.TGOS3_MASTER_KEY,
            upload_concurrency=settings.UPLOAD_CONCURRENCY,
            default_chunk_records=settings.CHUNK_RECORDS,
        )
        commit_driver = WorkerCommitDriver(factory, worker)
        multipart = MultipartHandler(
            session_factory=factory,
            staging=StagingReservation(settings.STAGING_MAX_SIZE),
            staging_dir=staging_dir,
            commit_driver=commit_driver,
        )

        # ---- CreateMultipartUpload -> UploadId ---------------------------
        initiate_response = await multipart(
            _make_context(
                s3_router.S3Operation.CREATE_MULTIPART_UPLOAD,
                method="POST",
                settings=settings,
                bucket=_BUCKET_NAME,
                key=_OBJECT_KEY,
                query_string=b"uploads",
            )
        )
        upload_id = _parse_upload_id(bytes(getattr(initiate_response, "body", b"")))

        # ---- UploadPart x3 (parts 1 and 2 >= 5 MiB; part 3 the small tail)
        upload_statuses: list[int] = []
        part_etags: list[str] = []
        for index, body in enumerate(_PART_BODIES):
            part_no = index + 1
            query = f"uploadId={upload_id}&partNumber={part_no}".encode()
            part_response = await multipart(
                _make_context(
                    s3_router.S3Operation.UPLOAD_PART,
                    method="PUT",
                    settings=settings,
                    bucket=_BUCKET_NAME,
                    key=_OBJECT_KEY,
                    headers={"content-length": str(len(body))},
                    body=body,
                    query_string=query,
                )
            )
            upload_statuses.append(part_response.status_code)
            part_etags.append(part_response.headers["ETag"])

        # ---- CompleteMultipartUpload (keep-alive stream -> final body) ---
        complete_body = _complete_request_body(
            [(index + 1, etag) for index, etag in enumerate(part_etags)]
        )
        complete_response = await asyncio.wait_for(
            multipart(
                _make_context(
                    s3_router.S3Operation.COMPLETE_MULTIPART_UPLOAD,
                    method="POST",
                    settings=settings,
                    bucket=_BUCKET_NAME,
                    key=_OBJECT_KEY,
                    body=complete_body,
                    query_string=f"uploadId={upload_id}".encode(),
                )
            ),
            timeout=60.0,
        )
        complete_payload = await _drain_response_body(
            cast(StreamingResponse, complete_response)
        )
        complete_etag = _parse_complete_etag(complete_payload)

        # ---- GET the committed object back -------------------------------
        cache = RecordCache(settings, factory, cache_root=cache_dir)
        get_handler = GetObjectHandler(
            factory, cast(TelegramPort, telegram), cache, settings
        )
        get_response = await get_handler(
            _make_context(
                s3_router.S3Operation.GET_OBJECT,
                method="GET",
                settings=settings,
                bucket=_BUCKET_NAME,
                key=_OBJECT_KEY,
            )
        )
        get_body = (
            await _drain_response_body(cast(StreamingResponse, get_response))
            if isinstance(get_response, StreamingResponse)
            else bytes(getattr(get_response, "body", b""))
        )
    finally:
        await factory.dispose()

    return _MultipartResult(
        create_bucket_status=create_bucket_response.status_code,
        initiate_status=initiate_response.status_code,
        upload_part_statuses=tuple(upload_statuses),
        part_etags=tuple(part_etags),
        complete_etag=complete_etag,
        get_status=get_response.status_code,
        get_body=get_body,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _skip_if_no_docker() -> None:
    """Skip the whole module when ``testcontainers`` cannot be imported."""
    if PostgresContainer is None:
        pytest.skip(
            "testcontainers[postgres] is unavailable: "
            f"{_TESTCONTAINERS_IMPORT_ERROR}"
        )


@pytest.fixture(scope="module")
def round_trip(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_MultipartResult]:
    """Start PostgreSQL 17, run one multipart round-trip, yield the result.

    Module-scoped so the expensive container start, migration run and the full
    multipart upload + GET happen once for every assertion in this file. Skips
    cleanly when Docker is unavailable so the suite stays collectable everywhere.
    """
    _skip_if_no_docker()

    try:
        container = PostgresContainer(_POSTGRES_IMAGE)
        container.start()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Could not start PostgreSQL test container: {exc}")

    try:
        async_url = _async_url(container.get_connection_url())
        _run_alembic_upgrade(async_url)
        tmp_path = tmp_path_factory.mktemp("multipart-upload")
        result = asyncio.run(_run_round_trip(async_url, tmp_path))
        yield result
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_multipart_upload_returns_upload_id(
    round_trip: _MultipartResult,
) -> None:
    """CreateMultipartUpload returns HTTP 200 with a parseable UploadId.

    The UploadId is the handle every later operation keys on, so a 200 initiate
    is the precondition for the part/complete/get assertions below.

    Validates: Requirements 10.4
    """
    assert round_trip.create_bucket_status == 200
    assert round_trip.initiate_status == 200


def test_upload_part_stages_three_parts(round_trip: _MultipartResult) -> None:
    """Each of the three UploadPart calls returns HTTP 200 with a quoted MD5 ETag.

    Parts 1 and 2 are 5 MiB (the minimum for a non-final part) and part 3 is the
    smaller tail that is only allowed as the highest-numbered part; all three
    stage successfully and report the MD5 ETag of their body.

    Validates: Requirements 10.2
    """
    assert round_trip.upload_part_statuses == (200, 200, 200)
    expected = tuple(
        f'"{hashlib.md5(body).hexdigest()}"' for body in _PART_BODIES
    )
    assert round_trip.part_etags == expected


def test_complete_returns_composite_etag(round_trip: _MultipartResult) -> None:
    """Complete returns the composite ETag ``<md5-of-part-md5s>-<count>``.

    The returned ETag ends in ``-3`` for a three-part upload and its hex prefix
    is the MD5 of the three raw part-MD5 digests concatenated in ascending
    part-number order (Requirement 10.9), recomputed independently here.

    Validates: Requirements 10.9
    """
    part_md5_hex = tuple(hashlib.md5(body).hexdigest() for body in _PART_BODIES)
    expected_etag = _expected_composite_etag(part_md5_hex)

    # S3 returns the ETag quoted; the composite value inside the quotes carries
    # the ``-<part-count>`` suffix and the md5-of-part-md5s prefix.
    unquoted = round_trip.complete_etag.strip('"')
    assert unquoted.endswith("-3")
    assert unquoted == expected_etag
    assert round_trip.complete_etag == f'"{expected_etag}"'


def test_get_returns_concatenated_parts(round_trip: _MultipartResult) -> None:
    """GetObject returns the three parts concatenated in ascending order.

    Reading the completed object back downloads the assembled blob's chunk
    documents from the channel and unseals them, proving CompleteMultipartUpload
    produced a durable, readable object whose bytes are exactly the three parts
    stitched in part-number order (Requirements 10.4, 10.9).

    Validates: Requirements 10.4
    """
    assert round_trip.get_status == 200
    assert round_trip.get_body == b"".join(_PART_BODIES)
