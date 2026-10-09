# Implementation Plan: TGOBJECTS3

## Overview

Implementation follows the four milestones in the design: M0 (feasibility benchmarks), M1 (storage format + recovery CLI), M2 (single-node S3 gateway with full workflow matrix), M3 (hardening + GC + soak), and M4 (console UI). Each milestone is self-contained and produces a testable artifact before the next begins.

Repository layout:

```
tgobjects3/
├── gateway/
│   └── app/
│       ├── format/        # pure codecs (record, manifest, commit, tombstone, sigv4)
│       ├── s3/            # S3 API layer (router, sigv4, xml_codecs, multipart, checksums)
│       ├── admin/         # Admin API + observability
│       ├── storage/       # DB-layer helpers (generations, blobs, heads, staging)
│       ├── telegram/      # TelegramPort protocol + Telethon adapter
│       ├── cache/         # Record cache
│       ├── jobs/          # Upload worker, checkpoint worker, GC worker
│       └── db/            # SQLAlchemy models, Alembic migrations
├── cli/                   # rebuild / recovery CLI
├── tests/
│   ├── unit/              # hypothesis PBT + example-based
│   ├── integration/       # testcontainers (PostgreSQL) + Telegram stub
│   └── workflow_matrix/   # pinned-client S3 compatibility tests
├── console/               # React + TypeScript + Vite SPA
└── deploy/                # docker-compose.yml, Dockerfile, nginx config
```

---

## Tasks

---

### M0 — Feasibility

- [x] 1. Set up repository skeleton and development tooling
  - Create the directory tree above with `__init__.py` stubs in every Python package
  - Add `pyproject.toml` (Python 3.12+, `[project.optional-dependencies]` groups: `dev`, `test`)
  - Pin direct dependencies: `fastapi`, `uvicorn[standard]`, `uvloop`, `pydantic>=2`, `telethon`, `cryptg`, `cryptography`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `awscrt`, `msgpack`, `hypothesis`, `pytest`, `pytest-asyncio`, `testcontainers[postgres]`
  - Add `Makefile` targets: `lint`, `test-unit`, `test-integration`, `typecheck`
  - Configure `ruff`, `mypy` (strict), and `pytest` in `pyproject.toml`
  - _Requirements: 1.1_

- [x] 2. Implement M0 benchmark harness
  - [x] 2.1 Create `cli/benchmark.py` entry point
    - Accept CLI args: `--channel-id`, `--duration` (seconds, default 60), `--chunk-records`, `--concurrency`
    - Use Telethon directly (no TelegramPort abstraction yet) to send and receive documents
    - Measure: sustained upload rate (bytes/s), download rate (bytes/s), cold TTFB (ms), flood-onset concurrency
    - Measure: file-reference refresh latency (ms), API call count, channel message scan rate (messages/s), each averaged over ≥ 10 iterations
    - Emit results as machine-readable JSON report to stdout
    - _Requirements: 21.1, 21.2_
  - [x] 2.2 Write unit tests for benchmark metric aggregation
    - Test averaging over 10 iterations
    - Test flood-onset detection logic
    - _Requirements: 21.1_

- [x] 3. Checkpoint — Record M0 results; set CHUNK_RECORDS, UPLOAD_CONCURRENCY, READ_AHEAD in pyproject defaults
  - After running the benchmark manually, update the `[tool.tgobjects3]` section with the chosen defaults before proceeding to M1.
  - _Requirements: 21.3_

---

### M1 — Storage Format

- [x] 4. Define core data types and PostgreSQL schema
  - [x] 4.1 Create SQLAlchemy 2 async models in `gateway/app/db/models.py`
    - Tables: `buckets`, `object_heads`, `generations`, `blobs`, `chunks`, `upload_jobs`, `multipart_uploads`, `multipart_parts`, `access_keys`, `checkpoints`, `access_stats`
    - Match every column from the ER diagram in the design (types, nullability, constraints)
    - Add composite unique index on `blobs(bucket_id, sha256)`; add `pg_advisory_lock` helper
    - _Requirements: 4.2, 6.9, 6.10_
  - [x] 4.2 Write Alembic initial migration
    - `alembic revision --autogenerate -m "initial_schema"`
    - Add FK constraints and indexes from the design explicitly (do not rely solely on autogenerate)
    - _Requirements: 1.4_
  - [x] 4.3 Write example-based tests for schema migration
    - Use `testcontainers[postgres]` to spin up PostgreSQL 17 and run `alembic upgrade head`
    - Verify all tables exist with correct column types
    - _Requirements: 1.4_

- [x] 5. Implement the format layer — record codec
  - [x] 5.1 Implement `gateway/app/format/record_codec.py`
    - `seal(plaintext: bytes, key: bytes, blob_id: bytes, record_index: int, total_plaintext: int, is_last: bool) -> bytes`
    - Nonce = `record_index` as 12-byte big-endian zero-padded integer
    - AAD = `b'\x01' + blob_id + record_index.to_bytes(4,'big') + total_plaintext.to_bytes(8,'big') + (b'\x01' if is_last else b'\x00')`
    - Use `cryptography` library `AESGCM`; output = nonce(12) + ciphertext(len(plaintext)) + tag(16)
    - Maximum plaintext: 1,048,560 bytes; ciphertext block (excl. nonce) = 1,048,576 bytes
    - `unseal(ciphertext: bytes, key: bytes, blob_id: bytes, record_index: int, total_plaintext: int, is_last: bool) -> bytes`
    - Raise `RecordAuthError` on GCM tag mismatch; never return partial plaintext
    - _Requirements: 13.5, 13.6, 13.7, 22.1, 22.2_
  - [x] 5.2 Write property test — Property 1: Record codec round-trip
    - **Property 1: Record codec round-trip**
    - **Validates: Requirements 22.2**
    - Use `hypothesis.strategies.binary(min_size=1, max_size=1_048_560)` for plaintext; `binary(min_size=32, max_size=32)` for key; `binary(min_size=16, max_size=16)` for blob_id
    - Assert `unseal(seal(pt, ...)) == pt` for all generated inputs
    - `settings(max_examples=500)`
    - _Requirements: 22.2_
  - [x] 5.3 Write property test — Property 2: Record ciphertext length invariant
    - **Property 2: Record ciphertext length invariant**
    - **Validates: Requirements 22.1**
    - Generate exactly 1,048,560-byte plaintexts; assert `len(seal(...)) - 12 == 1_048_576`
    - `settings(max_examples=200)`
    - _Requirements: 22.1_

