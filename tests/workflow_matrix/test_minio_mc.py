"""Workflow matrix tests for the MinIO client ``mc`` (task 30.6).

These tests drive the pinned MinIO client binary (``mc``) as a subprocess
against a *running* gateway endpoint, exercising the bucket and object
management operations in the workflow matrix: making a bucket (``mc mb``),
listing buckets and objects (``mc ls``), uploading and downloading objects with
a byte-for-byte round-trip (``mc cp``), inspecting object metadata (``mc stat``),
removing an object (``mc rm``), and removing a bucket (``mc rb``).

Design intent (requirement 20.5):

* **Requirement 20.5** — the gateway must pass the workflow matrix for MinIO
  ``mc`` at the version recorded in ``tests/workflow_matrix/pin.json``, covering
  bucket and object management operations.

Running these tests requires two things to be present in the environment:

* the ``mc`` binary on ``PATH`` (ideally at the pinned version), and
* a live gateway S3 endpoint, identified by the ``TGOS3_TEST_ENDPOINT``
  environment variable.

When either is absent the entire module is skipped at collection time, so the
suite stays collectable and green-by-skip in CI/dev without the matrix
environment, and runs for real once the matrix environment is provisioned.

The module reads test credentials and region from the environment, defaulting to
gateway-specific ``TGOS3_TEST_*`` overrides and falling back to the conventional
AWS variables. ``mc`` is configured against an *isolated* temporary config
directory (via ``--config-dir``) with a per-session alias pointing at the
gateway, so the suite never touches a developer's real ``mc`` configuration and
stays hermetic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
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

_MC_BINARY = shutil.which("mc")
_ENDPOINT = os.environ.get(ENDPOINT_ENV)


def _first_env(names: tuple[str, ...], default: str) -> str:
    """Return the first set, non-empty value among ``names`` else ``default``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _load_pinned_version() -> str:
    """Read the pinned MinIO ``mc`` version from ``pin.json``."""
    data = json.loads(_PIN_FILE.read_text(encoding="utf-8"))
    version = data["minio_mc"]
    assert isinstance(version, str) and version, "pin.json minio_mc must be a version string"
    return version


PINNED_MC_VERSION = _load_pinned_version()

# Skip the whole module unless both the binary and a live endpoint are present.
# This keeps the suite collectable everywhere while only executing when the
# matrix environment has been provisioned.
if _MC_BINARY is None:
    pytest.skip(
        "mc (MinIO client) binary not found on PATH; skipping MinIO mc workflow matrix suite",
        allow_module_level=True,
    )
if not _ENDPOINT:
    pytest.skip(
        f"{ENDPOINT_ENV} is unset; skipping MinIO mc workflow matrix suite "
        "(set it to a running gateway S3 endpoint to run these tests)",
        allow_module_level=True,
    )

# Past this point the binary and endpoint are known-present; narrow the types.
MC_BINARY: str = _MC_BINARY
ENDPOINT: str = _ENDPOINT

#: Alias name used for the gateway inside the isolated mc config directory.
_ALIAS = "tgos3"


# --- Subprocess helpers -----------------------------------------------------


def _mc_env() -> dict[str, str]:
    """Build a hermetic environment for an ``mc`` subprocess.

    ``mc`` is non-interactive and reads its configuration from the directory
    passed via ``--config-dir``; the alias created there carries the gateway
    credentials, so no credential environment variables are required. The
    environment is kept otherwise clean to avoid ambient configuration leaking
    in.
    """
    env = dict(os.environ)
    # Disable update checks / telemetry style ambient behaviour where honoured.
    env["MC_DISABLE_PAGER"] = "true"
    return env


