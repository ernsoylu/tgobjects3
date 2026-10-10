"""Proxy / SPA-fallback test for the console reverse proxy (task 13.3).

This integration test exercises the *real* ``deploy/nginx.console.conf`` running
inside a genuine nginx container against a tiny stub upstream, and asserts the
routing partition the design mandates:

**Property 8 — nginx routing partitions API paths from the SPA fallback.** *For
any* request path, the console nginx proxies it to the gateway when it is under
``/admin``, ``/health``, or ``/metrics`` and otherwise serves ``index.html`` (so
unknown client-side routes load the SPA rather than 404), while
``GET /healthz`` returns 200 for the container healthcheck.

**Validates: Requirements 8.3, 8.5, 8.6** (and the healthcheck target 8.4).

Harness
-------
Rather than parse the config and assert its text (which would never catch a
directive that nginx itself rejects or interprets differently), the suite stands
up the actual topology from ``deploy/docker-compose.yml``:

* a throwaway user-defined bridge network;
* a **stub upstream** container network-aliased ``gateway`` and listening on
  ``8080`` — exactly the ``proxy_pass http://gateway:8080`` target in the config
  — serving a distinctive marker body on every path so a proxied response is
  unambiguously identifiable;
* an **nginx** container running the unmodified ``deploy/nginx.console.conf``
  with a marker ``index.html`` dropped into its web root, published on a host
  port.

Requests then go through the published port with the standard library
(``urllib``) — the project treats ``httpx`` as unpinned and uses ``urllib`` for
HTTP in tests (see ``tests/soak/run_soak.py``). ``/admin/…``, ``/health``,
``/metrics`` must return the *upstream* marker (proven proxied); ``/``,
``/buckets``, ``/setup`` must return the *index.html* marker (SPA fallback); and
``/healthz`` must return 200. A hypothesis property generalises the fallback leg
over arbitrary client-side paths.

Both containers are the ``nginx:alpine`` image, so no application build is
needed and the stub upstream is just nginx returning a fixed body. The suite
needs a usable Docker daemon; when none is available (CI without Docker, or a
sandbox) it skips cleanly rather than failing, so ``make lint`` /
``make typecheck`` stay meaningful without a daemon present. No real network or
database is touched.
"""

from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Locations and constants
# ---------------------------------------------------------------------------

# tests/integration/test_nginx_console_proxy.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_NGINX_CONF = _REPO_ROOT / "deploy" / "nginx.console.conf"

# The config pins both the upstream host alias and port; keep the harness in
# lockstep with ``deploy/nginx.console.conf`` so a drift there fails the test.
_UPSTREAM_ALIAS = "gateway"
_UPSTREAM_PORT = 8080

# Distinctive bodies so a response's origin is unambiguous.
_UPSTREAM_MARKER = "UPSTREAM-GATEWAY-STUB"
_INDEX_MARKER = "SPA-INDEX-HTML"

# The base image for both containers. ``nginx:alpine`` serves static files and
# can stand in as the stub upstream (it just returns a fixed body), so the test
# needs no application image build.
_IMAGE = "nginx:alpine"

# Paths the config must proxy to the upstream (Requirement 8.3) ...
_PROXIED_PATHS = ("/admin", "/admin/setup/status", "/health", "/health/ready", "/metrics")
# ... and client-side routes that must fall back to the SPA (Requirements 8.5, 8.6).
_FALLBACK_PATHS = ("/", "/buckets", "/setup", "/settings", "/recovery")

_STARTUP_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Docker availability gate
# ---------------------------------------------------------------------------


def _docker_available() -> tuple[bool, str]:
    """Return ``(usable, reason)`` for the Docker CLI + daemon.

    The whole module skips with ``reason`` when Docker cannot run containers,
    mirroring the ``testcontainers`` skip convention used by the other
    integration suites.
    """

    if shutil.which("docker") is None:
        return False, "docker CLI is not installed"
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - env dependent
        return False, f"docker info failed to run: {exc}"
    if proc.returncode != 0:  # pragma: no cover - env dependent
        return False, "docker daemon is not usable (`docker info` failed)"
    return True, ""


_DOCKER_OK, _DOCKER_REASON = _docker_available()


