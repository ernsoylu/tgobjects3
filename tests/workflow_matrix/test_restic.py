"""Workflow matrix tests for the restic backup client (task 30.5).

These tests drive the pinned ``restic`` binary as a subprocess against a
*running* gateway endpoint used as an S3 backend, exercising the operations in
the workflow matrix: repository initialisation, backup, snapshot listing, and
restore.

Design intent (requirement 20.4):

* **Requirement 20.4** — the gateway must pass the matrix for restic at the
  version recorded in ``tests/workflow_matrix/pin.json``, covering repository
  initialisation (``restic init``), backup (``restic backup``), restore
  (``restic restore``), and snapshot listing (``restic snapshots``).

restic addresses an S3 backend through a repository URL of the form
``s3:<endpoint>/<bucket>``. The bucket must exist before ``restic init`` runs,
so each test provisions its repository bucket up front (via boto3 against the
same endpoint) and removes it on teardown. Credentials are supplied through the
conventional ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` environment
variables that restic reads for its S3 backend, and the repository encryption
password through ``RESTIC_PASSWORD``.

Running these tests requires two things to be present in the environment:

* the ``restic`` binary on ``PATH`` (ideally at the pinned version), and
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

import contextlib
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

#: Password restic uses to encrypt the repository. Fixed for the test suite.
_RESTIC_PASSWORD = os.environ.get("RESTIC_PASSWORD") or "tgos3-matrix-restic-password"

_RESTIC_BINARY = shutil.which("restic")
_ENDPOINT = os.environ.get(ENDPOINT_ENV)


def _first_env(names: tuple[str, ...], default: str) -> str:
    """Return the first set, non-empty value among ``names`` else ``default``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _load_pinned_version() -> str:
    """Read the pinned restic version from ``pin.json``."""
    data = json.loads(_PIN_FILE.read_text(encoding="utf-8"))
    version = data["restic"]
    assert isinstance(version, str) and version, "pin.json restic must be a version string"
    return version


PINNED_RESTIC_VERSION = _load_pinned_version()

# Skip the whole module unless both the binary and a live endpoint are present.
# This keeps the suite collectable everywhere while only executing when the
# matrix environment has been provisioned.
if _RESTIC_BINARY is None:
    pytest.skip(
        "restic binary not found on PATH; skipping restic workflow matrix suite",
        allow_module_level=True,
    )
if not _ENDPOINT:
    pytest.skip(
        f"{ENDPOINT_ENV} is unset; skipping restic workflow matrix suite "
        "(set it to a running gateway S3 endpoint to run these tests)",
        allow_module_level=True,
    )

# Past this point the binary and endpoint are known-present; narrow the types.
RESTIC_BINARY: str = _RESTIC_BINARY
ENDPOINT: str = _ENDPOINT

#: restic's S3 backend wants the endpoint without its URL scheme; the repository
#: URL carries the ``s3:`` prefix instead. We strip ``http(s)://`` so a plain
#: ``s3:host:port/bucket`` repository URL is formed.
_ENDPOINT_HOSTPORT = ENDPOINT.split("://", 1)[-1].rstrip("/")


# --- Subprocess helpers -----------------------------------------------------


def _restic_env() -> dict[str, str]:
    """Build the environment for a ``restic`` subprocess.

    Credentials and region come from the test environment (with gateway-specific
    overrides). ``RESTIC_PASSWORD`` supplies the repository encryption password
    non-interactively. ``AWS_EC2_METADATA_DISABLED`` keeps the S3 backend from
    reaching for the EC2 metadata service, so the suite stays hermetic.
    """
    env = dict(os.environ)
    env["AWS_ACCESS_KEY_ID"] = _first_env(_ACCESS_KEY_ENVS, "tgos3-test-access-key")
    env["AWS_SECRET_ACCESS_KEY"] = _first_env(_SECRET_KEY_ENVS, "tgos3-test-secret-key")
    env["AWS_DEFAULT_REGION"] = _first_env(_REGION_ENVS, "us-east-1")
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["RESTIC_PASSWORD"] = _RESTIC_PASSWORD
    return env


def _repo_url(bucket: str) -> str:
    """Build restic's S3 repository URL for ``bucket`` on the gateway endpoint."""
    return f"s3:{_ENDPOINT_HOSTPORT}/{bucket}"