def _run_mc(
    config_dir: Path,
    *args: str,
    check: bool = True,
    timeout: float = 300,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``mc --config-dir <dir> <args>`` and return the completed process.

    ``--config-dir`` isolates the client to a per-session configuration tree so
    the suite never mutates a developer's real ``~/.mc`` state. Output is
    captured as bytes to allow exact byte-for-byte round-trip assertions and
    JSON parsing of ``--json`` output.
    """
    cmd = [MC_BINARY, "--config-dir", str(config_dir), *args]
    return subprocess.run(  # noqa: S603 - args are test-controlled, not user input
        cmd,
        capture_output=True,
        check=check,
        env=_mc_env(),
        timeout=timeout,
    )


def _unique_bucket_name() -> str:
    """Generate a DNS-compliant, unique bucket name for a test."""
    return f"tgos3-matrix-{uuid.uuid4().hex[:16]}"


def _alias_path(name: str) -> str:
    """Build an ``alias/bucket`` or ``alias/bucket/key`` mc path."""
    return f"{_ALIAS}/{name}"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="session")
def mc_config_dir() -> Iterator[Path]:
    """An isolated ``mc`` config directory with the gateway alias configured.

    The alias is created once per session via ``mc alias set`` into a temporary
    config directory, keeping the suite hermetic: the developer's real ``mc``
    configuration is never read or written.
    """
    with tempfile.TemporaryDirectory(prefix="tgos3-mc-") as tmp:
        config_dir = Path(tmp)
        access_key = _first_env(_ACCESS_KEY_ENVS, "tgos3-test-access-key")
        secret_key = _first_env(_SECRET_KEY_ENVS, "tgos3-test-secret-key")
        _run_mc(
            config_dir,
            "alias",
            "set",
            _ALIAS,
            ENDPOINT,
            access_key,
            secret_key,
            "--api",
            "S3v4",
        )
        yield config_dir


@pytest.fixture(scope="session")
def mc_version_output(mc_config_dir: Path) -> str:
    """Captured ``mc --version`` output for the installed binary."""
    result = _run_mc(mc_config_dir, "--version")
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


@pytest.fixture
def bucket(mc_config_dir: Path) -> Iterator[str]:
    """Create a fresh bucket, yield its name, and best-effort clean it up."""
    name = _unique_bucket_name()
    _run_mc(mc_config_dir, "mb", _alias_path(name))
    try:
        yield name
    finally:
        # Force-remove the bucket and any residual objects. Best-effort:
        # teardown must not mask a test failure.
        _run_mc(mc_config_dir, "rb", "--force", _alias_path(name), check=False)


# --- Version pinning --------------------------------------------------------


def test_installed_mc_matches_pinned_version(mc_version_output: str) -> None:
    """The installed ``mc`` binary matches the pinned version (req 20.5).

    ``mc --version`` emits a line like
    ``mc version RELEASE.2024-08-13T05-33-17Z (commit-id=...)``; we assert the
    pinned ``RELEASE.*`` token appears in that output.
    """
    assert PINNED_MC_VERSION in mc_version_output, (
        f"expected pinned version {PINNED_MC_VERSION!r} in mc --version output, "
        f"got: {mc_version_output!r}"
    )


# --- Bucket management ------------------------------------------------------


def test_make_list_and_remove_bucket(mc_config_dir: Path) -> None:
    """Full bucket lifecycle via mc: ``mb`` then ``ls`` then ``rb`` (req 20.5)."""
    name = _unique_bucket_name()

    made = _run_mc(mc_config_dir, "mb", _alias_path(name))
    assert made.returncode == 0, made.stderr.decode("utf-8", "replace")

    try:
        # ``mc ls <alias>`` lists buckets; the new bucket must be present.
        listing = _run_mc(mc_config_dir, "ls", "--json", _ALIAS)
        names = {
            json.loads(line)["key"].rstrip("/")
            for line in listing.stdout.decode("utf-8").splitlines()
            if line.strip()
        }
        assert name in names
    finally:
        removed = _run_mc(mc_config_dir, "rb", "--force", _alias_path(name), check=False)
        assert removed.returncode == 0, removed.stderr.decode("utf-8", "replace")

    # After removal the bucket must no longer be listed.
    after = _run_mc(mc_config_dir, "ls", "--json", _ALIAS)
    after_names = {
        json.loads(line)["key"].rstrip("/")
        for line in after.stdout.decode("utf-8").splitlines()
        if line.strip()
    }
    assert name not in after_names


# --- Object management ------------------------------------------------------


def test_object_cp_stat_ls_rm_round_trip(
    mc_config_dir: Path, bucket: str, tmp_path: Path
) -> None:
    """``mc cp`` round-trips bytes; ``stat``/``ls`` report it; ``rm`` deletes it.

    Covers the object-management side of requirement 20.5: upload a local file,
    confirm it is listed and its metadata is reported, download it back with a
    byte-for-byte round-trip, then remove it.
    """
    key = "round-trip/object.bin"
    payload = b"tgobjects3 mc workflow matrix round-trip payload \x00\x01\x02\xff" * 64

    src = tmp_path / "upload.bin"
    src.write_bytes(payload)

    # Upload the local file into the bucket.
    uploaded = _run_mc(mc_config_dir, "cp", str(src), f"{_alias_path(bucket)}/{key}")
    assert uploaded.returncode == 0, uploaded.stderr.decode("utf-8", "replace")

    # ``mc ls`` recursively over the bucket lists the uploaded key.
    listing = _run_mc(mc_config_dir, "ls", "--json", "--recursive", _alias_path(bucket))
    listed_keys = {
        json.loads(line)["key"]
        for line in listing.stdout.decode("utf-8").splitlines()
        if line.strip()
    }
    assert key in listed_keys

    # ``mc stat`` reports object metadata, including its size.
    stat = _run_mc(mc_config_dir, "stat", "--json", f"{_alias_path(bucket)}/{key}")
    stat_meta = json.loads(stat.stdout.decode("utf-8"))
    assert stat_meta["size"] == len(payload)

    # Download the object back and assert an exact byte round-trip.
    dst = tmp_path / "download.bin"
    downloaded = _run_mc(mc_config_dir, "cp", f"{_alias_path(bucket)}/{key}", str(dst))
    assert downloaded.returncode == 0, downloaded.stderr.decode("utf-8", "replace")
    assert dst.read_bytes() == payload

    # Remove the object; it must no longer be stat-able.
    removed = _run_mc(mc_config_dir, "rm", f"{_alias_path(bucket)}/{key}")
    assert removed.returncode == 0, removed.stderr.decode("utf-8", "replace")

    missing = _run_mc(
        mc_config_dir, "stat", f"{_alias_path(bucket)}/{key}", check=False
    )
    assert missing.returncode != 0


def test_cp_multiple_objects_and_list(
    mc_config_dir: Path, bucket: str, tmp_path: Path
) -> None:
    """Multiple uploaded objects are all reported by a recursive ``mc ls``."""
    keys = [f"flat/{i:03d}.dat" for i in range(5)]
    for key in keys:
        src = tmp_path / Path(key).name
        src.write_bytes(key.encode("ascii"))
        put = _run_mc(mc_config_dir, "cp", str(src), f"{_alias_path(bucket)}/{key}")
        assert put.returncode == 0, put.stderr.decode("utf-8", "replace")

    listing = _run_mc(mc_config_dir, "ls", "--json", "--recursive", _alias_path(bucket))
    listed = {
        json.loads(line)["key"]
        for line in listing.stdout.decode("utf-8").splitlines()
        if line.strip()
    }
    assert set(keys) <= listed