def _skip_if_no_docker() -> None:
    if not _DOCKER_OK:
        pytest.skip(f"Docker is unavailable: {_DOCKER_REASON}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Reserve an ephemeral localhost port for the published nginx port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run a docker command, raising with captured output on failure."""

    proc = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`docker {' '.join(args)}` failed ({proc.returncode}):\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


@dataclass(frozen=True)
class _Response:
    status: int
    body: str


def _get(url: str, timeout: float = 5.0) -> _Response:
    """GET ``url`` with the standard library, returning status + body.

    HTTP error statuses (404, 502, …) are returned as a :class:`_Response`
    rather than raised, so assertions can inspect them directly.
    """

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost test URL
            return _Response(status=resp.status, body=resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:  # non-2xx still carries a body
        return _Response(status=exc.code, body=exc.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Container topology fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Topology:
    base_url: str


@pytest.fixture(scope="module")
def nginx_console() -> Iterator[_Topology]:
    """Bring up the stub upstream + the real nginx config on a shared network.

    Module-scoped so the (cheap but non-trivial) container start happens once
    for every assertion in this file. Everything created here is torn down in
    the finally block, including on a failed startup.
    """

    _skip_if_no_docker()
    assert _NGINX_CONF.is_file(), f"missing nginx config: {_NGINX_CONF}"

    suffix = secrets.token_hex(4)
    network = f"tgo-nginx-test-{suffix}"
    upstream_name = f"tgo-upstream-{suffix}"
    nginx_name = f"tgo-nginx-{suffix}"
    host_port = _free_port()

    # Write the stub-upstream config and the marker index.html to the host and
    # mount them read-only, rather than smuggling them through an in-container
    # ``sh -c "printf … > file"``: the config body contains ``$request_uri``,
    # braces and newlines that are fragile to pass through nested shell quoting,
    # whereas a bind mount hands nginx exactly the bytes we wrote.
    with TemporaryDirectory(prefix="tgo-nginx-test-") as tmp:
        tmp_dir = Path(tmp)
        upstream_conf = tmp_dir / "upstream.conf"
        # Any URI -> 200 with the upstream marker, so a proxied response is
        # provably distinguishable from the SPA index.
        upstream_conf.write_text(
            "server {\n"
            f"  listen {_UPSTREAM_PORT};\n"
            "  server_name _;\n"
            "  location / {\n"
            "    default_type text/plain;\n"
            f'    return 200 "{_UPSTREAM_MARKER} $request_uri\\n";\n'
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        index_html = tmp_dir / "index.html"
        index_html.write_text(
            f"<!doctype html><title>console</title><h1>{_INDEX_MARKER}</h1>\n",
            encoding="utf-8",
        )

        created_network = False
        try:
            # Shared user-defined bridge: containers resolve each other by name,
            # so the config's ``gateway`` alias resolves to the stub upstream.
            _run("network", "create", network)
            created_network = True

            # --- Stub upstream: nginx returning a fixed marker on every path. -
            _run(
                "run",
                "-d",
                "--name",
                upstream_name,
                "--network",
                network,
                "--network-alias",
                _UPSTREAM_ALIAS,
                "-v",
                f"{upstream_conf}:/etc/nginx/conf.d/default.conf:ro",
                _IMAGE,
            )

            # The real config uses a bare ``proxy_pass http://gateway:8080`` with
            # no ``resolver``, so nginx resolves the ``gateway`` alias *once at
            # startup* and refuses to boot if it cannot. Wait until the alias is
            # resolvable (upstream registered + listening) before starting nginx.
            _wait_until_upstream_resolvable(network)

            # --- nginx under test: the real deploy config + marker index.html. -
            _run(
                "run",
                "-d",
                "--name",
                nginx_name,
                "--network",
                network,
                "-p",
                f"127.0.0.1:{host_port}:80",
                "-v",
                # The unmodified deploy config, read-only, at nginx's conf path.
                f"{_NGINX_CONF}:/etc/nginx/conf.d/default.conf:ro",
                "-v",
                f"{index_html}:/usr/share/nginx/html/index.html:ro",
                _IMAGE,
            )

            base_url = f"http://127.0.0.1:{host_port}"
            _wait_until_ready(base_url, nginx_name)
            yield _Topology(base_url=base_url)
        finally:
            # Best-effort teardown; never mask a test failure with a cleanup error.
            for name in (nginx_name, upstream_name):
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if created_network:
                subprocess.run(
                    ["docker", "network", "rm", network],
                    capture_output=True,
                    text=True,
                    check=False,
                )


def _wait_until_upstream_resolvable(network: str) -> None:
    """Block until the ``gateway`` alias answers on the shared network.

    nginx (with a literal upstream host and no ``resolver``) resolves the name
    once at startup and aborts if it fails, so the stub upstream must be both
    DNS-resolvable *and* listening before nginx launches. A short-lived
    ``nginx:alpine`` container on the same network polls the alias with ``wget``.
    """

    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    # One resolve + connect attempt per invocation; the outer loop handles retry.
    probe = f"wget -q -O /dev/null http://{_UPSTREAM_ALIAS}:{_UPSTREAM_PORT}/"
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--entrypoint",
                "sh",
                _IMAGE,
                "-c",
                probe,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0:
            return
        time.sleep(0.3)
    raise RuntimeError(
        f"stub upstream '{_UPSTREAM_ALIAS}:{_UPSTREAM_PORT}' did not become "
        f"resolvable within {_STARTUP_TIMEOUT_S:.0f}s"
    )


def _wait_until_ready(base_url: str, nginx_name: str) -> None:
    """Poll ``/healthz`` until nginx answers 200, or fail with its logs."""

    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    last_err: str = "no attempt made"
    while time.monotonic() < deadline:
        try:
            resp = _get(f"{base_url}/healthz", timeout=2.0)
            if resp.status == 200:
                return
            last_err = f"/healthz returned {resp.status}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_err = str(exc)
        time.sleep(0.3)

    logs = subprocess.run(
        ["docker", "logs", nginx_name],
        capture_output=True,
        text=True,
        check=False,
    )
    raise RuntimeError(
        f"nginx did not become ready within {_STARTUP_TIMEOUT_S:.0f}s "
        f"(last error: {last_err})\n--- nginx logs ---\n{logs.stdout}\n{logs.stderr}"
    )


# ---------------------------------------------------------------------------
# Example assertions (concrete scenarios from the design's deploy test)
# ---------------------------------------------------------------------------


def test_healthz_returns_200(nginx_console: _Topology) -> None:
    """`GET /healthz` is the container healthcheck target (Requirement 8.4)."""

    resp = _get(f"{nginx_console.base_url}/healthz")
    assert resp.status == 200
    assert resp.body.strip() == "ok"


@pytest.mark.parametrize("path", _PROXIED_PATHS)
def test_api_paths_proxy_to_gateway(nginx_console: _Topology, path: str) -> None:
    """`/admin`, `/health`, `/metrics` reach the upstream (Requirement 8.3)."""

    resp = _get(f"{nginx_console.base_url}{path}")
    assert resp.status == 200
    assert _UPSTREAM_MARKER in resp.body, (
        f"{path} should be proxied to the gateway stub, got: {resp.body!r}"
    )
    assert _INDEX_MARKER not in resp.body


@pytest.mark.parametrize("path", _FALLBACK_PATHS)
def test_spa_routes_fall_back_to_index(nginx_console: _Topology, path: str) -> None:
    """`/`, `/buckets`, `/setup`, … serve index.html (Requirements 8.5, 8.6)."""

    resp = _get(f"{nginx_console.base_url}{path}")
    assert resp.status == 200
    assert _INDEX_MARKER in resp.body, (
        f"{path} should fall back to the SPA index, got: {resp.body!r}"
    )
    assert _UPSTREAM_MARKER not in resp.body


# ---------------------------------------------------------------------------
# Property 8: the routing partition holds for arbitrary client-side paths
# ---------------------------------------------------------------------------

# A client-side route is any path that is NOT under one of the proxied prefixes.
# We generate path segments from an unreserved-ish alphabet and join them, then
# require the result to avoid the API prefixes — every such path must load the
# SPA rather than 404.
# Restrict to URL-safe ASCII so the generated path can go on the HTTP request
# line verbatim (no percent-encoding) — real SPA route segments look like this,
# and the property is about path *routing*, not URL-encoding behaviour.
_SEGMENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=12,
)
_API_PREFIXES = ("/admin", "/health", "/metrics")


@st.composite
def _spa_paths(draw: st.DrawFn) -> str:
    segments = draw(st.lists(_SEGMENT, min_size=1, max_size=4))
    path = "/" + "/".join(segments)
    # Keep the property on the fallback side of the partition only.
    assume_ok = not any(path == pfx or path.startswith(pfx + "/") for pfx in _API_PREFIXES)
    if not assume_ok:
        path = "/app" + path  # push it clear of the API prefixes
    return path


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(path=_spa_paths())
def test_property8_unknown_client_routes_load_spa(nginx_console: _Topology, path: str) -> None:
    """Property 8 (fallback leg): any non-API path loads the SPA, never 404s.

    **Validates: Requirements 8.5, 8.6**
    """

    resp = _get(f"{nginx_console.base_url}{path}")
    assert resp.status == 200, f"{path} should not 404 under the SPA fallback"
    assert _INDEX_MARKER in resp.body
    assert _UPSTREAM_MARKER not in resp.body


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(suffix=st.lists(_SEGMENT, min_size=0, max_size=3))
def test_property8_api_prefixes_always_proxy(nginx_console: _Topology, suffix: list[str]) -> None:
    """Property 8 (proxy leg): any path under an API prefix is proxied.

    **Validates: Requirement 8.3**
    """

    for prefix in _API_PREFIXES:
        path = prefix + ("/" + "/".join(suffix) if suffix else "")
        resp = _get(f"{nginx_console.base_url}{path}")
        assert resp.status == 200, f"{path} should be proxied, got {resp.status}"
        assert _UPSTREAM_MARKER in resp.body, f"{path} should reach the gateway stub"
        assert _INDEX_MARKER not in resp.body
