"""Workflow matrix tests for the rclone S3 client (task 30.4).

These tests drive the pinned ``rclone`` binary as a subprocess against a
*running* gateway endpoint, exercising the rclone row of the workflow matrix
defined in the design's Testing Strategy: ``copy``, ``sync`` and a FUSE
``mount`` with read-only assertions.

Design intent (Requirement 20.3):

* **Requirement 20.3** — the gateway must pass the workflow matrix for rclone at
  the version recorded in ``tests/workflow_matrix/pin.json``, covering the
  ``copy``, ``sync`` and ``mount`` operations using the tool's standard S3
  remote configuration.

Operations covered:

* ``rclone copy`` of a local directory to the remote, verified by an
  ``rclone ls`` listing and an ``rclone cat`` byte-for-byte round-trip.
* ``rclone sync`` making the remote mirror the source, including that deleting a
  file on the source propagates the deletion to the remote on the next sync.
* ``rclone mount`` exposing a bucket as a read-only FUSE filesystem: a file is
  read back through the mount and a write through the mount is rejected.

Collection and execution model
-------------------------------
The suite is always collectable so CI stays green-by-skip when the matrix
environment is not provisioned. The whole module is skipped at collection time
unless both of the following are present:

* the ``rclone`` binary on ``PATH`` (ideally at the pinned version), and
* a live gateway S3 endpoint named by the ``TGOS3_TEST_ENDPOINT`` environment
  variable.

The mount test is skipped additionally when FUSE is unavailable (no
``/dev/fuse`` device and no ``fusermount`` helper), since many CI environments
cannot mount FUSE filesystems.

Credentials and region are read from the environment (with gateway-specific
``TGOS3_TEST_*`` overrides taking precedence over the conventional AWS
variables) and injected into each subprocess via ``RCLONE_CONFIG_<REMOTE>_*``
environment variables, so the suite stays hermetic and does not depend on an
ambient ``rclone.conf``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
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

#: Name of the rclone remote configured purely via environment variables.
_REMOTE = "tgos3"

_RCLONE_BINARY = shutil.which("rclone")
_ENDPOINT = os.environ.get(ENDPOINT_ENV)


def _first_env(names: tuple[str, ...], default: str) -> str:
    """Return the first set, non-empty value among ``names`` else ``default``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _load_pinned_version() -> str:
    """Read the pinned rclone version from ``pin.json``."""
    data = json.loads(_PIN_FILE.read_text(encoding="utf-8"))
    version = data["rclone"]
    assert isinstance(version, str) and version, "pin.json rclone must be a version string"
    return version


PINNED_RCLONE_VERSION = _load_pinned_version()

# Skip the whole module unless both the binary and a live endpoint are present.
# This keeps the suite collectable everywhere while only executing when the
# matrix environment has been provisioned.
if _RCLONE_BINARY is None:
    pytest.skip(
        "rclone binary not found on PATH; skipping rclone workflow matrix suite",
        allow_module_level=True,
    )
if not _ENDPOINT:
    pytest.skip(
        f"{ENDPOINT_ENV} is unset; skipping rclone workflow matrix suite "
        "(set it to a running gateway S3 endpoint to run these tests)",
        allow_module_level=True,
    )

# Past this point the binary and endpoint are known-present; narrow the types.
RCLONE_BINARY: str = _RCLONE_BINARY
ENDPOINT: str = _ENDPOINT


# --- FUSE availability ------------------------------------------------------


def _fuse_available() -> bool:
    """Return whether a FUSE mount is likely to work in this environment.

    rclone's ``mount`` command needs both the kernel ``/dev/fuse`` device and
    the ``fusermount`` (or ``fusermount3``) helper to unmount cleanly. If either
    is missing the mount cannot succeed, so the mount test is skipped.
    """
    if not Path("/dev/fuse").exists():
        return False
    return shutil.which("fusermount") is not None or shutil.which("fusermount3") is not None


# --- Subprocess helpers -----------------------------------------------------


