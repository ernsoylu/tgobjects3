# Requirements Document

## Introduction

TGOBJECTS3 is a self-hosted, Docker-deployed, S3-compatible archive tier that uses a Telegram account as its durable backing store. Each bucket is a private Telegram channel; object data is sealed into encrypted document messages; every write is finalised with a commit record in the same channel. PostgreSQL serves as a rebuildable index, a local volume stages uploads and caches hot records.

The system targets single-owner or small-team use-cases: second copies of backups, media archives and rclone targets, accessed through standard S3 tooling (AWS CLI v2, boto3, rclone, restic, MinIO mc). It is positioned as a cold, unlimited, free-to-store secondary archive — not a primary hot store and not the only copy of irreplaceable data.

---

## Glossary

- **Gateway**: The v1 container that hosts the S3 API, admin API, upload worker, record cache and the single Telegram session.
- **Bucket**: An S3 namespace backed by a private Telegram channel.
- **Generation**: One logical write to a key — a PutObject, a completed multipart upload, or a delete — identified by an incrementing integer.
- **Blob**: The encrypted, deduplicated binary payload of one generation, stored as one or more Telegram document messages.
- **Record**: One AES-256-GCM ciphertext unit of exactly 1 MiB (1,048,576 bytes) covering 1,048,560 plaintext bytes with a 16-byte authentication tag.
- **Blob Manifest**: A Telegram message of type `tgo3-b-<blob>` carrying the blob size, SHA-256 digest, record layout, chunk message IDs and the wrapped blob key.
- **Commit Record**: A Telegram message of type `tgo3-c-<generation>` carrying bucket, key, generation number, blob manifest message ID, S3 metadata, ETag and checksums.
- **Tombstone**: A Telegram message of type `tgo3-t-<generation>` written on DeleteObject.
- **Bucket Manifest**: A Telegram message of type `tgo3-k-<version>` carrying the S3 bucket name and the wrapped KEK.
- **Checkpoint**: A Telegram message of type `tgo3-p-<time>` in the system channel, carrying an encrypted pg_dump and the highest included message ID per channel.
- **KEK (Key Encryption Key)**: A per-bucket symmetric key wrapped by the master key.
- **Blob Key**: A random 256-bit AES key, per blob, wrapped by the bucket KEK.
- **Master Key**: The root symmetric key supplied via `TGOS3_MASTER_KEY`; wraps all bucket KEKs.
- **Staging Area**: A local filesystem volume that buffers upload bodies before they are transmitted to Telegram.
- **Record Cache**: An on-disk LRU-like cache of downloaded records, ranked by decayed access frequency.
- **SigV4**: AWS Signature Version 4 authentication used by all S3 clients.
- **ETag**: The S3 entity tag for an object; equals the MD5 hex digest for single-part objects or the multipart composite form.
- **Recovery Kit**: A self-contained bundle sufficient to rebuild the PostgreSQL index from Telegram alone, distributed to the operator at setup time.
- **TelegramPort**: The internal abstraction interface over Telethon used by all Gateway components.
- **Dedup**: Content-addressed deduplication of blobs within a bucket, keyed by SHA-256.
- **Workflow Matrix**: The set of named S3 client integration tests that must pass before a release: AWS CLI v2, boto3, rclone (copy, sync, mount), restic and MinIO mc, each at a pinned version.
- **Upload Worker**: An internal Gateway background task that claims staged blobs and transmits them to Telegram.
- **GC Worker**: An internal Gateway background task that removes staging files and index rows for orphaned or expired multipart uploads and deleted generations after the GC grace period.

---

## Requirements

---

### Requirement 1: Docker Compose Deployment

**User Story:** As an operator, I want to deploy the entire system with a single `docker compose up` command, so that I can run TGOBJECTS3 without manual service configuration.

#### Acceptance Criteria

1. THE Gateway SHALL be packaged as a multi-stage Docker image that exposes the S3 API port (default 9000) and the admin API port (default 8080).
2. THE System SHALL ship a `docker-compose.yml` that declares three services: Gateway, PostgreSQL 17+ and the static Console.
3. WHEN `docker compose up` is executed for the first time and required environment variables are absent, THE Gateway SHALL refuse to start and emit a human-readable error to stderr listing each missing variable on its own line.
4. WHEN the Gateway starts, THE Gateway SHALL apply all Alembic database migrations before accepting any requests.
5. WHEN the Gateway starts and PostgreSQL is not yet available, THE Gateway SHALL retry the connection up to 10 times at 3-second intervals; IF all 10 attempts fail, THEN THE Gateway SHALL exit with a non-zero exit code and emit an error message indicating that the database is unreachable.
6. WHEN an Alembic migration fails during startup, THE Gateway SHALL halt immediately, emit an error message identifying the failed migration, and SHALL NOT accept any requests.

---

### Requirement 2: Guided Setup Wizard

**User Story:** As an operator, I want a guided first-run setup, so that I can configure Telegram credentials, buckets and security settings without reading internal documentation.

#### Acceptance Criteria