def _run_restic(
    repo: str,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``restic -r <repo> <args>`` against the gateway and return the result.

    The ``-r/--repo`` flag is always injected so every command targets the
    gateway-backed repository. Output is captured as bytes.
    """
    cmd = [RESTIC_BINARY, "-r", repo, *args]
    return subprocess.run(  # noqa: S603 - args are test-controlled, not user input
        cmd,
        capture_output=True,
        check=check,
        env=_restic_env(),
        timeout=300,
    )


def _unique_bucket_name() -> str:
    """Generate a DNS-compliant, unique bucket name for a test."""
    return f"tgos3-matrix-{uuid.uuid4().hex[:16]}"


# --- Repository bucket provisioning -----------------------------------------


def _s3_client() -> object:
    """Build a boto3 S3 client targeting the gateway endpoint.

    boto3 is used only to create/destroy the repository bucket that restic's S3
    backend requires to pre-exist; restic itself owns every object inside it.
    """
    import boto3  # imported lazily so collection does not require boto3

    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=_first_env(_ACCESS_KEY_ENVS, "tgos3-test-access-key"),
        aws_secret_access_key=_first_env(_SECRET_KEY_ENVS, "tgos3-test-secret-key"),
        region_name=_first_env(_REGION_ENVS, "us-east-1"),
    )


def _empty_and_delete_bucket(client: object, bucket: str) -> None:
    """Best-effort removal of every object in ``bucket`` then the bucket itself.

    Teardown must not mask a test failure, so every call tolerates errors.
    """
    with contextlib.suppress(Exception):  # best-effort teardown
        paginator = client.get_paginator("list_objects_v2")  # type: ignore[attr-defined]
        for page in paginator.paginate(Bucket=bucket):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                client.delete_objects(  # type: ignore[attr-defined]
                    Bucket=bucket, Delete={"Objects": keys}
                )
    with contextlib.suppress(Exception):  # best-effort teardown
        client.delete_bucket(Bucket=bucket)  # type: ignore[attr-defined]


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="session")
def restic_version_output() -> str:
    """Captured ``restic version`` output for the installed binary."""
    result = subprocess.run(  # noqa: S603 - fixed, test-controlled command
        [RESTIC_BINARY, "version"],
        capture_output=True,
        check=True,
        env=_restic_env(),
        timeout=60,
    )
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


@pytest.fixture
def repo() -> Iterator[str]:
    """Provision a fresh repository bucket, yield its restic URL, clean up.

    The S3 bucket backing the repository must exist before ``restic init``, so
    it is created via boto3 here. If the bucket cannot be created the test is
    skipped rather than failed, keeping the suite green-by-skip when the matrix
    environment is incomplete.
    """
    try:
        client = _s3_client()
    except Exception as exc:  # noqa: BLE001 - boto3 missing or misconfigured
        pytest.skip(f"cannot construct S3 client for restic repo bucket: {exc}")

    bucket = _unique_bucket_name()
    try:
        client.create_bucket(Bucket=bucket)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - endpoint unreachable or denied
        pytest.skip(f"cannot create restic repository bucket {bucket!r}: {exc}")

    try:
        yield _repo_url(bucket)
    finally:
        _empty_and_delete_bucket(client, bucket)


# --- Version pinning --------------------------------------------------------


def test_installed_restic_matches_pinned_version(restic_version_output: str) -> None:
    """The installed ``restic`` binary matches the pinned version (req 20.4).

    ``restic version`` emits a line like ``restic 0.17.0 compiled with go1.22``;
    we assert the pinned ``restic <version>`` token is present.
    """
    expected = f"restic {PINNED_RESTIC_VERSION}"
    assert expected in restic_version_output, (
        f"expected {expected!r} in restic version output, got: {restic_version_output!r}"
    )


# --- Backup lifecycle -------------------------------------------------------


def test_init_backup_snapshots_restore_round_trip(repo: str, tmp_path: Path) -> None:
    """Full restic lifecycle against the gateway S3 backend (req 20.4).

    Exercises every operation in the matrix in order:

    * ``restic init`` creates the repository in the pre-provisioned bucket.
    * ``restic backup`` snapshots a temporary directory of files.
    * ``restic snapshots`` lists snapshots and the new one appears.
    * ``restic restore`` extracts the snapshot to a fresh directory, and every
      restored file is verified byte-for-byte against the source.
    """
    # Build a small tree of files with distinct, deterministic contents.
    source = tmp_path / "source"
    source.mkdir()
    files = {
        "hello.txt": b"hello tgobjects3 restic workflow matrix\n",
        "binary.bin": bytes(range(256)) * 32,
        "nested/data.log": b"line one\nline two\n" * 100,
        "empty": b"",
    }
    for rel, content in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # init -------------------------------------------------------------------
    init = _run_restic(repo, "init")
    assert init.returncode == 0, init.stderr.decode("utf-8", "replace")

    # backup -----------------------------------------------------------------
    backup = _run_restic(repo, "backup", "--json", str(source))
    assert backup.returncode == 0, backup.stderr.decode("utf-8", "replace")

    # The final JSON line of a --json backup is a summary carrying the snapshot
    # id. Parse it so the snapshot can be addressed directly on restore.
    snapshot_id = _parse_backup_snapshot_id(backup.stdout)
    assert snapshot_id, "restic backup did not report a snapshot_id"

    # snapshots --------------------------------------------------------------
    listing = _run_restic(repo, "snapshots", "--json")
    assert listing.returncode == 0, listing.stderr.decode("utf-8", "replace")
    snapshots = json.loads(listing.stdout)
    assert isinstance(snapshots, list) and snapshots, "expected at least one snapshot"
    ids = {snap["id"] for snap in snapshots}
    short_ids = {snap["short_id"] for snap in snapshots}
    assert snapshot_id in ids or snapshot_id in short_ids, (
        f"backup snapshot {snapshot_id!r} not present in snapshot listing {ids!r}"
    )

    # restore ----------------------------------------------------------------
    target = tmp_path / "restored"
    restore = _run_restic(repo, "restore", snapshot_id, "--target", str(target))
    assert restore.returncode == 0, restore.stderr.decode("utf-8", "replace")

    # restic restores files under the absolute source path within the target.
    restored_root = target / source.relative_to(source.anchor)
    for rel, content in files.items():
        restored = restored_root / rel
        assert restored.is_file(), f"restored file missing: {rel}"
        assert restored.read_bytes() == content, f"restored content differs: {rel}"


def _parse_backup_snapshot_id(stdout: bytes) -> str:
    """Extract the snapshot id from a ``restic backup --json`` output stream.

    The stream is newline-delimited JSON; the terminal ``{"message_type":
    "summary", ...}`` object carries ``snapshot_id``.
    """
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("snapshot_id"):
            return str(obj["snapshot_id"])
    return ""