- [x] 6. Implement the format layer — key hierarchy and key wrap
  - [x] 6.1 Implement `gateway/app/format/key_wrap.py`
    - `derive_root_key(master_key: str) -> bytes` — HKDF-SHA256, info=`b"tgobjects3-v1-root"`, 32-byte output
    - `wrap(kek: bytes, plaintext_key: bytes) -> bytes` — AES Key Wrap RFC 3394 via `cryptography`
    - `unwrap(kek: bytes, wrapped_key: bytes) -> bytes` — raises `KeyUnwrapError` on failure
    - _Requirements: 13.1, 13.2, 13.3, 13.4_
  - [x] 6.2 Write property test — Property 10: AES Key Wrap round-trip
    - **Property 10: AES Key Wrap round-trip**
    - **Validates: Requirements 13.3, 13.4**
    - Generate random 32-byte KEK and 32-byte plaintext key; assert `unwrap(kek, wrap(kek, key)) == key`
    - _Requirements: 13.3, 13.4_

- [x] 7. Implement the format layer — blob manifest and commit record codecs
  - [x] 7.1 Define Pydantic v2 models in `gateway/app/format/types.py`
    - `BlobManifest`: `format_version`, `blob_id`, `bucket_id`, `sha256`, `size`, `chunk_records`, `chunk_message_ids`, `key_wrapped`
    - `CommitRecord`: `format_version`, `generation_id`, `bucket_id`, `key`, `generation_number`, `blob_manifest_message_id`, `etag`, `checksums`, `content_type`, `user_meta`, `last_modified`
    - `Tombstone`: `format_version`, `generation_id`, `bucket_id`, `key`, `generation_number`
    - `BucketManifest`: `format_version`, `bucket_name`, `kek_wrapped`, `kek_version`
    - `Checkpoint`: `format_version`, `pg_dump_enc`, `high_water`
    - All serialise to/from msgpack via `blob_manifest.serialise/deserialise`, `commit_record.serialise/deserialise`, etc.
    - Embed `format_version = 1` as a class constant; reject any deserialised message where `format_version != 1`
    - _Requirements: 16.1, 16.2, 22.3, 22.4, 22.5, 22.6_
  - [x] 7.2 Implement `gateway/app/format/blob_manifest.py`, `commit_record.py`, `tombstone.py`, `bucket_manifest.py`, `checkpoint.py`
    - Each module exposes `serialise(obj) -> bytes` and `deserialise(data: bytes) -> obj`
    - Raise `FormatVersionError` when `format_version != 1`
    - _Requirements: 16.1, 16.2, 22.3, 22.4, 22.5, 22.6_
  - [x] 7.3 Write property test — Property 3: CommitRecord serialisation round-trip
    - **Property 3: CommitRecord serialisation round-trip**
    - **Validates: Requirements 22.3, 22.4**
    - Build composite strategy covering all CommitRecord field types; assert field-by-field equality after serialise→deserialise round-trip
    - _Requirements: 22.3, 22.4_
  - [x] 7.4 Write property test — Property 4: BlobManifest serialisation round-trip
    - **Property 4: BlobManifest serialisation round-trip**
    - **Validates: Requirements 22.5, 22.6**
    - Build composite strategy covering all BlobManifest field types; assert field-by-field equality after round-trip
    - _Requirements: 22.5, 22.6_

- [x] 8. Implement the format layer — SigV4 parser
  - [x] 8.1 Implement `gateway/app/format/sigv4_parser.py`
    - `parse(authorization: str) -> ParseResult` where `ParseResult` is a dataclass/Pydantic model with `algorithm`, `credential`, `signed_headers`, `signature` fields
    - On missing field or malformed format: return `ParseError(kind: str, position: str)` — never raise unhandled exception
    - `format_header(result: ParseResult) -> str` — reconstruct the `Authorization` header from parsed fields
    - _Requirements: 22.7, 22.8, 22.9_
  - [x] 8.2 Write property test — Property 5: SigV4 Authorization header round-trip
    - **Property 5: SigV4 Authorization header round-trip**
    - **Validates: Requirements 22.7, 22.8**
    - Build `sigv4_auth_header()` strategy generating well-formed headers; assert `format_header(parse(h))` produces identical field values
    - _Requirements: 22.7, 22.8_
  - [x] 8.3 Write property test — Property 6: SigV4 parser rejects malformed headers
    - **Property 6: SigV4 parser rejects malformed headers**
    - **Validates: Requirements 22.9**
    - Generate headers with at least one required field removed or corrupted; assert result is `ParseError` with `kind` and `position` set; assert no unhandled exception is raised
    - _Requirements: 22.9_

- [x] 9. Checkpoint — M1 format layer complete; run all format property tests before proceeding
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement the TelegramPort interface and Telethon adapter
  - [x] 10.1 Define `TelegramPort` Protocol in `gateway/app/telegram/port.py`
    - Methods: `send_document`, `get_messages`, `download_document`, `create_channel`, `delete_channel`, `get_session_state`, `is_connected`
    - Signatures exactly as in the design document
    - _Requirements: 3.4_
  - [x] 10.2 Implement `TelethonAdapter` in `gateway/app/telegram/adapter.py`
    - Wrap Telethon 1.x + cryptg; implement all `TelegramPort` methods
    - Store exactly one `TelegramClient` instance; expose `connect()` and `disconnect()` coroutines
    - Raise typed exceptions (`TelegramUnavailableError`, `FloodWaitError`) from Telethon errors
    - _Requirements: 3.1, 3.4_
  - [x] 10.3 Implement reconnection logic in `gateway/app/telegram/session.py`
    - Detect disconnect within 30 s; exponential backoff from 1 s doubling up to configurable max (default 60 s, range 5–300 s)
    - While reconnecting, raise `TelegramUnavailableError` for all callers
    - _Requirements: 3.2, 3.3_
  - [x] 10.4 Write unit tests for reconnection state machine
    - Use a fake `TelegramPort` that injects disconnect events
    - Assert backoff intervals are within spec; assert callers receive `TelegramUnavailableError`
    - _Requirements: 3.2, 3.3_