1. WHEN the Gateway detects that no access key exists, THE Console SHALL redirect the operator to the setup wizard on any Console request.
2. WHILE the Setup_Wizard is active, THE Setup_Wizard SHALL collect the following fields before proceeding: Telegram API ID, Telegram API hash, Telegram login (phone number or bot token), bucket channel assignments, staging volume path and maximum size (between 1 MiB and 1 TiB), record cache path and maximum size (between 1 MiB and 100 GiB), and master key (minimum 16 characters).
3. WHEN Telegram login requires a two-factor authentication code, THE Setup_Wizard SHALL prompt the operator to enter the code and SHALL retry authentication with that code up to 3 attempts.
4. IF the operator exceeds 3 failed two-factor authentication attempts, THEN THE Setup_Wizard SHALL abort the Telegram login step, display an error message indicating that authentication failed, and return the operator to the beginning of the Telegram credential entry step.
5. WHEN setup completes successfully, THE Setup_Wizard SHALL generate and present a downloadable Recovery Kit containing the master key verifier, the Telegram session credentials, and the bucket channel assignments.
6. WHEN setup completes successfully, THE Setup_Wizard SHALL create the first S3 access key pair and display the secret key exactly once before navigating away from the confirmation screen.
7. WHEN a channel is selected as a bucket during setup and that channel already contains bucket manifest messages, THE Setup_Wizard SHALL offer the operator the choice to adopt the existing bucket or to reinitialise it, and IF the operator chooses to reinitialise, THEN THE Setup_Wizard SHALL warn that existing bucket data will become inaccessible before proceeding.

---

### Requirement 3: Telegram Session Management

**User Story:** As the Gateway, I want to maintain exactly one authenticated Telegram session, so that all bucket channels are accessible through a single account without hitting multi-session limits.

#### Acceptance Criteria

1. THE Gateway SHALL maintain exactly one active Telegram session across all concurrent S3 and admin operations, where "active" means the session is authenticated and able to send and receive Telegram API calls.
2. WHEN the Telegram session is disconnected, THE Gateway SHALL detect the disconnection within 30 seconds and attempt reconnection using exponential back-off starting at 1 second, doubling on each attempt, up to a configurable maximum interval (default 60 seconds, range 5–300 seconds).
3. WHILE the Telegram session is in a reconnecting state, THE Gateway SHALL return a 503 error response indicating Telegram unavailability to any S3 or admin request that requires Telegram access, without modifying any previously committed state.
4. THE Gateway SHALL route all Telegram operations through the TelegramPort interface, so that Telethon can be replaced without changes to the rest of the codebase.

---

### Requirement 4: Bucket Operations

**User Story:** As an S3 client, I want to create, list and delete buckets, so that I can organise objects into independent namespaces backed by Telegram channels.

#### Acceptance Criteria

1. THE Gateway SHALL support the following S3 bucket operations: ListBuckets, CreateBucket, HeadBucket, DeleteBucket, GetBucketLocation.
2. WHEN CreateBucket is called for a bucket name that does not exist, THE Gateway SHALL create a new private Telegram channel, write a Bucket Manifest message to it, and insert a row in the `buckets` table, then return HTTP 200; IF any step of this sequence fails, THEN THE Gateway SHALL roll back all preceding steps of that sequence and return an HTTP 500 error indicating the creation failure.
3. IF DeleteBucket is called for a bucket that contains at least one object that has reached the committed state, THEN THE Gateway SHALL return HTTP 409 Conflict and SHALL NOT modify the Telegram channel or the `buckets` table.
4. IF HeadBucket is called for a bucket name that exists in the `buckets` table, THEN THE Gateway SHALL return HTTP 200 OK with no response body; IF HeadBucket is called for a bucket name that does not exist in the `buckets` table, THEN THE Gateway SHALL return HTTP 404 Not Found.
5. THE Gateway SHALL return the configured `S3_REGION` value in GetBucketLocation responses.
6. IF CreateBucket is called for a bucket name that already exists in the `buckets` table, THEN THE Gateway SHALL return HTTP 409 Conflict and SHALL NOT create a new Telegram channel or modify the `buckets` table.
7. WHEN DeleteBucket is called for a bucket that contains no objects in the committed state, THE Gateway SHALL delete the associated Telegram channel, remove the corresponding row from the `buckets` table, and return HTTP 204 No Content.

---

### Requirement 5: Object Listing

**User Story:** As an S3 client, I want to list objects and use prefix and delimiter filtering, so that I can navigate bucket contents with standard tooling.

#### Acceptance Criteria

1. WHEN a ListObjectsV2 or ListObjects (v1) request is received, THE Gateway SHALL accept and process the `prefix`, `delimiter`, `max-keys`, and `continuation-token` / `marker` parameters, treating an absent `max-keys` as a default of 1000 and rejecting any `max-keys` value outside the range 1 to 1000 with an error response indicating an invalid argument.
2. WHEN ListObjectsV2 is called with a `delimiter`, THE Gateway SHALL aggregate into `CommonPrefixes` all keys that share a common prefix up to and including the first occurrence of the delimiter beyond any specified `prefix`, return `Contents` and `CommonPrefixes` entries in lexicographic order, and SHALL NOT include any key in both `Contents` and `CommonPrefixes`.
3. IF the number of matching keys exceeds `max-keys`, THEN THE Gateway SHALL return a truncated response with a `NextContinuationToken` (v2) or `NextMarker` (v1) that, when supplied in a subsequent request, retrieves the immediately following lexicographic page of results.
4. THE Gateway SHALL return only objects whose generation status is COMMITTED in listing responses for both ListObjectsV2 and ListObjects (v1).
5. IF a `continuation-token` or `marker` value is invalid or does not correspond to a known pagination state, THEN THE Gateway SHALL return an error response indicating an invalid or expired token and SHALL NOT return partial results.

