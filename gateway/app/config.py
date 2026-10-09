"""Gateway configuration — the single source of truth for runtime settings.

This module defines :class:`Settings`, a Pydantic v2
:class:`pydantic_settings.BaseSettings` model that materialises every
configuration variable from the design's Configuration table. All settings are
read from environment variables (optionally seeded from a ``.env`` file). The
model is the authoritative config object: downstream components (database
session factory, workers, S3/admin servers) consume a constructed
:class:`Settings` instance rather than reading the environment directly.

Normalisation of human-readable forms
-------------------------------------
Several design defaults are expressed as durations (``"7d"``, ``"24h"``) or
sizes (``"16GiB"``, ``"4GiB"``). To keep the rest of the codebase arithmetic-
friendly, those fields accept human-readable strings on input and normalise to
plain integers:

* duration fields → **seconds** (``int``)
* size fields → **bytes** (``int``)

Numeric input (``int``/``float`` or an all-digit string) is accepted verbatim
and interpreted directly as seconds / bytes, so an operator may set either
``GC_GRACE=7d`` or ``GC_GRACE=604800``.

Startup validation (requirement 1.3)
-------------------------------------
:func:`load_settings` constructs the model and, when one or more *required*
variables (``TGOS3_MASTER_KEY``, ``DATABASE_URL``) are missing, prints each
missing variable name on its own line to ``stderr`` and raises
:class:`SystemExit` with a non-zero code. This happens before any port is
bound, satisfying the design's "Startup Failures" contract. Range violations
and other validation failures are likewise reported to ``stderr`` and cause a
non-zero exit.
"""

from __future__ import annotations

import re
import sys
from typing import Annotated, Any, Final

from pydantic import BeforeValidator, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Required-variable registry
# ---------------------------------------------------------------------------

#: Environment variable names that have no default and MUST be supplied by the
#: operator. Used by :func:`load_settings` to produce the "each missing
#: variable on its own line" report (requirement 1.3).
REQUIRED_ENV_VARS: Final[tuple[str, ...]] = ("TGOS3_MASTER_KEY", "DATABASE_URL")

#: Minimum accepted length of the master key, in characters (requirement 1.2:
#: "master key (minimum 16 characters)").
MASTER_KEY_MIN_LENGTH: Final[int] = 16


# ---------------------------------------------------------------------------
# Human-readable duration / size parsing
# ---------------------------------------------------------------------------

#: Multipliers for duration suffixes, expressed in seconds.
_DURATION_UNITS: Final[dict[str, int]] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 60 * 60 * 24,
    "w": 60 * 60 * 24 * 7,
}

#: Multipliers for size suffixes, expressed in bytes. Both IEC binary units
#: (KiB, MiB, …) and their bare-letter short forms (K, M, …) are treated as
#: powers of 1024, matching the design defaults such as ``16GiB``.
_SIZE_UNITS: Final[dict[str, int]] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kib": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mib": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gib": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tib": 1024**4,
    "tb": 1024**4,
}

_DURATION_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<value>\d+)\s*(?P<unit>[a-zA-Z]+)?\s*$"
)
_SIZE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<value>\d+)\s*(?P<unit>[a-zA-Z]+)?\s*$"
)