- [x] 11. Implement the Recovery CLI (`cli/rebuild.py`)
  - [x] 11.1 Implement channel scanner and message parser
    - Accept `--channel-id`, `--master-key`, `--db-url`, `--system-channel-id` as CLI args
    - Scan messages in ascending `message_id` order using `TelegramPort`
    - Detect Checkpoint with highest `message_id`; use it as start point for subsequent scan
    - Decrypt `BlobManifest` and `CommitRecord` using KEKs from `BucketManifest` messages found in the same channel
    - _Requirements: 15.1, 15.2, 15.3, 15.7_
  - [x] 11.2 Implement index reconstruction
    - For each valid `CommitRecord`: insert generation row with status `COMMITTED` into PostgreSQL; upsert `object_heads`
    - For each valid `Tombstone`: mark the referenced generation `DELETED`
    - Skip and count unreadable/undecryptable messages; detect and report gap ranges
    - _Requirements: 14.4, 15.1, 15.4_
  - [x] 11.3 Implement CLI output and error reporting
    - On completion: print reconstructed generation count, skipped message count, gap ranges
    - If a required KEK / BucketManifest is absent: treat all CommitRecords in that channel as unreadable
    - If format_version ≠ 1: reject message, log Telegram message ID, channel, and version found
    - _Requirements: 15.4, 16.3, 16.4_
  - [x] 11.4 Write integration test for full recovery round-trip
    - Use testcontainers PostgreSQL + Telegram stub; insert CommitRecords directly, wipe DB, run rebuild, verify `object_heads`
    - _Requirements: 15.1, 15.4_

- [x] 12. Checkpoint — M1 complete; format frozen; recovery CLI tested end-to-end
  - Ensure all tests pass, ask the user if questions arise.

---

### M2 — Single Node S3 Gateway

- [x] 13. Implement database session factory and startup/shutdown lifecycle
  - [x] 13.1 Create `gateway/app/db/session.py`
    - `AsyncSessionFactory` using SQLAlchemy 2 async + asyncpg
    - Connection pool config: min=2, max=10, timeout=30 s
    - Startup: create engine, run `alembic upgrade head`, run staging crash-recovery scan
    - Shutdown: gracefully close all connections
    - _Requirements: 1.4, 1.5, 1.6_
  - [x] 13.2 Write example-based tests for startup retry logic
    - Simulate PostgreSQL unavailable for 9 attempts then available on attempt 10; assert gateway proceeds
    - Simulate 10 consecutive failures; assert gateway exits non-zero
    - _Requirements: 1.5_

- [x] 14. Implement gateway configuration and startup validation
  - [x] 14.1 Create `gateway/app/config.py` with Pydantic v2 `Settings` model
    - All configuration variables from the design table; use `pydantic_settings.BaseSettings`
    - Validate ranges (e.g. `CHECKPOINT_INTERVAL` 60–86400, `UPLOAD_CONCURRENCY` ≥ 1)
    - On missing required variable (`TGOS3_MASTER_KEY`, `DATABASE_URL`): collect all missing vars, emit each on its own line to stderr, exit non-zero before binding any port
    - _Requirements: 1.3_
  - [x] 14.2 Write example-based tests for startup validation
    - Test: missing `TGOS3_MASTER_KEY` → stderr lists that variable; missing both required vars → both listed
    - _Requirements: 1.3_

- [x] 15. Implement staging area
  - [x] 15.1 Implement `gateway/app/storage/staging.py`
    - `StagingReservation`: in-memory atomic counter using `asyncio.Lock`
    - `reserve(size: int) -> StagingHandle | None` — returns None when reservation would exceed `STAGING_MAX_SIZE`
    - `release(handle: StagingHandle)` — decrements counter
    - `fsync_file(path: Path)` + `fsync_dir(path: Path)` helpers
    - On startup: scan staging directory; quarantine files with no corresponding generation row (move to `/data/staging/quarantine/`)
    - _Requirements: 6.3, 6.4, 14.2_
  - [x] 15.2 Write property test — Property 9: Staging reservation prevents overflow
    - **Property 9: Staging reservation prevents overflow**
    - **Validates: Requirements 6.3, 6.4**
    - Use `hypothesis` to generate sequences of concurrent reservation requests; assert total in-progress reservations never exceed `STAGING_MAX_SIZE`; assert any request that would overflow returns None (translated to HTTP 507)
    - _Requirements: 6.3, 6.4_

- [x] 16. Implement storage layer helpers
  - [x] 16.1 Implement `gateway/app/storage/generations.py`
    - `insert_staged(...)` — insert generation row + upload_job in one transaction
    - `advance_committed(generation_id, commit_message_id, blob_id, ...)` — atomic state advance + `object_heads` upsert
    - State transition helpers: `mark_uploading`, `mark_retry_wait`, `mark_failed`, `mark_superseded`
    - _Requirements: 6.9, 6.13, 14.1_
  - [x] 16.2 Implement `gateway/app/storage/blobs.py`
    - `claim_or_wait(bucket_id, sha256) -> BlobClaimResult` — dedup check with `pg_advisory_lock(sha256_as_int64)`
    - `insert_blob(...)` — insert new blob row
    - `increment_refcount` / `decrement_refcount`
    - _Requirements: 6.10, 6.11_
  - [x] 16.3 Implement `gateway/app/storage/heads.py`
    - `get_head(bucket_id, key) -> generation_id | None`
    - `advance_head(bucket_id, key, generation_id)` — upsert in one statement
    - _Requirements: 6.13_