---

### Requirement 6: PutObject — Synchronous Durability

**User Story:** As an S3 client, I want a PUT to return 200 only after the object is durably stored in Telegram, so that I can treat the 200 response as proof of durability by default.

#### Acceptance Criteria

1. WHEN a PutObject request is received, THE Gateway SHALL verify the SigV4 signature before processing the request body.
2. IF SigV4 verification fails, THEN THE Gateway SHALL return HTTP 403 Forbidden and SHALL NOT write any data to staging.
3. WHEN a PutObject request is received with a `Content-Length` header, THE Gateway SHALL reserve the declared number of bytes in staging before reading the body.
4. IF the sum of all in-progress staging reservations would exceed `STAGING_MAX_SIZE`, THEN THE Gateway SHALL return HTTP 507 Insufficient Storage and SHALL NOT begin streaming the body.
5. IF the object body exceeds `MAX_OBJECT_SIZE`, THEN THE Gateway SHALL return HTTP 413 Request Entity Too Large and SHALL discard any partially received body bytes without writing them to staging.
6. WHEN the body is fully received, THE Gateway SHALL compute MD5, SHA-256, and any client-supplied checksum algorithm over the received body bytes.
7. IF a client-supplied `Content-MD5` header does not match the computed MD5, THEN THE Gateway SHALL return HTTP 400 Bad Request and SHALL delete the staging file without committing any data.
8. IF a client-supplied checksum header or trailer does not match the computed value for the declared algorithm, THEN THE Gateway SHALL return HTTP 400 Bad Request and SHALL delete the staging file without committing any data.
9. WHEN all checksums are verified, THE Gateway SHALL fsync the staging file, rename it to its content-addressed path, fsync the staging directory, and insert a generation record with status STAGED and one upload job as a single atomic database transaction.
10. WHEN the Upload Worker claims a staged blob, IF a COMMITTED blob with the same SHA-256 already exists in the same bucket, THEN THE Upload_Worker SHALL reuse that existing blob without re-uploading to Telegram and SHALL immediately mark the generation COMMITTED in a single database transaction.
11. WHEN two Upload Workers attempt to upload the same SHA-256 blob simultaneously, THE Gateway SHALL allow only one upload to proceed and SHALL cause the other worker to wait until the first upload either succeeds or fails before proceeding.
12. WHEN an upload proceeds, THE Upload_Worker SHALL partition the blob into 1 MiB Records, send each Record as a Telegram document to the designated channel, write the Blob Manifest, and write the Commit Record to the channel in that order.
13. WHEN the Commit Record write to Telegram is confirmed, THE Gateway SHALL mark the generation COMMITTED, advance the bucket key's head pointer to this generation, and return HTTP 200 to the client as a single atomic database transaction.
14. IF the Telegram channel is unreachable for more than 30 seconds during an upload attempt, THEN THE Gateway SHALL return HTTP 503 Service Unavailable to the client and SHALL retain the STAGED generation record for retry.
15. WHERE the bucket has async durability enabled, THE Gateway SHALL return HTTP 200 after the generation is marked STAGED and the upload job is enqueued, without waiting for Telegram confirmation.

---

### Requirement 7: Streaming Signature and Checksum Support

**User Story:** As an S3 client using aws-chunked transfer encoding, I want the gateway to authenticate and validate each chunk, so that I can use all standard AWS SDK transfer modes without modification.

#### Acceptance Criteria

1. WHEN a request arrives with an `Authorization` header, THE Gateway SHALL authenticate the request using SigV4 header authentication.
2. WHEN a request arrives with an `X-Amz-Signature` query parameter, THE Gateway SHALL authenticate the request using SigV4 presigned query-string authentication.
3. IF SigV4 authentication fails, THEN THE Gateway SHALL reject the request with an error response indicating authentication failure and SHALL NOT process the request body.
4. WHEN a request declares `x-amz-content-sha256: UNSIGNED-PAYLOAD`, THE Gateway SHALL accept the payload without verifying its hash.
5. WHEN a request declares `x-amz-content-sha256: STREAMING-AWS4-HMAC-SHA256-PAYLOAD`, THE Gateway SHALL parse the body as aws-chunked encoding and SHALL verify the SigV4 signature of each chunk before forwarding or storing it.
6. IF a chunk signature verification fails, THEN THE Gateway SHALL abort the request, discard all received chunk data, and return an error response indicating which chunk failed validation.
7. WHEN a request declares `x-amz-content-sha256: STREAMING-UNSIGNED-PAYLOAD-TRAILER`, THE Gateway SHALL parse the body as aws-chunked encoding and SHALL validate the checksum carried in the trailing header after all chunks are received.
8. IF the trailer checksum validation fails, THEN THE Gateway SHALL discard the received data and return an error response indicating a checksum mismatch.
9. WHEN a request includes signed trailers, THE Gateway SHALL verify the trailer signature before treating the trailer values as trusted.
10. WHEN a client-supplied checksum is present, THE Gateway SHALL validate it against one of the following algorithms based on the declared algorithm header: CRC32, CRC32C, CRC64NVME, SHA-1, or SHA-256.
11. IF a client supplies a checksum algorithm not in the supported set, THEN THE Gateway SHALL reject the request with an error response indicating the unsupported algorithm before processing the payload.