def _rclone_env() -> dict[str, str]:
    """Build the environment for an ``rclone`` subprocess.

    The remote is defined entirely through ``RCLONE_CONFIG_<REMOTE>_*``
    variables so no ``rclone.conf`` file is required; credentials and region
    come from the test environment (with gateway-specific overrides). This keeps
    the suite hermetic and independent of any ambient rclone configuration.
    """
    prefix = f"RCLONE_CONFIG_{_REMOTE.upper()}_"
    env = dict(os.environ)
    env[prefix + "TYPE"] = "s3"
    env[prefix + "PROVIDER"] = "Other"
    env[prefix + "ENDPOINT"] = ENDPOINT
    env[prefix + "ACCESS_KEY_ID"] = _first_env(_ACCESS_KEY_ENVS, "tgos3-test-access-key")
    env[prefix + "SECRET_ACCESS_KEY"] = _first_env(_SECRET_KEY_ENVS, "tgos3-test-secret-key")
    env[prefix + "REGION"] = _first_env(_REGION_ENVS, "us-east-1")
    # Path-style addressing: the gateway serves buckets as a path, not a vhost.
    env[prefix + "FORCE_PATH_STYLE"] = "true"
    # Keep rclone non-interactive and quiet about config confirmation prompts.
    env["RCLONE_CONFIG"] = os.devnull
    return env


def _run_rclone(
    *args: str,
    check: bool = True,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``rclone <args>`` with the env-configured remote and return the result.

    Output is captured as bytes so ``cat`` round-trips can be asserted exactly.
    """
    cmd = [RCLONE_BINARY, *args]
    return subprocess.run(  # noqa: S603 - args are test-controlled, not user input
        cmd,
        capture_output=True,
        check=check,
        env=_rclone_env(),
        timeout=timeout,
    )


def _unique_bucket_name() -> str:
    """Generate a DNS-compliant, unique bucket name for a test."""
    return f"tgos3-rclone-{uuid.uuid4().hex[:16]}"


def _remote_path(bucket: str, path: str = "") -> str:
    """Build an ``rclone`` remote path like ``tgos3:bucket/path``."""
    base = f"{_REMOTE}:{bucket}"
    return f"{base}/{path}" if path else base


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="session")
def rclone_version_output() -> str:
    """Captured ``rclone version`` output for the installed binary."""
    result = _run_rclone("version")
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


@pytest.fixture
def bucket() -> Iterator[str]:
    """Create a fresh bucket, yield its name, and best-effort clean it up."""
    name = _unique_bucket_name()
    _run_rclone("mkdir", _remote_path(name))
    try:
        yield name
    finally:
        # Purge removes the bucket and any residual objects. Best-effort:
        # teardown must not mask a test failure.
        _run_rclone("purge", _remote_path(name), check=False)


# --- Version pinning --------------------------------------------------------


def test_installed_rclone_matches_pinned_version(rclone_version_output: str) -> None:
    """The installed ``rclone`` binary matches the pinned version (req 20.3).

    ``rclone version`` emits a first line like ``rclone v1.68.0``; we assert the
    pinned ``v<version>`` token is present in the output.
    """
    expected = f"v{PINNED_RCLONE_VERSION}"
    assert expected in rclone_version_output, (
        f"expected {expected!r} in rclone version output, got: {rclone_version_output!r}"
    )


# --- rclone copy ------------------------------------------------------------


def test_copy_local_dir_round_trip(bucket: str, tmp_path: Path) -> None:
    """``rclone copy`` uploads a local tree; ls + cat confirm a round-trip."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    files = {
        "alpha.txt": b"the quick brown fox\x00\x01\x02",
        "nested/beta.bin": bytes(range(256)) * 8,
    }
    for rel, payload in files.items():
        target = src_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    copy = _run_rclone("copy", str(src_dir), _remote_path(bucket))
    assert copy.returncode == 0, copy.stderr.decode("utf-8", "replace")

    # ``rclone ls`` lists ``<size> <path>`` lines; collect the remote paths.
    listing = _run_rclone("ls", _remote_path(bucket))
    listed = {
        line.split(maxsplit=1)[1]
        for line in listing.stdout.decode("utf-8").splitlines()
        if line.strip()
    }
    assert listed == set(files)

    # Each object must come back byte-for-byte via ``rclone cat``.
    for rel, payload in files.items():
        cat = _run_rclone("cat", _remote_path(bucket, rel))
        assert cat.stdout == payload