- [x] 17. Implement the Upload Worker
  - [x] 17.1 Implement `gateway/app/jobs/upload_worker.py`
    - Claim upload_job with `SELECT ... FOR UPDATE SKIP LOCKED`
    - Dedup check: if COMMITTED blob with same SHA-256 in bucket → reuse blob, mark generation COMMITTED, skip Telegram upload
    - Concurrent same-SHA-256 path: `pg_advisory_lock`; one worker proceeds, others wait
    - Normal upload path: seal records in thread pool (`asyncio.to_thread`), send chunk documents in parallel up to `UPLOAD_CONCURRENCY`, send BlobManifest, send CommitRecord
    - On Telegram error: exponential backoff 1 s doubling, respect `FLOOD_WAIT`, increment attempt counter; after max attempts → FAILED
    - _Requirements: 6.10, 6.11, 6.12, 6.13, 6.14, 14.1_
  - [x] 17.2 Write property test — Property 12: Dedup does not re-upload identical content
    - **Property 12: Dedup does not re-upload identical content**
    - **Validates: Requirements 6.10**
    - Use a fake TelegramPort that counts `send_document` calls; assert second identical PUT results in 0 new `send_document` calls and the generation references the same `blob_id`
    - _Requirements: 6.10_
  - [x] 17.3 Write unit tests for Upload Worker retry/backoff
    - Inject Telegram errors; assert backoff intervals; assert FAILED after max attempts; assert STAGED file retained
    - _Requirements: 6.14_

- [x] 18. Implement range mapping
  - [x] 18.1 Implement `gateway/app/format/range_mapper.py`
    - `records_for_range(start: int, end: int, record_size: int = 1_048_560) -> list[int]`
    - Returns contiguous list of record indices covering `[start, end]`; minimal set (removing any one record leaves at least one byte uncovered)
    - `chunk_for_record(record_index: int, chunk_records: int) -> int`
    - `intra_chunk_offset(record_index: int, chunk_records: int) -> int`
    - _Requirements: 8.2_
  - [x] 18.2 Write property test — Property 11: Range request maps to minimal record set
    - **Property 11: Range request maps to minimal record set**
    - **Validates: Requirements 8.2**
    - Generate `(object_size, start, end)` where `0 <= start <= end < object_size`; assert result is contiguous; assert removing any record leaves at least one requested byte uncovered
    - _Requirements: 8.2_

- [x] 19. Implement the Record Cache
  - [x] 19.1 Implement `gateway/app/cache/store.py`
    - Two-level path: `/data/cache/{blob_id[:2]}/{blob_id}/{record_index}`
    - `get(blob_id, record_index) -> bytes | None` — return cached bytes or None on miss
    - `put(blob_id, record_index, data: bytes)` — write + update `access_stats`
    - Dedup concurrent misses via `asyncio.Event` per `(blob_id, record_index)` key
    - Admission policy: ≤ 64 MiB objects admit all records on first access; > 64 MiB admit in 8-record blocks on second access within one half-life
    - Optional encryption: when `CACHE_ENCRYPT=true`, AES-256-GCM using HKDF-derived key per record
    - _Requirements: 12.1, 12.5, 12.6, 12.7_
  - [x] 19.2 Implement cache ranking and eviction in `gateway/app/cache/eviction.py`
    - Score: `s(t) = s_prev × 2^(−Δt / H) + 1`; priority: `s(t) / size^α`
    - Background eviction task: wake when cache reaches `CACHE_HIGH_WATERMARK` (90%); evict lowest-priority records until `CACHE_LOW_WATERMARK` (80%)
    - Tie-breaking: evict least recently accessed
    - _Requirements: 12.2, 12.3, 12.4_
  - [x] 19.3 Write property test — Property 13: Cache ranking score decays over time and scales with access frequency
    - **Property 13: Cache ranking score decays over time and scales with access frequency**
    - **Validates: Requirements 12.2**
    - Generate `(s_prev, delta_t, H, size, alpha)` with valid domains; assert decayed score < original score; assert higher-score record has higher priority than lower-score record of equal size and equal elapsed time
    - _Requirements: 12.2_
  - [x] 19.4 Write example-based tests for cache eviction watermarks
    - Fill cache to exactly 90%; assert eviction triggers; assert size drops to 80%
    - _Requirements: 12.3_

- [x] 20. Implement XML codecs and checksum helpers
  - [x] 20.1 Implement `gateway/app/s3/xml_codecs.py`
    - Typed serialisers/deserialisers for: `ListBucketsResponse`, `ListObjectsV2Response`, `ListObjectsResponse` (v1), `CompleteMultipartUploadResponse`, `InitiateMultipartUploadResponse`, `ListPartsResponse`, `ListMultipartUploadsResponse`, `DeleteObjectsRequest`, `DeleteObjectsResponse`, `Error` (S3 error XML), `GetBucketLocationResponse`
    - Use `lxml` or stdlib `xml.etree.ElementTree`; never use string formatting for XML output
    - _Requirements: 4.1, 5.1, 5.2, 5.3, 10.1, 10.7, 10.8, 10.9_
  - [x] 20.2 Implement `gateway/app/s3/checksums.py`
    - `compute(algorithm: str, data: bytes) -> str` — supports CRC32, CRC32C, CRC64NVME (via `awscrt`), SHA-1, SHA-256
    - Raise `UnsupportedChecksumAlgorithm` for unknown algorithms
    - _Requirements: 6.6, 7.10, 7.11_