---

### Requirement 8: GetObject and Ranged Reads

**User Story:** As an S3 client, I want to retrieve objects or byte ranges of objects, so that I can seek within large files without downloading the entire object.

#### Acceptance Criteria

1. THE Gateway SHALL support GetObject with no Range header, returning the full object body.
2. WHEN a `Range` header is present, THE Gateway SHALL map the requested byte range to the minimal set of Records that covers it and SHALL download only those Records.
3. WHEN a `Range` header is present, THE Gateway SHALL return HTTP 206 Partial Content with a `Content-Range` header matching the actual bytes served.
4. IF the requested byte range references Records not present in the cache, THEN THE Gateway SHALL fetch those Records from Telegram before responding, pre-fetching up to `READ_AHEAD` additional sequential Records beyond the required set, where `READ_AHEAD` is a configurable value between 0 and 32 Records inclusive.
5. THE Gateway SHALL authenticate the AES-256-GCM tag of every Record before writing any bytes of that Record to the response stream.
6. IF authentication of a Record's AES-256-GCM tag fails, THEN THE Gateway SHALL immediately terminate the response connection without writing further bytes and SHALL return an error response indicating an internal integrity failure, provided no response bytes have yet been flushed; if bytes have already been flushed, THE Gateway SHALL close the connection without completing the response.
7. THE Gateway SHALL support the conditional request headers `If-Match`, `If-None-Match`, `If-Modified-Since`, and `If-Unmodified-Since`.
8. IF a `Range` header specifies a byte range that is syntactically invalid or that begins beyond the last byte of the object, THEN THE Gateway SHALL return HTTP 416 Range Not Satisfiable with a `Content-Range` header indicating the actual object size and SHALL NOT transmit the object body.
9. IF an `If-None-Match` or `If-Modified-Since` conditional header evaluates to false, THEN THE Gateway SHALL return HTTP 304 Not Modified without transmitting the object body.
10. IF an `If-Match` or `If-Unmodified-Since` conditional header evaluates to false, THEN THE Gateway SHALL return HTTP 412 Precondition Failed without transmitting the object body.
11. THE Gateway SHALL update the last-access timestamp and access count for each Record served, for use by the cache ranking algorithm.

---

### Requirement 9: HeadObject and DeleteObject

**User Story:** As an S3 client, I want to check object existence and delete objects, so that I can manage bucket contents with standard tooling.

#### Acceptance Criteria

1. WHEN HeadObject is requested for a key that exists, THE Gateway SHALL return HTTP 200 with no response body and headers including Content-Length (bytes), Content-Type, ETag, and Last-Modified.
2. IF HeadObject is requested for a key that does not exist, THEN THE Gateway SHALL return HTTP 404 with no response body.
3. WHEN DeleteObject is requested for a key that exists, THE Gateway SHALL write a Tombstone message to the Telegram channel, mark the generation DELETED in the index, and return HTTP 204 with no response body.
4. IF the Telegram channel write fails during DeleteObject, THEN THE Gateway SHALL return HTTP 500 with no response body and leave the index entry unchanged.
5. IF DeleteObject is requested for a key that does not exist, THEN THE Gateway SHALL return HTTP 204 with no response body.
6. WHEN DeleteObjects (batch delete) is requested with 1 to 1000 keys, THE Gateway SHALL process each key independently and return a response body containing per-key success or error status for every requested key.
7. IF DeleteObjects is requested with more than 1000 keys, THEN THE Gateway SHALL return HTTP 400 with no response body.

---

### Requirement 10: Multipart Upload

**User Story:** As an S3 client uploading large objects, I want multipart upload support, so that I can upload objects larger than the maximum single-part size with standard SDK retry behaviour.

#### Acceptance Criteria

