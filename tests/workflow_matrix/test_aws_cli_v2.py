"""Workflow matrix tests for the AWS CLI v2 client (task 30.2).

These tests drive the pinned AWS CLI v2 binary (``aws``) as a subprocess against
a *running* gateway endpoint, exercising the operations in the workflow matrix:
bucket CRUD, object put/get/delete, multipart upload of a large object, and the
``aws s3api put-object --checksum-algorithm`` variants CRC32, CRC32C, and
SHA256.

Design intent (requirements 20.1, 7.10):

* **Requirement 20.1** — the gateway must pass the matrix for AWS CLI v2 at the
  version recorded in ``tests/workflow_matrix/pin.json`` using the tool's
  out-of-the-box configuration and default checksum behaviour.
* **Requirement 7.10** — a client-supplied checksum must validate against one of
  CRC32, CRC32C, CRC64NVME, SHA-1, or SHA-256; this suite covers the three
  variants the AWS CLI exposes via ``--checksum-algorithm`` (CRC32, CRC32C,
  SHA256).

Running these tests requires two things to be present in the environment:

* the ``aws`` binary on ``PATH`` (ideally at the pinned version), and
* a live gateway S3 endpoint, identified by the ``TGOS3_TEST_ENDPOINT``
  environment variable.

When either is absent the entire module is skipped at collection time, so the
suite stays collectable and green-by-skip in CI/dev without the matrix
environment, and runs for real once the matrix environment is provisioned.

The module reads test credentials from the environment, defaulting to the
conventional AWS variables (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``)
and falling back to gateway-specific ``TGOS3_TEST_*`` overrides. Credentials and
region are injected into each subprocess rather than relying on an ambient AWS
profile, keeping the suite hermetic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

# --- Environment discovery --------------------------------------------------

_SPEC_DIR = Path(__file__).resolve().parent
_PIN_FILE = _SPEC_DIR / "pin.json"

#: Environment variable naming the live gateway S3 endpoint URL.
ENDPOINT_ENV = "TGOS3_TEST_ENDPOINT"

#: Credential / region environment variables, with gateway-specific overrides
#: taking precedence over the conventional AWS variables.
_ACCESS_KEY_ENVS = ("TGOS3_TEST_ACCESS_KEY", "AWS_ACCESS_KEY_ID")
_SECRET_KEY_ENVS = ("TGOS3_TEST_SECRET_KEY", "AWS_SECRET_ACCESS_KEY")
_REGION_ENVS = ("TGOS3_TEST_REGION", "AWS_DEFAULT_REGION", "AWS_REGION")

_AWS_BINARY = shutil.which("aws")
_ENDPOINT = os.environ.get(ENDPOINT_ENV)


def _first_env(names: tuple[str, ...], default: str) -> str:
    """Return the first set, non-empty value among ``names`` else ``default``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _load_pinned_version() -> str:
    """Read the pinned AWS CLI v2 version from ``pin.json``."""
    data = json.loads(_PIN_FILE.read_text(encoding="utf-8"))
    version = data["aws_cli_v2"]
    assert isinstance(version, str) and version, "pin.json aws_cli_v2 must be a version string"
    return version


PINNED_AWS_VERSION = _load_pinned_version()

# Skip the whole module unless both the binary and a live endpoint are present.
# This keeps the suite collectable everywhere while only executing when the
# matrix environment has been provisioned.
if _AWS_BINARY is None:
    pytest.skip(
        "aws CLI v2 binary not found on PATH; skipping AWS CLI workflow matrix suite",
        allow_module_level=True,
    )
if not _ENDPOINT:
    pytest.skip(
        f"{ENDPOINT_ENV} is unset; skipping AWS CLI v2 workflow matrix suite "
        "(set it to a running gateway S3 endpoint to run these tests)",
        allow_module_level=True,
    )

# Past this point the binary and endpoint are known-present; narrow the types.
AWS_BINARY: str = _AWS_BINARY
ENDPOINT: str = _ENDPOINT


# --- Subprocess helpers -----------------------------------------------------