# --- rclone sync ------------------------------------------------------------


def test_sync_mirrors_source_including_deletions(bucket: str, tmp_path: Path) -> None:
    """``rclone sync`` makes the remote mirror the source, deletions included.

    After an initial sync the remote matches the source. Removing a file locally
    and syncing again must delete the corresponding object on the remote, since
    ``sync`` (unlike ``copy``) prunes extraneous destination files.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    keep = src_dir / "keep.txt"
    drop = src_dir / "drop.txt"
    keep.write_bytes(b"retained across syncs")
    drop.write_bytes(b"present only in the first sync")

    first = _run_rclone("sync", str(src_dir), _remote_path(bucket))
    assert first.returncode == 0, first.stderr.decode("utf-8", "replace")

    after_first = _remote_file_set(bucket)
    assert after_first == {"keep.txt", "drop.txt"}

    # Delete one source file and re-sync; the deletion must propagate.
    drop.unlink()
    second = _run_rclone("sync", str(src_dir), _remote_path(bucket))
    assert second.returncode == 0, second.stderr.decode("utf-8", "replace")

    after_second = _remote_file_set(bucket)
    assert after_second == {"keep.txt"}


def _remote_file_set(bucket: str) -> set[str]:
    """Return the set of object paths under ``bucket`` via ``rclone ls``."""
    listing = _run_rclone("ls", _remote_path(bucket))
    return {
        line.split(maxsplit=1)[1]
        for line in listing.stdout.decode("utf-8").splitlines()
        if line.strip()
    }


# --- rclone mount (FUSE, read-only) -----------------------------------------


@pytest.mark.skipif(
    not _fuse_available(),
    reason="FUSE is unavailable (no /dev/fuse or fusermount helper); skipping mount test",
)
def test_mount_read_only(bucket: str, tmp_path: Path) -> None:
    """``rclone mount --read-only`` exposes objects read-only over FUSE.

    A file placed in the bucket is readable through the mount, while a write
    attempt through the mount is rejected because the filesystem is read-only.
    """
    key = "mounted.txt"
    payload = b"content served through a read-only FUSE mount"

    # Seed an object so there is something to read back through the mount.
    src = tmp_path / "seed.txt"
    src.write_bytes(payload)
    _run_rclone("copyto", str(src), _remote_path(bucket, key))

    mount_point = tmp_path / "mnt"
    mount_point.mkdir()

    proc = subprocess.Popen(  # noqa: S603 - args are test-controlled, not user input
        [
            RCLONE_BINARY,
            "mount",
            _remote_path(bucket),
            str(mount_point),
            "--read-only",
            # Foreground so the process owns the mount lifetime; we tear it down.
            "--vfs-cache-mode",
            "off",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_rclone_env(),
    )
    try:
        _wait_for_mount(mount_point, proc)

        # The seeded object is visible and reads back byte-for-byte.
        mounted_file = mount_point / key
        assert mounted_file.read_bytes() == payload

        # A write through the mount must be rejected: the mount is read-only.
        with pytest.raises(OSError):
            (mount_point / "should-not-write.txt").write_bytes(b"nope")
    finally:
        _unmount(mount_point, proc)


def _wait_for_mount(
    mount_point: Path,
    proc: subprocess.Popen[bytes],
    timeout: float = 30.0,
) -> None:
    """Block until ``mount_point`` is an active mount or raise on failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate()
            raise AssertionError(
                f"rclone mount exited early (rc={proc.returncode}): "
                f"{err.decode('utf-8', 'replace')}"
            )
        if os.path.ismount(mount_point):
            return
        time.sleep(0.25)
    raise AssertionError(f"rclone mount did not become ready within {timeout}s")


def _unmount(mount_point: Path, proc: subprocess.Popen[bytes]) -> None:
    """Best-effort unmount of ``mount_point`` and termination of ``proc``."""
    for helper in ("fusermount3", "fusermount"):
        tool = shutil.which(helper)
        if tool is not None:
            subprocess.run(  # noqa: S603 - helper path from PATH lookup
                [tool, "-u", str(mount_point)],
                capture_output=True,
                check=False,
                timeout=30,
            )
            break
    try:
        proc.terminate()
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