1. WHEN a CreateMultipartUpload request is received, THE Gateway SHALL create a multipart upload record and return a unique `UploadId`.
2. WHEN an UploadPart request is received with a valid `UploadId` and a part number in the range 1–10,000, THE Gateway SHALL stage the part body durably using the same per-file fsync and checksum verification guarantees as PutObject, and return an ETag for the part; parts smaller than 5 MB are accepted only for the final part (highest part number submitted at CompleteMultipartUpload).
3. IF an UploadPart request references an `UploadId` that does not exist or has already been completed or aborted, THEN THE Gateway SHALL reject the request with an error indicating the upload ID is invalid.
4. WHEN a CompleteMultipartUpload request is received with a valid `UploadId`, THE Gateway SHALL concatenate all staged parts in ascending part-number order to form a single blob and process it through the same Upload Worker path as PutObject.
5. IF a CompleteMultipartUpload request references a part number that has not been uploaded, or lists fewer than 1 part, THEN THE Gateway SHALL reject the request with an error indicating the invalid part list and SHALL NOT modify any stored data.
6. WHEN an AbortMultipartUpload request is received with a valid `UploadId`, THE Gateway SHALL delete all staged parts for that upload and remove the multipart upload record.
7. WHEN a ListParts request is received for a valid `UploadId`, THE Gateway SHALL return the part number, size in bytes, and ETag for each staged part.
8. WHEN a ListMultipartUploads request is received, THE Gateway SHALL return the `UploadId`, key, and initiation timestamp for each in-progress multipart upload.
9. WHEN a CompleteMultipartUpload succeeds, THE Gateway SHALL return an ETag in the form `<md5-of-concatenated-part-md5s>-<part-count>`, where part MD5s are ordered by ascending part number.
10. WHILE a multipart upload is in progress, IF no CompleteMultipartUpload or AbortMultipartUpload is received within `MULTIPART_EXPIRY` (default: 24 hours, configurable to a minimum of 1 hour), THEN THE GC_Worker SHALL abort the upload and delete all its staged parts.

---

### Requirement 11: CopyObject

**User Story:** As an S3 client, I want to copy objects within or between buckets on the same gateway, so that I can rename, duplicate or move objects using standard S3 operations.

#### Acceptance Criteria

1. THE Gateway SHALL support CopyObject for source and destination keys within buckets served by the same Gateway instance.
2. WHEN the CopyObject request specifies a source key and destination key where the source object's SHA-256 hash matches an existing committed blob in the destination bucket, THE Gateway SHALL record the destination key as pointing to the existing blob and SHALL NOT upload any data to Telegram.
3. WHEN the CopyObject request specifies a source key that does not exist in the source bucket, THE Gateway SHALL return HTTP 404 Not Found and SHALL NOT create or modify any destination object.
4. WHEN CopyObject completes successfully, THE Gateway SHALL return the ETag of the destination object and the last-modified timestamp of the newly created destination generation.
5. WHEN the destination bucket does not exist, THE Gateway SHALL return HTTP 404 Not Found and SHALL NOT create any object.
6. IF the source bucket and destination bucket are identical and the source key and destination key are identical, THEN THE Gateway SHALL treat the request as a no-op and SHALL return the existing object's ETag and last-modified timestamp without uploading data to Telegram.

---

### Requirement 12: Record Cache

**User Story:** As the Gateway, I want to cache decrypted records on local disk, so that repeated reads of the same data do not incur Telegram download latency.

#### Acceptance Criteria

1. THE Gateway SHALL maintain an on-disk record cache whose total size in bytes SHALL NOT exceed `CACHE_MAX_SIZE` bytes.
2. THE Gateway SHALL rank cached records by a decayed-frequency score computed from access frequency, elapsed time since last access, and record size in bytes using `CACHE_HALF_LIFE` (measured in seconds) and `CACHE_SIZE_BIAS`; records with lower scores are ranked lower.
3. WHEN the cache is full and a new record must be stored, THE Gateway SHALL evict the lowest-ranked record before writing the new record; IF two or more records share the lowest score, THEN THE Gateway SHALL evict the least recently accessed among them.
4. IF the cache is full and eviction fails due to a disk I/O error, THEN THE Gateway SHALL return an error to the caller indicating the record could not be cached, and SHALL NOT write a partial record to disk.
5. WHEN two concurrent requests need the same record, THE Gateway SHALL deduplicate the in-flight Telegram fetch so that exactly one download occurs and both requests receive the result.
6. IF the deduplicated Telegram fetch fails, THEN THE Gateway SHALL return an error indicating the fetch failure to all waiting requests for that record, and SHALL NOT store a partial record in the cache.
7. WHERE `CACHE_ENCRYPT` is true, THE Gateway SHALL encrypt each cached record file at rest using a key derived from the master key.

---

### Requirement 13: Encryption and Key Hierarchy

**User Story:** As an operator, I want all object data encrypted end-to-end with a key hierarchy under my control, so that Telegram cannot read stored object content.

#### Acceptance Criteria

1. THE Gateway SHALL derive all bucket KEKs from the master key supplied via `TGOS3_MASTER_KEY`.
2. WHEN a new Blob is created, THE Gateway SHALL generate a random 256-bit blob key using a cryptographically secure random number generator.
3. WHEN storing a blob key, THE Gateway SHALL wrap it with the bucket KEK using AES Key Wrap (RFC 3394) before storing it in the Blob Manifest.
4. WHEN storing a bucket KEK, THE Gateway SHALL wrap it with the master key using AES Key Wrap (RFC 3394) before storing it in the Bucket Manifest.
5. WHEN writing a Record, THE Gateway SHALL encrypt 1,048,560 plaintext bytes using AES-256-GCM with a 96-bit nonce, producing 1,048,560 bytes of ciphertext plus a 16-byte GCM tag, and prepend the nonce to yield a stored record of 1,048,588 bytes (12-byte nonce + 1,048,560 ciphertext + 16-byte tag).
6. WHEN a Record is read from cache or Telegram, THE Gateway SHALL verify the AES-256-GCM authentication tag before returning any decrypted bytes.
7. IF AES-256-GCM tag verification fails for a Record, THEN THE Gateway SHALL return a 500 error, log the failure, and SHALL NOT return any decrypted bytes to the caller.
8. WHEN a master key rotation command is issued, THE Gateway SHALL re-wrap all bucket KEKs under the new master key and update all Bucket Manifest messages in Telegram atomically, such that either all KEKs are re-wrapped under the new key or no changes are committed.
9. IF master key rotation fails partway through, THEN THE Gateway SHALL retain the previous key wrapping for any manifests not yet updated and SHALL return an actionable error indicating which manifests were not updated.