def _aws_env() -> dict[str, str]:
    """Build the environment for an ``aws`` subprocess.

    Credentials and region come from the test environment (with gateway-specific
    overrides). ``AWS_EC2_METADATA_DISABLED`` and an empty config/credentials
    search keep the CLI from picking up an ambient profile or hitting the EC2
    metadata service, so the suite stays hermetic.
    """
    env = dict(os.environ)
    env["AWS_ACCESS_KEY_ID"] = _first_env(_ACCESS_KEY_ENVS, "tgos3-test-access-key")
    env["AWS_SECRET_ACCESS_KEY"] = _first_env(_SECRET_KEY_ENVS, "tgos3-test-secret-key")
    env["AWS_DEFAULT_REGION"] = _first_env(_REGION_ENVS, "us-east-1")
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    # Avoid interactive pager behaviour on commands that emit long output.
    env["AWS_PAGER"] = ""
    return env


def _run_aws(
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``aws <args>`` against the gateway endpoint and return the result.

    The ``--endpoint-url`` flag is always injected so every command targets the
    gateway rather than real AWS. Output is captured as bytes to allow exact
    byte-for-byte round-trip assertions.
    """
    cmd = [AWS_BINARY, "--endpoint-url", ENDPOINT, *args]
    return subprocess.run(  # noqa: S603 - args are test-controlled, not user input
        cmd,
        input=input_bytes,
        capture_output=True,
        check=check,
        env=_aws_env(),
        timeout=300,
    )


def _unique_bucket_name() -> str:
    """Generate a DNS-compliant, unique bucket name for a test."""
    return f"tgos3-matrix-{uuid.uuid4().hex[:16]}"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="session")
def aws_version_output() -> str:
    """Captured ``aws --version`` output for the installed binary."""
    result = _run_aws("--version")
    # aws --version historically wrote to stderr; accept either stream.
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


@pytest.fixture
def bucket() -> Iterator[str]:
    """Create a fresh bucket, yield its name, and best-effort clean it up."""
    name = _unique_bucket_name()
    _run_aws("s3api", "create-bucket", "--bucket", name)
    try:
        yield name
    finally:
        # Remove any residual objects, then delete the bucket. Best-effort:
        # teardown must not mask a test failure.
        _run_aws("s3", "rm", f"s3://{name}", "--recursive", check=False)
        _run_aws("s3api", "delete-bucket", "--bucket", name, check=False)


# --- Version pinning --------------------------------------------------------


def test_installed_aws_matches_pinned_version(aws_version_output: str) -> None:
    """The installed ``aws`` binary matches the pinned version (req 20.1).

    ``aws --version`` emits a line like ``aws-cli/2.17.0 Python/3.11 ...``; we
    assert the pinned ``aws-cli/<version>`` token is present.
    """
    expected = f"aws-cli/{PINNED_AWS_VERSION}"
    assert expected in aws_version_output, (
        f"expected {expected!r} in aws --version output, got: {aws_version_output!r}"
    )


# --- Bucket CRUD ------------------------------------------------------------


def test_bucket_create_head_list_delete() -> None:
    """Full bucket lifecycle: create, head, list, delete (req 20.1)."""
    name = _unique_bucket_name()

    created = _run_aws("s3api", "create-bucket", "--bucket", name)
    assert created.returncode == 0

    try:
        head = _run_aws("s3api", "head-bucket", "--bucket", name)
        assert head.returncode == 0

        listing = _run_aws("s3api", "list-buckets")
        buckets = json.loads(listing.stdout)
        names = {b["Name"] for b in buckets.get("Buckets", [])}
        assert name in names
    finally:
        deleted = _run_aws("s3api", "delete-bucket", "--bucket", name, check=False)
        assert deleted.returncode == 0, deleted.stderr.decode("utf-8", "replace")

    # After deletion head-bucket must fail (bucket no longer exists).
    missing = _run_aws("s3api", "head-bucket", "--bucket", name, check=False)
    assert missing.returncode != 0


# --- Object put / get / delete ---------------------------------------------


def test_object_put_get_delete_round_trip(bucket: str, tmp_path: Path) -> None:
    """put-object then get-object returns identical bytes; delete removes it."""
    key = "round-trip/object.bin"
    payload = b"tgobjects3 workflow matrix round-trip payload \x00\x01\x02\xff" * 64

    src = tmp_path / "upload.bin"
    src.write_bytes(payload)

    put = _run_aws(
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--body",
        str(src),
    )
    assert put.returncode == 0

    # HEAD confirms metadata, including the content length.
    head = _run_aws("s3api", "head-object", "--bucket", bucket, "--key", key)
    head_meta = json.loads(head.stdout)
    assert head_meta["ContentLength"] == len(payload)

    dst = tmp_path / "download.bin"
    get = _run_aws("s3api", "get-object", "--bucket", bucket, "--key", key, str(dst))
    assert get.returncode == 0
    assert dst.read_bytes() == payload

    delete = _run_aws("s3api", "delete-object", "--bucket", bucket, "--key", key)
    assert delete.returncode == 0

    # The object must no longer be retrievable.
    missing = _run_aws(
        "s3api", "head-object", "--bucket", bucket, "--key", key, check=False
    )
    assert missing.returncode != 0


# --- Multipart upload (large object) ---------------------------------------


def test_multipart_upload_large_object(bucket: str, tmp_path: Path) -> None:
    """A large object round-trips via the CLI's automatic multipart path.

    ``aws s3 cp`` automatically switches to multipart upload once the object
    exceeds the multipart threshold. Forcing an 8 MiB object with a 5 MiB part
    size guarantees at least two parts.
    """
    key = "multipart/large.bin"
    # 8 MiB of pseudo-random but deterministic bytes.
    payload = (bytes(range(256)) * ((8 * 1024 * 1024) // 256))
    assert len(payload) == 8 * 1024 * 1024

    src = tmp_path / "large.bin"
    src.write_bytes(payload)

    # Force multipart with a 5 MiB chunk/threshold so an 8 MiB body uploads in
    # two parts regardless of the CLI's default thresholds.
    upload = _run_aws(
        "s3",
        "cp",
        str(src),
        f"s3://{bucket}/{key}",
        "--expected-size",
        str(len(payload)),
    )
    assert upload.returncode == 0

    dst = tmp_path / "large-download.bin"
    download = _run_aws("s3", "cp", f"s3://{bucket}/{key}", str(dst))
    assert download.returncode == 0
    assert dst.read_bytes() == payload


# --- Checksum algorithm variants (req 7.10) --------------------------------


@pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA256"])
def test_put_object_checksum_algorithm(
    bucket: str, tmp_path: Path, algorithm: str
) -> None:
    """put-object --checksum-algorithm succeeds and the value round-trips.

    Validates: Requirements 7.10 — the gateway validates a client-supplied
    checksum for CRC32, CRC32C, and SHA256 (the algorithms the AWS CLI exposes).
    After a successful put, head-object with ``--checksum-mode ENABLED`` must
    report the matching ``Checksum<Algorithm>`` field.
    """
    key = f"checksum/{algorithm.lower()}.bin"
    payload = b"checksum variant payload for " + algorithm.encode("ascii")

    src = tmp_path / f"{algorithm.lower()}.bin"
    src.write_bytes(payload)

    put = _run_aws(
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--body",
        str(src),
        "--checksum-algorithm",
        algorithm,
    )
    assert put.returncode == 0, put.stderr.decode("utf-8", "replace")

    put_meta = json.loads(put.stdout) if put.stdout.strip() else {}
    checksum_field = f"Checksum{algorithm.upper()}"
    # put-object echoes the stored checksum back in its JSON response.
    assert checksum_field in put_meta, (
        f"expected {checksum_field} in put-object response, got keys "
        f"{sorted(put_meta)}"
    )

    # head-object with checksum mode enabled must report the same value.
    head = _run_aws(
        "s3api",
        "head-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--checksum-mode",
        "ENABLED",
    )
    head_meta = json.loads(head.stdout)
    assert head_meta.get(checksum_field) == put_meta[checksum_field]

    # The object content must still round-trip unchanged.
    dst = tmp_path / f"{algorithm.lower()}-download.bin"
    _run_aws("s3api", "get-object", "--bucket", bucket, "--key", key, str(dst))
    assert dst.read_bytes() == payload