- [x] 21. Implement SigV4 authentication
  - [x] 21.1 Implement `gateway/app/s3/sigv4.py`
    - `verify_header_auth(request, secret_provider) -> AuthResult` — header-based SigV4 verification
    - `verify_presigned_auth(request, secret_provider) -> AuthResult` — query-string presigned auth
    - Timestamp window: ±15 minutes; return `RequestTimeTooSkewed` if outside
    - Bcrypt verification of stored secret hashes
    - `verify_streaming_chunks(body_stream, seed_signature, signing_key)` — verify each chunk signature for `STREAMING-AWS4-HMAC-SHA256-PAYLOAD`
    - Accept `UNSIGNED-PAYLOAD` without body hash check
    - Verify trailer signature for `STREAMING-UNSIGNED-PAYLOAD-TRAILER`
    - _Requirements: 6.1, 6.2, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 17.1, 17.2, 17.3, 17.4_
  - [x] 21.2 Write example-based tests for SigV4 boundary conditions
    - Timestamp at exactly ±15 min: accepted; at ±15 min + 1 s: rejected with 403
    - UNSIGNED-PAYLOAD: accepted without hash
    - Signature mismatch: 403 returned; no body bytes written
    - _Requirements: 17.1, 17.3, 17.4_

- [x] 22. Implement S3 router and PutObject
  - [x] 22.1 Implement `gateway/app/s3/router.py`
    - FastAPI `APIRouter`; dispatch by HTTP method + query parameter presence (`?uploads`, `?partNumber`, `?delete`, `?location`, etc.)
    - Mount under `/` on the S3 port; virtual-hosted and path-style bucket addressing
    - _Requirements: 4.1, 6.1_
  - [x] 22.2 Implement PutObject handler in `gateway/app/s3/operations/put_object.py`
    - Verify SigV4; reserve staging; stream body; compute MD5 + SHA-256 + client checksum; verify checksums; fsync + rename; insert STAGED generation + upload_job; block until COMMITTED (sync) or return 200 at STAGED (async bucket)
    - Return 507 if staging full; 413 if body exceeds MAX_OBJECT_SIZE; 400 on checksum mismatch; 503 on Telegram timeout
    - _Requirements: 6.1–6.15_
  - [x] 22.3 Write integration test for PutObject sync durability
    - Full PUT → GET round-trip against testcontainers PostgreSQL + Telegram stub; assert 200 returned only after COMMITTED
    - _Requirements: 6.13, 14.5_

- [x] 23. Implement GetObject and ranged reads
  - [x] 23.1 Implement `gateway/app/s3/operations/get_object.py`
    - Resolve head → validate range → map to records → cache lookup → fetch missing records from Telegram (with `READ_AHEAD`) → verify AES-GCM tag per record → stream bytes
    - Return 206 + `Content-Range` for ranged reads; 200 for full-object reads
    - Conditional headers: `If-Match`, `If-None-Match`, `If-Modified-Since`, `If-Unmodified-Since`
    - On GCM tag failure: close connection; do not flush partial bytes; return 500 if no bytes flushed yet
    - Update `access_stats` for each record served
    - _Requirements: 8.1–8.11_
  - [x] 23.2 Write integration test for ranged reads
    - Upload 10 MiB object; request byte range spanning two records; assert 206 + correct bytes
    - _Requirements: 8.2, 8.3_

- [x] 24. Implement HeadObject, DeleteObject, and DeleteObjects
  - [x] 24.1 Implement HeadObject in `gateway/app/s3/operations/head_object.py`
    - Return 200 with Content-Length, Content-Type, ETag, Last-Modified; 404 if not found
    - _Requirements: 9.1, 9.2_
  - [x] 24.2 Implement DeleteObject in `gateway/app/s3/operations/delete_object.py`
    - Write Tombstone to Telegram; mark generation DELETED; return 204; 500 on Telegram failure (index unchanged)
    - Return 204 silently for non-existent keys
    - _Requirements: 9.3, 9.4, 9.5_
  - [x] 24.3 Implement DeleteObjects (batch) in `gateway/app/s3/operations/delete_objects.py`
    - Process each key independently; return per-key success/error; reject > 1000 keys with 400
    - _Requirements: 9.6, 9.7_

- [x] 25. Implement bucket operations
  - [x] 25.1 Implement `gateway/app/s3/operations/bucket_ops.py`
    - ListBuckets, CreateBucket, HeadBucket, DeleteBucket, GetBucketLocation
    - CreateBucket: create Telegram channel + write BucketManifest + insert `buckets` row in sequence; rollback all on any failure
    - DeleteBucket: reject with 409 if any COMMITTED objects; delete channel + remove row + return 204 on success
    - HeadBucket: 200 if exists, 404 otherwise
    - GetBucketLocation: return `S3_REGION`
    - Reject duplicate CreateBucket with 409
    - _Requirements: 4.1–4.7_
  - [x] 25.2 Write integration tests for bucket CRUD
    - Create, head, delete empty bucket; attempt delete non-empty bucket → 409
    - _Requirements: 4.2, 4.3, 4.7_

- [x] 26. Implement object listing (ListObjectsV2 and ListObjects v1)
  - [x] 26.1 Implement `gateway/app/s3/operations/list_objects.py`
    - Accept `prefix`, `delimiter`, `max-keys` (default 1000, range 1–1000), `continuation-token` / `marker`
    - Delimiter grouping into `CommonPrefixes`; lexicographic order for both Contents and CommonPrefixes; no key in both
    - Pagination with `NextContinuationToken` / `NextMarker`; reject invalid tokens with error
    - Return only COMMITTED generations
    - _Requirements: 5.1–5.5_
  - [x] 26.2 Write property test — Property 7: Object listing excludes non-COMMITTED generations
    - **Property 7: Object listing excludes non-COMMITTED generations**
    - **Validates: Requirements 5.4**
    - Generate `bucket_with_generations()` with mixed states; assert ListObjectsV2 returns only COMMITTED; assert no key duplicates
    - _Requirements: 5.4_
  - [x] 26.3 Write property test — Property 8: ListObjectsV2 delimiter produces disjoint lexicographically-ordered results
    - **Property 8: ListObjectsV2 delimiter produces disjoint lexicographically-ordered results**
    - **Validates: Requirements 5.2**
    - Generate `object_key_set()` with prefix and delimiter; assert Contents ∩ CommonPrefixes = ∅; assert both lists in lexicographic order
    - _Requirements: 5.2_