---

### Requirement 14: Durable Commit Semantics and Crash Safety

**User Story:** As an operator, I want every committed object to be fully recoverable after a crash at any boundary, so that I can trust the system's durability guarantees.

#### Acceptance Criteria

1. WHEN the Gateway begins uploading a generation, THE Gateway SHALL write messages to Telegram in the following order: all Record documents first, then the Blob Manifest, then the Commit Record; THE Gateway SHALL treat a Telegram write as confirmed only when Telegram returns a message_id for that message, and SHALL NOT mark the generation COMMITTED in PostgreSQL until all three message types have received a message_id confirmation.
2. WHEN the Gateway restarts after a crash, THE Gateway SHALL scan all generations with status STAGED or UPLOADING and re-enqueue their upload jobs, skipping any generation whose upload job is already present in the job queue to prevent duplicate processing.
3. WHEN the Gateway detects that a Blob Manifest message exists in Telegram for a generation but no Commit Record message exists for that generation, THE Gateway SHALL re-attempt to write the Commit Record up to 5 times with exponential backoff; IF all 5 re-attempts fail, THEN THE Gateway SHALL set the generation status to UPLOAD_FAILED and surface an error indicating that the Commit Record could not be written.
4. IF a Commit Record message exists in Telegram for a generation that has no corresponding row in PostgreSQL, THEN THE Recovery_CLI SHALL reconstruct the generation by inserting a generation row into PostgreSQL with status COMMITTED using the metadata fields present in the Commit Record message, and SHALL report the count of reconstructed generations upon completion.
5. IF the target bucket is configured with sync durability, THEN THE Gateway SHALL NOT return a 200 response to the client until the Commit Record message_id confirmation has been received from Telegram for that generation.

---

### Requirement 15: Recovery and Rebuild

**User Story:** As an operator, I want to rebuild the entire PostgreSQL index from Telegram alone using the recovery kit, so that I can recover from total index loss without losing any committed objects.

#### Acceptance Criteria

1. WHEN the operator initiates recovery, THE Recovery_CLI SHALL scan all messages in all bucket channels in ascending message ID order and reconstruct every committed object version, overwrite, and delete from Commit Records and Tombstones alone; IF a message is unreadable or undecryptable, THEN THE Recovery_CLI SHALL skip that message, increment the skipped-message counter, and continue scanning the remaining messages.
2. WHEN a Checkpoint message is present, THE Recovery_CLI SHALL select the Checkpoint with the highest message ID as the starting point and SHALL only scan messages whose message ID is strictly greater than that Checkpoint's highest included message ID.
3. THE Recovery_CLI SHALL decrypt Commit Records and Blob Manifests using the master key and the KEKs stored in the Bucket Manifests found in the same bucket channel; IF a required KEK or Bucket Manifest is absent, THEN THE Recovery_CLI SHALL treat all Commit Records in that channel as unreadable and increment the skipped-message counter accordingly.
4. WHEN recovery is complete, THE Recovery_CLI SHALL report the number of reconstructed generations, the number of skipped or unreadable messages, and any gaps detected; a gap is defined as a message ID range of one or more consecutive integers within a scanned channel for which no Commit Record, Tombstone, or system message is found.
5. WHILE the Gateway is running, THE Gateway SHALL write a Checkpoint to the system channel once per interval defined by `CHECKPOINT_INTERVAL`, where `CHECKPOINT_INTERVAL` is a positive integer number of seconds in the range 60 to 86400; each Checkpoint SHALL include an encrypted pg_dump and the highest confirmed message ID per channel.
6. IF the Gateway fails to write a Checkpoint to the system channel, THEN THE Gateway SHALL retry the write up to 3 times at 5-second intervals and, if all retries fail, SHALL emit an error log entry indicating checkpoint write failure without interrupting normal object storage operations.
7. THE Recovery_Kit SHALL contain the system channel identifier, the identifiers of all bucket channels, the master key required to decrypt Bucket Manifests, and sufficient metadata for the Recovery_CLI to locate the most recent Checkpoint in the system channel, all without requiring any prior PostgreSQL data.

---

### Requirement 16: Storage Format Stability

**User Story:** As an operator, I want the on-wire storage format to be frozen at milestone M1, so that future gateway versions can always read data written by v1.

#### Acceptance Criteria