def _parse_duration(value: Any) -> int:
    """Normalise a duration to a whole number of seconds.

    Accepts an existing ``int``, an all-digit string (interpreted as seconds),
    or a suffixed string such as ``"24h"``, ``"7d"``, ``"90m"``, ``"604800s"``.

    Raises:
        ValueError: If the value is negative or cannot be parsed as a duration.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly.
        raise ValueError("duration must be a number or duration string")
    if isinstance(value, int):
        seconds = value
    else:
        match = _DURATION_RE.match(str(value))
        if match is None:
            raise ValueError(f"invalid duration: {value!r}")
        unit = (match.group("unit") or "s").lower()
        if unit not in _DURATION_UNITS:
            raise ValueError(f"unknown duration unit {unit!r} in {value!r}")
        seconds = int(match.group("value")) * _DURATION_UNITS[unit]
    if seconds < 0:
        raise ValueError("duration must be non-negative")
    return seconds


def _parse_size(value: Any) -> int:
    """Normalise a byte-size to a whole number of bytes.

    Accepts an existing ``int``, an all-digit string (interpreted as bytes),
    or a suffixed string such as ``"16GiB"``, ``"4GiB"``, ``"512MB"``. Binary
    (1024-based) multipliers are used for all unit suffixes.

    Raises:
        ValueError: If the value is negative or cannot be parsed as a size.
    """
    if isinstance(value, bool):
        raise ValueError("size must be a number or size string")
    if isinstance(value, int):
        size = value
    else:
        match = _SIZE_RE.match(str(value))
        if match is None:
            raise ValueError(f"invalid size: {value!r}")
        unit = (match.group("unit") or "").lower()
        if unit not in _SIZE_UNITS:
            raise ValueError(f"unknown size unit {unit!r} in {value!r}")
        size = int(match.group("value")) * _SIZE_UNITS[unit]
    if size < 0:
        raise ValueError("size must be non-negative")
    return size


#: Reusable annotated types that run the normalising validators before the
#: standard ``int`` coercion / range checks.
DurationSeconds = Annotated[int, BeforeValidator(_parse_duration)]
SizeBytes = Annotated[int, BeforeValidator(_parse_size)]


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Materialised gateway configuration.

    Field names match the environment variable names from the design's
    Configuration table exactly (case-insensitive lookup is disabled so the
    mapping is one-to-one with the documented upper-case names). Duration
    fields are stored as integer seconds and size fields as integer bytes
    after normalisation; see the module docstring.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        frozen=True,
    )

    # --- Required, env-only ------------------------------------------------
    TGOS3_MASTER_KEY: str = Field(
        min_length=MASTER_KEY_MIN_LENGTH,
        description="Root symmetric key; wraps all bucket KEKs. Minimum 16 chars.",
    )
    DATABASE_URL: str = Field(
        min_length=1,
        description="SQLAlchemy/asyncpg connection URL for PostgreSQL.",
    )

    # --- Network -----------------------------------------------------------
    S3_PORT: int = Field(default=9000, ge=1, le=65535)
    ADMIN_PORT: int = Field(default=8080, ge=1, le=65535)
    S3_REGION: str = Field(default="us-east-1", min_length=1)

    # --- Storage format ----------------------------------------------------
    CHUNK_RECORDS: int = Field(default=512, ge=1, description="Records per Telegram document.")
    DEFAULT_DURABILITY: str = Field(default="sync", description="'sync' or 'async' per bucket.")

    # --- Retention / checkpointing ----------------------------------------
    GC_GRACE: DurationSeconds = Field(
        default=7 * 24 * 60 * 60,  # 7d
        ge=0,
        description="Retention (seconds) before Telegram deletion.",
    )
    CHECKPOINT_INTERVAL: DurationSeconds = Field(
        default=24 * 60 * 60,  # 24h
        ge=60,
        le=86400,
        description="Checkpoint cadence in seconds; range 60–86400.",
    )

    # --- Staging -----------------------------------------------------------
    STAGING_MAX_SIZE: SizeBytes = Field(
        default=16 * 1024**3,  # 16GiB
        ge=1,
        description="Maximum total in-flight staging bytes.",
    )
    MAX_OBJECT_SIZE: SizeBytes = Field(
        default=5 * 1024**4,  # 5TiB — S3 single-object ceiling
        ge=1,
        description="Largest permitted object body; PutObject returns 413 above this.",
    )

    # --- Record cache ------------------------------------------------------
    CACHE_MAX_SIZE: SizeBytes = Field(
        default=4 * 1024**3,  # 4GiB
        ge=1,
        description="Maximum on-disk record cache size in bytes.",
    )
    CACHE_HALF_LIFE: DurationSeconds = Field(
        default=24 * 60 * 60,  # 24h
        ge=1,
        description="Half-life H (seconds) in the cache ranking formula.",
    )
    CACHE_SIZE_BIAS: float = Field(
        default=0.5,
        ge=0.0,
        description="Exponent α in the cache priority formula.",
    )
    CACHE_HIGH_WATERMARK: float = Field(
        default=0.90,
        gt=0.0,
        le=1.0,
        description="Cache fill fraction that triggers eviction.",
    )
    CACHE_LOW_WATERMARK: float = Field(
        default=0.80,
        gt=0.0,
        le=1.0,
        description="Cache fill fraction eviction drains down to.",
    )
    READ_AHEAD: int = Field(
        default=4,
        ge=0,
        le=32,
        description="Read-ahead blocks fetched on a cache miss; range 0–32.",
    )
    CACHE_ENCRYPT: bool = Field(
        default=False,
        description="Encrypt cached record files at rest when true.",
    )

    # --- Upload / multipart -----------------------------------------------
    UPLOAD_CONCURRENCY: int = Field(
        default=2,
        ge=1,
        description="Parallel chunk uploads per blob; must be >= 1.",
    )
    MULTIPART_EXPIRY: DurationSeconds = Field(
        default=24 * 60 * 60,  # 24h
        ge=3600,  # min 1h
        description="Multipart upload expiry in seconds; minimum 1 hour (3600 s).",
    )

    # --- Cross-field validation -------------------------------------------
    @model_validator(mode="after")
    def _check_invariants(self) -> Settings:
        """Enforce invariants that span more than one field.

        Raising here surfaces as a :class:`~pydantic.ValidationError`, so these
        failures flow through the same :func:`load_settings` reporting path as
        single-field range violations.
        """
        if self.DEFAULT_DURABILITY not in ("sync", "async"):
            raise ValueError(
                f"DEFAULT_DURABILITY must be 'sync' or 'async', got "
                f"{self.DEFAULT_DURABILITY!r}"
            )
        if self.CACHE_LOW_WATERMARK >= self.CACHE_HIGH_WATERMARK:
            raise ValueError(
                "CACHE_LOW_WATERMARK must be strictly less than "
                "CACHE_HIGH_WATERMARK"
            )
        return self


# ---------------------------------------------------------------------------
# Startup loader
# ---------------------------------------------------------------------------


def _missing_required_fields(error: ValidationError) -> list[str]:
    """Extract the set of required variables reported missing by ``error``.

    Only fields in :data:`REQUIRED_ENV_VARS` that Pydantic flagged as
    ``missing`` are returned, preserving the registry's declaration order so
    the stderr report is deterministic.
    """
    missing: set[str] = set()
    for err in error.errors():
        if err.get("type") != "missing":
            continue
        loc = err.get("loc", ())
        if loc and isinstance(loc[0], str):
            missing.add(loc[0])
    return [name for name in REQUIRED_ENV_VARS if name in missing]


def load_settings() -> Settings:
    """Construct :class:`Settings`, failing fast on invalid configuration.

    On success, returns the fully validated settings object. On failure the
    function writes a human-readable diagnostic to ``stderr`` and raises
    :class:`SystemExit` with a non-zero code *before* any port is bound
    (requirement 1.3):

    * If one or more required variables are missing, each missing variable name
      is emitted on its own line.
    * Any other validation failure (range violations, bad enum values, …) is
      reported with its field location and message, one per line.

    Raises:
        SystemExit: With a non-zero code whenever configuration is invalid.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values sourced from env
    except ValidationError as exc:
        missing = _missing_required_fields(exc)
        if missing:
            print(
                "Missing required environment variable(s):",
                file=sys.stderr,
            )
            for name in missing:
                print(name, file=sys.stderr)
        else:
            print("Invalid configuration:", file=sys.stderr)
            for err in exc.errors():
                loc = ".".join(str(part) for part in err.get("loc", ()))
                msg = err.get("msg", "invalid value")
                print(f"{loc}: {msg}", file=sys.stderr)
        raise SystemExit(1) from exc