- [x] 27. Implement multipart upload
  - [x] 27.1 Implement `gateway/app/s3/operations/multipart.py`
    - CreateMultipartUpload → return `UploadId`
    - UploadPart: stage part with fsync + checksum; return ETag; reject invalid/completed `UploadId`; enforce 5 MiB minimum except for final part
    - CompleteMultipartUpload: concatenate parts in ascending order → Upload Worker path; HTTP 200 + whitespace keep-alive + final XML body; reject invalid part lists (missing part, 0 parts)
    - AbortMultipartUpload: delete staged parts + remove record
    - ListParts: return part number, size, ETag per staged part
    - ListMultipartUploads: return `UploadId`, key, initiation timestamp
    - ETag format for completed upload: `<md5-of-concatenated-part-md5s>-<part-count>`
    - _Requirements: 10.1–10.10_
  - [x] 27.2 Write integration test for multipart upload
    - Upload 3 parts, complete, verify ETag format, GET object → correct bytes
    - _Requirements: 10.2, 10.4, 10.9_

- [x] 28. Implement CopyObject
  - [x] 28.1 Implement `gateway/app/s3/operations/copy_object.py`
    - Resolve source head; if SHA-256 matches existing COMMITTED blob in destination bucket → record new generation without Telegram upload
    - Return 404 if source key or destination bucket not found
    - Handle same-bucket, same-key no-op case
    - Return ETag and last-modified of new destination generation
    - _Requirements: 11.1–11.6_

- [x] 29. Checkpoint — M2 core S3 operations complete; run unit + integration tests before workflow matrix
  - Ensure all tests pass, ask the user if questions arise.

- [x] 30. Implement workflow matrix test suite
  - [x] 30.1 Create `tests/workflow_matrix/pin.json` with pinned client versions
    - Record: `aws_cli_v2`, `boto3`, `rclone`, `restic`, `minio_mc` version strings
    - _Requirements: 20.1–20.5_
  - [x] 30.2 Write workflow matrix tests for AWS CLI v2
    - Bucket CRUD, put, get, delete, multipart, `--checksum-algorithm` variants (CRC32, CRC32C, SHA-256)
    - _Requirements: 20.1, 7.10_
  - [x] 30.3 Write workflow matrix tests for boto3
    - Bucket creation/deletion, object put/get/delete, multipart, listing
    - _Requirements: 20.2_
  - [x] 30.4 Write workflow matrix tests for rclone
    - `rclone copy`, `rclone sync`; mount (FUSE, read-only assertions)
    - _Requirements: 20.3_
  - [x] 30.5 Write workflow matrix tests for restic
    - `restic init`, `restic backup`, `restic restore`, `restic snapshots`
    - _Requirements: 20.4_
  - [x] 30.6 Write workflow matrix tests for MinIO mc
    - Bucket and object management operations
    - _Requirements: 20.5_

- [x] 31. Implement checkpoint worker
  - [x] 31.1 Implement `gateway/app/jobs/checkpoint_worker.py`
    - Background task: fire every `CHECKPOINT_INTERVAL` seconds (range 60–86400)
    - Take `pg_dump`; encrypt with AES-256-GCM using HKDF-derived key; build `Checkpoint` msgpack; send to system channel
    - Record highest confirmed `message_id` per bucket channel in `Checkpoint.high_water`
    - Insert row in `checkpoints` table on success
    - On failure: retry up to 3 times at 5-second intervals; if all fail → error log entry; do not interrupt object storage operations
    - _Requirements: 15.5, 15.6_
  - [x] 31.2 Write unit tests for checkpoint write retry
    - Inject Telegram failure on first 2 attempts → succeeds on 3rd; inject failure on all 3 → error logged, operations continue
    - _Requirements: 15.6_

- [x] 32. Wire FastAPI application and Docker packaging
  - [x] 32.1 Create `gateway/app/main.py`
    - `lifespan` context manager: startup (config validation, DB connect + migrate, staging scan, start Upload Worker + Checkpoint Worker tasks, connect Telegram session); shutdown (graceful drain)
    - Mount S3 router on `S3_PORT`; mount Admin router on `ADMIN_PORT`; expose `/metrics`, `/health/live`, `/health/ready` on admin port
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_
  - [x] 32.2 Create multi-stage `Dockerfile` in `deploy/`
    - Stage 1: build Python wheels; Stage 2: slim runtime image; expose ports 9000, 8080
    - _Requirements: 1.1_
  - [x] 32.3 Create `deploy/docker-compose.yml`
    - Three services: `gateway`, `postgres` (image `postgres:17`), `console` (nginx static)
    - Mount `/data` volume for staging and cache; pass all required env vars via `.env.example`
    - _Requirements: 1.2_

- [x] 33. Implement observability endpoints and audit log
  - [x] 33.1 Implement `/health/live`, `/health/ready` in `gateway/app/admin/health.py`
    - `/health/live`: 200 if process running; 503 if non-recoverable
    - `/health/ready`: 503 + body `{"unhealthy": ["postgresql"]}` if DB down; 503 + body `{"unhealthy": ["telegram"]}` if session disconnected
    - _Requirements: 18.2, 18.3, 18.4, 18.7_
  - [x] 33.2 Implement Prometheus metrics endpoint in `gateway/app/admin/metrics.py`
    - Expose at `/metrics`; at minimum: `audit_log_write_failures_total`, `record_auth_failure_total`, `upload_job_state` (labelled by state), `staging_reserved_bytes`, `cache_size_bytes`
    - _Requirements: 18.1_
  - [x] 33.3 Implement audit log middleware in `gateway/app/s3/audit.py`
    - Log per S3 request: timestamp (ISO 8601 UTC), access key ID, operation, bucket, key, HTTP status, latency ms
    - Redact all credential values, encryption keys, session tokens with fixed placeholder `[REDACTED]`
    - On write failure: continue processing; increment `audit_log_write_failures_total`
    - _Requirements: 18.5, 18.6, 18.8_
  - [x] 33.4 Write observability tests
    - `/health/ready` returns 503 body identifying `postgresql` when DB container stopped
    - `/metrics` returns valid Prometheus text format; audit entries have all required fields with credentials redacted
    - _Requirements: 18.3, 18.4, 18.5, 18.6_