1. THE Gateway SHALL embed format version 1 in every Blob Manifest, Commit Record, Tombstone, Bucket Manifest and Checkpoint message.
2. IF the format version field of an incoming message is not equal to 1, THEN THE Gateway SHALL reject the message and return an error to the caller indicating the unsupported format version value found, without processing the message content.
3. IF the format version field of an incoming message is not equal to 1, THEN THE Recovery_CLI SHALL reject the message without processing its content.
4. IF the format version field of an incoming message is not equal to 1, THEN THE Recovery_CLI SHALL log the rejected message including its Telegram message ID, channel, and the format version value found.

---

### Requirement 17: S3 Authentication and Access Control

**User Story:** As an operator, I want access keys to control which clients can use the gateway, so that the gateway is not open to anonymous access.

#### Acceptance Criteria

1. THE Gateway SHALL require valid SigV4 credentials for every S3 API request, where valid means the request signature matches the stored secret and the request timestamp is within 15 minutes of the Gateway's current system time.
2. THE Gateway SHALL store access key secrets as bcrypt hashes and SHALL NOT store plaintext secrets after the initial key-creation response is returned to the caller.
3. WHEN an S3 API request is received with an access key ID that does not exist, or with a signature that does not match the stored secret for that key ID, THE Gateway SHALL return HTTP 403 Forbidden with an error body indicating an authentication failure.
4. IF an S3 API request timestamp deviates by more than 15 minutes from the Gateway's current system time, THEN THE Gateway SHALL return HTTP 403 Forbidden with an error body indicating a request timestamp mismatch.
5. THE Admin_API SHALL require HTTP Basic authentication credentials matching the admin password configured at Gateway startup for every request.
6. IF the Gateway starts without an admin password configured, THEN THE Gateway SHALL refuse to start and SHALL emit an error message indicating that admin credentials are required.
7. WHEN a state-mutating request (POST, PUT, DELETE, PATCH) is received by the Console admin endpoints, THE Gateway SHALL validate a CSRF token supplied in the request header against the token bound to the current admin session, and SHALL reject requests where the tokens do not match with HTTP 403 Forbidden.

---

### Requirement 18: Observability

**User Story:** As an operator, I want metrics, health endpoints and an audit log, so that I can monitor the system and investigate incidents.

#### Acceptance Criteria

1. THE Gateway SHALL expose a Prometheus metrics endpoint at `/metrics` on the admin port.
2. THE Gateway SHALL expose a liveness health endpoint at `/health/live` and a readiness health endpoint at `/health/ready` on the admin port.
3. WHEN `/health/ready` is called and the PostgreSQL connection is unavailable, THE Gateway SHALL return HTTP 503 with a response body indicating the PostgreSQL dependency is unhealthy.
4. WHEN `/health/ready` is called and the Telegram session is disconnected, THE Gateway SHALL return HTTP 503 with a response body indicating the Telegram dependency is unhealthy.
5. THE Gateway SHALL write an entry to the audit log for every S3 API request, recording: timestamp (ISO 8601 UTC), access key ID, operation name, bucket name, object key, HTTP response status code, and latency in milliseconds.
6. THE Audit_Log SHALL redact all credential values, encryption keys, and session tokens before writing each entry, replacing each redacted value with a fixed placeholder string.
7. WHEN `/health/live` is called, THE Gateway SHALL return HTTP 200 if the process is running and able to handle requests, or HTTP 503 if the process is in a non-recoverable error state.
8. IF an audit log write operation fails, THEN THE Gateway SHALL continue processing the S3 API request and increment a Prometheus counter tracking audit log write failures.

---

### Requirement 19: Console UI

**User Story:** As an operator, I want a web console, so that I can manage buckets, browse objects, monitor pending writes and initiate recovery without using the CLI.

#### Acceptance Criteria

1. THE Console SHALL display a bucket list with per-bucket object count, total stored bytes in human-readable units (B, KB, MB, GB, TB), and pending-write count, refreshed automatically at intervals of no more than 30 seconds.
2. THE Console SHALL provide an object browser that lists objects in a selected bucket with prefix-based navigation, displaying objects whose keys share the current prefix and collapsing common sub-prefixes into navigable folder entries, supporting pagination of 100 objects per page for buckets with more than 1,000 objects.
3. THE Console SHALL display the list of upload jobs with status (STAGED, UPLOADING, COMMITTED, FAILED) and allow the operator to retry a FAILED job, transitioning the job status to STAGED upon successful retry submission.
4. THE Console SHALL display the timestamp (date and time to second precision, UTC) of the last completed Checkpoint and the outcome (pass or fail) of the most recent recovery drill, or display "None" for either value when no Checkpoint or recovery drill has been completed.
5. THE Console SHALL provide a settings page for updating staging size, cache size, cache half-life, read-ahead count, and upload concurrency, with each change taking effect within 5 seconds of confirmation without restarting the Gateway.
6. IF a settings change would reduce staging size below the currently reserved amount, THEN THE Console SHALL display an error indicating the minimum permitted value.
7. IF a settings change would reduce staging size below the currently reserved amount, THEN THE Console SHALL NOT apply the change.

---

### Requirement 20: Workflow Matrix Compatibility

**User Story:** As an operator, I want the gateway to pass the workflow matrix tests, so that I can use it with standard S3 tools without modification.

