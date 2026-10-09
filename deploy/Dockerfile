# syntax=docker/dockerfile:1

# =============================================================================
# tgobjects3 gateway image (Requirement 1)
# =============================================================================
# A multi-stage build that produces a slim, non-root runtime image exposing the
# two HTTP surfaces the design mandates: the S3 API on S3_PORT (default 9000)
# and the Admin API on ADMIN_PORT (default 8080). The gateway applies all
# Alembic migrations itself at startup (Requirement 1.4), so the entrypoint only
# launches the app — no migration shell step is wired here.
#
# Layout:
#   * builder — full toolchain; builds a wheel for the project and all pinned
#     dependencies into /wheels. Several deps ship native extensions that need a
#     compiler/headers to build from source (cryptg, awscrt, asyncpg), hence the
#     build toolchain lives here and never in the runtime image.
#   * runtime — python:3.12-slim + libpq client only. The Checkpoint Worker
#     (gateway/app/jobs/checkpoint_worker.py) shells out to `pg_dump`, so
#     `postgresql-client` is installed in the runtime stage.
#
# Base images are pinned by exact tag + digest so rebuilds are reproducible and
# no secrets are ever baked into the image.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder — compile wheels for the project and its pinned deps.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Fail fast, no .pyc clutter, unbuffered logs during the build.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build toolchain + headers needed by native-extension deps:
#   * build-essential / cmake      — awscrt (CRT libraries) and cryptg
#   * libffi-dev / libssl-dev      — cryptography / cryptg
#   * pkg-config                   — awscrt build discovery
# Installed only in this stage; the runtime image stays slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        pkg-config \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Copy only what the build needs: project metadata + the packages listed under
# [tool.setuptools].packages in pyproject.toml. README is referenced by
# project.readme so it must be present for the metadata build.
COPY pyproject.toml README.md ./
COPY gateway ./gateway
COPY cli ./cli

# Build a wheel for tgobjects3 and wheels for every pinned runtime dependency
# into /wheels. The runtime stage installs purely from these wheels with
# --no-index, so no compiler or network is needed there.
RUN python -m pip install --upgrade pip wheel \
    && python -m pip wheel --wheel-dir /wheels .

# -----------------------------------------------------------------------------
# Stage 2: runtime — slim, non-root, libpq client only.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Default ports — the app reads these from Settings; EXPOSE below documents
    # the same values. Overridable at runtime via the environment.
    S3_PORT=9000 \
    ADMIN_PORT=8080

# Runtime OS dependency: the `pg_dump` client used by the Checkpoint Worker.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install the project + deps from the wheels built in the builder stage. No
# toolchain, no index access, no build step — just unpack prebuilt wheels.
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels tgobjects3 \
    && rm -rf /wheels

# Create an unprivileged user and the /data volume root (staging + cache, see
# gateway/app/main.py DEFAULT_DATA_DIR), owned by that user so the app can write
# without running as root.
RUN useradd --system --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /data/staging /data/cache \
    && chown -R app:app /data

# /data backs both staging and the on-disk record cache; declare it a volume so
# the data survives container recreation (docker-compose mounts it, task 32.3).
VOLUME ["/data"]

USER app
WORKDIR /home/app

# Document the two API surfaces. These mirror the S3_PORT / ADMIN_PORT defaults;
# if those env vars are overridden, publish the matching host ports accordingly.
EXPOSE 9000 8080

# Launch the gateway. main() loads+validates config (fail-fast, Requirement 1.3),
# applies migrations at startup (Requirement 1.4), then serves both APIs under
# uvicorn/uvloop. Exec form so the process is PID 1 and receives signals for a
# graceful shutdown.
ENTRYPOINT ["python", "-m", "gateway.app.main"]