- [x] 34. Checkpoint — M2 complete; all workflow matrix clients pass; all tests green
  - Ensure all tests pass, ask the user if questions arise.

---

### M3 — Hardening

- [x] 35. Implement GC worker
  - [x] 35.1 Implement `gateway/app/jobs/gc_worker.py`
    - Orphan sweep: delete staging files and index rows for multipart uploads older than `MULTIPART_EXPIRY` with no CompleteMultipartUpload received
    - Delete sweep: after `GC_GRACE` elapses for SUPERSEDED/DELETED generations with refcount=0: delete Telegram messages, mark generation PURGED in PostgreSQL, retain row for audit
    - _Requirements: 10.10_
  - [x] 35.2 Write integration test for GC worker
    - Stage a multipart upload; let `MULTIPART_EXPIRY` elapse (use short value in test); assert staged parts deleted and record removed
    - Verify Telegram messages purged after `GC_GRACE` for a deleted generation
    - _Requirements: 10.10_

- [x] 36. Implement crash recovery on startup
  - [x] 36.1 Implement crash-recovery scan in `gateway/app/storage/staging.py` (startup path)
    - Scan staging directory at startup
    - Files with no corresponding generation row → quarantine (move to `/data/staging/quarantine/`)
    - Generation rows in STAGED or UPLOADING with no upload_job → re-enqueue upload_job (idempotent insert)
    - Generation rows in STAGED or UPLOADING with existing upload_job → skip
    - _Requirements: 14.2_
  - [x] 36.2 Implement partial-commit-record recovery in `gateway/app/jobs/upload_worker.py`
    - On startup: for each generation where BlobManifest exists in Telegram but no CommitRecord exists: re-attempt CommitRecord write up to 5 times with exponential backoff; on all 5 failures → set `UPLOAD_FAILED`, surface error
    - _Requirements: 14.3_
  - [x] 36.3 Write integration test for crash recovery
    - Kill gateway after BlobManifest written (using fake TelegramPort that halts after manifest); restart; assert generation reaches COMMITTED
    - _Requirements: 14.3_

- [x] 37. Implement master key rotation
  - [x] 37.1 Implement `cli/rotate_master_key.py`
    - Accept `--old-master-key`, `--new-master-key`, `--db-url`
    - Re-wrap all bucket KEKs under new master key; update BucketManifest messages in Telegram one by one
    - Atomicity: retain old wrapping for any manifest not yet updated; report exactly which manifests were and were not updated
    - _Requirements: 13.8, 13.9_
  - [x] 37.2 Write integration test for mid-rotation failure
    - Simulate failure after updating half the buckets; assert unupdated buckets still readable with old key; rerun rotation; assert all buckets updated
    - _Requirements: 13.9_

- [x] 38. Implement fault injection test suite
  - [x] 38.1 Create `tests/integration/fault_injection.py` harness
    - `FaultInjectingTelegramPort` wrapper: configurable failure modes (disconnect after N calls, FLOOD_WAIT after M calls, corrupt message on specific message_id)
    - Test: disconnect during chunk upload → STAGED retained; reconnection resumes upload
    - Test: FLOOD_WAIT injected → backoff respected; upload eventually COMMITTED
    - Test: GCM tag corruption on download → 500 returned; no bytes flushed
    - Test: concurrent uploads of same SHA-256 → exactly one Telegram upload; both generations COMMITTED
    - _Requirements: 6.14, 12.5, 12.6, 13.6, 13.7_
  - [x] 38.2 Write fault injection tests using the harness
    - At least one test for each failure mode listed above
    - _Requirements: 6.14, 13.7_

- [x] 39. Run 500 GB soak test (M3 pass criteria)
  - [x] 39.1 Create soak test script `tests/soak/run_soak.py`
    - Use rclone against a real staging deployment; upload/download 500 GB over 24 hours
    - Pass criteria: zero data integrity errors; no unrecovered FAILED generations; `record_auth_failure_total` counter remains at 0
    - Emit pass/fail verdict as structured JSON output
    - _Requirements: 18.1_

- [x] 40. Checkpoint — M3 complete; soak test passed; all hardening tests green
  - Ensure all tests pass, ask the user if questions arise.

---

### M4 — Console

- [x] 41. Implement Admin API backend
  - [x] 41.1 Implement `gateway/app/admin/router.py` with all admin endpoints
    - `GET /admin/buckets` — list with per-bucket stats (object count, stored bytes, pending-write count)
    - `GET /admin/objects` — prefix-filtered browser with pagination (100 per page)
    - `GET /admin/jobs` — upload job list; `POST /admin/jobs/{id}/retry` — transition FAILED → STAGED
    - `GET/PUT /admin/settings` — runtime config; validate new value ranges; apply within 5 s without restart; reject staging-size reduction below reserved amount
    - `GET /admin/keys`, `POST /admin/keys`, `DELETE /admin/keys/{id}` — access key management
    - CSRF token validation on all state-mutating endpoints (POST, PUT, DELETE, PATCH)
    - HTTP Basic auth for all admin endpoints; reject if no admin password configured at startup
    - _Requirements: 17.5, 17.6, 17.7, 18.1, 19.1–19.7_
  - [x] 41.2 Write unit tests for admin API
    - Settings update: valid change applied; staging reduction below reserved → rejected
    - CSRF mismatch → 403; valid CSRF → accepted
    - _Requirements: 17.7, 19.6, 19.7_