#### Acceptance Criteria

1. THE Gateway SHALL pass all operations defined in the workflow matrix test suite for AWS CLI v2 at the version recorded in the workflow matrix pin file, using the tool's out-of-the-box client configuration and its default checksum behaviour.
2. THE Gateway SHALL pass all operations defined in the workflow matrix test suite for boto3 at the version recorded in the workflow matrix pin file, covering bucket creation and deletion, object put, get, delete, multipart upload, and listing operations.
3. THE Gateway SHALL pass all operations defined in the workflow matrix test suite for rclone at the version recorded in the workflow matrix pin file, covering copy, sync, and mount operations.
4. THE Gateway SHALL pass all operations defined in the workflow matrix test suite for restic at the version recorded in the workflow matrix pin file, covering repository initialisation, backup, restore, and snapshot listing operations.
5. THE Gateway SHALL pass all operations defined in the workflow matrix test suite for MinIO mc at the version recorded in the workflow matrix pin file, covering bucket and object management operations.
6. WHEN a client sends a request whose payload signature is `UNSIGNED-PAYLOAD`, THE Gateway SHALL accept the request and process it without performing checksum validation on the request body.
7. IF a request bearing `UNSIGNED-PAYLOAD` is otherwise structurally invalid, THEN THE Gateway SHALL reject it with an error response indicating the specific structural violation without attempting body checksum validation.

---

### Requirement 21: Benchmarking and Capacity (Milestone M0)

**User Story:** As the project, I want measured Telegram throughput baselines before the storage format is frozen, so that chunk size and concurrency defaults are grounded in real data.

#### Acceptance Criteria

1. THE M0_Benchmark SHALL measure sustained upload rate (bytes/s), sustained download rate (bytes/s), cold time-to-first-byte (ms, starting from a process with no cached file references), and flood onset (the upload concurrency level or request rate at which the first Telegram rate-limit response is received), each over a continuous run of at least 60 seconds at the configured chunk size and upload concurrency.
2. THE M0_Benchmark SHALL measure file-reference refresh cost as both round-trip latency (ms) and API call count, and channel message scan rate (messages/s), each averaged over at least 10 consecutive iterations, and SHALL emit all results as a machine-readable report.
3. WHEN M0 results are available, THE Project SHALL set `CHUNK_RECORDS` to the chunk size that achieved the highest sustained upload rate, `UPLOAD_CONCURRENCY` to the concurrency level one step below flood onset, and `READ_AHEAD` to the value that minimised cold time-to-first-byte, before freezing the storage format at M1.

---

### Requirement 22: Parser and Serialiser Correctness

**User Story:** As the Gateway, I want all binary format parsers and serialisers to be round-trip correct, so that data is never silently corrupted during encode/decode cycles.

#### Acceptance Criteria

1. WHEN the Record_Codec serialises a 1,048,560-byte plaintext input using AES-256-GCM, THE Record_Codec SHALL produce a ciphertext output of exactly 1,048,576 bytes (excluding the prepended nonce).
2. WHEN the Record_Codec decrypts a ciphertext produced by encrypting a plaintext input of 1 to 1,048,560 bytes using the same key and nonce, THE Record_Codec SHALL produce output that is byte-for-byte identical to the original plaintext input.
3. WHEN the Commit_Record_Serialiser serialises a CommitRecord object to a Telegram message payload, THE Commit_Record_Serialiser SHALL produce a payload that, when deserialised, yields a CommitRecord object whose every field compares equal by value to the original.
4. WHEN the Commit_Record_Serialiser deserialises a payload produced by serialising any CommitRecord object with all required fields present and of their declared types, THE Commit_Record_Serialiser SHALL produce a CommitRecord object whose every field compares equal by value to the original.
5. WHEN the Blob_Manifest_Serialiser serialises a BlobManifest object to a Telegram message payload, THE Blob_Manifest_Serialiser SHALL produce a payload that, when deserialised, yields a BlobManifest object whose every field compares equal by value to the original.
6. WHEN the Blob_Manifest_Serialiser deserialises a payload produced by serialising any BlobManifest object with all required fields present and of their declared types, THE Blob_Manifest_Serialiser SHALL produce a BlobManifest object whose every field compares equal by value to the original.
7. WHEN the SigV4_Parser receives an `Authorization` header containing all four required fields — Algorithm, Credential, SignedHeaders, and Signature — each separated by commas and with values in the format defined by the AWS SigV4 specification, THE SigV4_Parser SHALL extract and return the Credential, SignedHeaders, and Signature field values without modification.
8. WHEN the SigV4_Parser parses an `Authorization` header containing all four required fields as defined in criterion 7 and then formats the extracted fields back into an `Authorization` header, THE SigV4_Parser SHALL produce a header whose Algorithm, Credential, SignedHeaders, and Signature field values are identical to those of the original header.
9. IF the SigV4_Parser receives an `Authorization` header missing at least one of the four required fields (Algorithm, Credential, SignedHeaders, Signature) or containing any field in a format that does not conform to the AWS SigV4 specification, THEN THE SigV4_Parser SHALL return a structured parse error value containing an error kind indicator and the position or field name at which parsing failed, without raising an unhandled exception.