- [x] 42. Implement the guided setup wizard API
  - [x] 42.1 Implement `POST /admin/setup/*` wizard steps
    - Steps: collect Telegram API credentials, channel assignments, staging/cache config, master key
    - Handle 2FA: prompt for code; retry up to 3 attempts; abort on 4th failure with error; return operator to credential entry step
    - On success: generate Recovery Kit (system channel ID, bucket channel IDs, master key, checkpoint metadata) as downloadable bundle; create first S3 access key pair; display secret key exactly once
    - On channel adoption: detect existing BucketManifest messages; offer adopt vs reinitialise; warn on reinitialise
    - _Requirements: 2.1–2.7_
  - [x] 42.2 Write unit tests for setup wizard edge cases
    - 2FA: 3rd failed attempt → abort + error; 4th attempt → return to credential entry
    - Channel with existing manifests: adoption and reinitialise paths
    - _Requirements: 2.3, 2.4, 2.7_

- [x] 43. Scaffold Console SPA
  - [x] 43.1 Create Vite + React + TypeScript project in `console/`
    - Dependencies: `@tanstack/react-query`, `@radix-ui/react-*`, `axios`
    - Configure Vite proxy: `/admin/*` → gateway admin port
    - Add `npm run build` producing `console/dist/` static files served by nginx in `deploy/`
    - _Requirements: 19.1_

- [x] 44. Implement Console UI screens
  - [x] 44.1 Implement Bucket List screen
    - Display per-bucket: object count, stored bytes (human-readable B/KB/MB/GB/TB), pending-write count
    - Auto-refresh every ≤ 30 seconds via TanStack Query `refetchInterval`
    - _Requirements: 19.1_
  - [x] 44.2 Implement Object Browser screen
    - Prefix-based navigation; collapse sub-prefixes into folder entries; pagination at 100 objects/page for > 1000 objects
    - _Requirements: 19.2_
  - [x] 44.3 Implement Upload Jobs screen
    - List jobs with status badge; Retry button for FAILED jobs → POST retry endpoint → status transitions to STAGED
    - _Requirements: 19.3_
  - [x] 44.4 Implement Recovery Status screen
    - Display last completed Checkpoint timestamp (UTC, second precision) and last recovery drill outcome; display "None" if no data yet
    - _Requirements: 19.4_
  - [x] 44.5 Implement Settings screen
    - Editable fields: staging size, cache size, cache half-life, read-ahead count, upload concurrency
    - On submit: call `PUT /admin/settings`; display validation error if staging size reduction below reserved amount
    - _Requirements: 19.5, 19.6, 19.7_

- [x] 45. Write Playwright end-to-end tests for Console
  - [x] 45.1 Write Playwright tests for Console critical paths
    - Setup wizard: complete happy path; 2FA retry boundary
    - Bucket list refreshes; object browser navigates prefixes; retry FAILED job; settings validation error
    - _Requirements: 2.1–2.7, 19.1–19.7_

- [x] 46. Final checkpoint — M4 complete; full system tested end-to-end
  - Ensure all tests pass and the Playwright suite passes. Ask the user if questions arise.

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; the implementation agent must NOT implement them unless explicitly directed.
- Each task references the specific requirements clauses and design properties it satisfies.
- Milestone checkpoints ensure incremental validation — do not proceed to the next milestone until the checkpoint passes.
- Property-based tests validate universal correctness properties (Properties 1–13 from the design); unit tests validate specific examples and edge cases.
- The `FaultInjectingTelegramPort` pattern in M3 enables deterministic fault injection without a real Telegram account.
- Soak test (task 39) requires a real staging deployment and cannot be automated in CI — run manually and record results before cutting a v1.0 tag.
- The `TGOS3_MASTER_KEY` environment variable is the root secret; never log or expose it. All tests use randomly-generated keys scoped to the test process.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["2.1"] },
    { "id": 1, "tasks": ["2.2", "4.1"] },
    { "id": 2, "tasks": ["4.2", "5.1", "6.1"] },
    { "id": 3, "tasks": ["4.3", "5.2", "5.3", "6.2", "7.1"] },
    { "id": 4, "tasks": ["7.2", "8.1"] },
    { "id": 5, "tasks": ["7.3", "7.4", "8.2", "8.3", "10.1"] },
    { "id": 6, "tasks": ["10.2", "11.1"] },
    { "id": 7, "tasks": ["10.3", "11.2"] },
    { "id": 8, "tasks": ["10.4", "11.3", "13.1"] },
    { "id": 9, "tasks": ["11.4", "13.2", "14.1"] },
    { "id": 10, "tasks": ["14.2", "15.1", "16.1", "16.2", "16.3"] },
    { "id": 11, "tasks": ["15.2", "17.1", "18.1", "20.1", "20.2"] },
    { "id": 12, "tasks": ["17.2", "17.3", "18.2", "21.1"] },
    { "id": 13, "tasks": ["19.1", "21.2", "22.1"] },
    { "id": 14, "tasks": ["19.2", "22.2"] },
    { "id": 15, "tasks": ["19.3", "19.4", "22.3", "23.1"] },
    { "id": 16, "tasks": ["23.2", "24.1", "24.2", "24.3", "25.1"] },
    { "id": 17, "tasks": ["25.2", "26.1"] },
    { "id": 18, "tasks": ["26.2", "26.3", "27.1", "28.1"] },
    { "id": 19, "tasks": ["27.2", "30.1", "31.1"] },
    { "id": 20, "tasks": ["30.2", "30.3", "30.4", "30.5", "30.6", "31.2", "32.1"] },
    { "id": 21, "tasks": ["32.2", "32.3", "33.1", "33.2", "33.3"] },
    { "id": 22, "tasks": ["33.4", "35.1", "36.1"] },
    { "id": 23, "tasks": ["35.2", "36.2", "37.1"] },
    { "id": 24, "tasks": ["36.3", "37.2", "38.1"] },
    { "id": 25, "tasks": ["38.2", "39.1"] },
    { "id": 26, "tasks": ["41.1"] },
    { "id": 27, "tasks": ["41.2", "42.1"] },
    { "id": 28, "tasks": ["42.2", "43.1"] },
    { "id": 29, "tasks": ["44.1", "44.2", "44.3", "44.4", "44.5"] },
    { "id": 30, "tasks": ["45.1"] }
  ]
}
```
