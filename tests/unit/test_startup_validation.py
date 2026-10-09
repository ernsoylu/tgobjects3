"""Example-based tests for configuration startup validation.

Covers the fail-fast missing-required-variable path of
:func:`gateway.app.config.load_settings` (Requirement 1.3):

    WHEN ``docker compose up`` is executed for the first time and required
    environment variables are absent, THE Gateway SHALL refuse to start and
    emit a human-readable error to stderr listing each missing variable on its
    own line.

The two scenarios asserted here are the concrete examples named by task 14.2:

* **One variable missing** — ``TGOS3_MASTER_KEY`` is absent while
  ``DATABASE_URL`` is present. ``load_settings`` must exit non-zero and the
  stderr report must list ``TGOS3_MASTER_KEY`` on its own line (and must not
  name the variable that *was* supplied).
* **Both variables missing** — neither required variable is present. The
  stderr report must list both ``TGOS3_MASTER_KEY`` and ``DATABASE_URL``, each
  on its own line.

Environment isolation
----------------------
``Settings`` sources required values from the process environment and,
secondarily, from a ``.env`` file in the current working directory. Each test
deletes the required variables from ``os.environ`` and changes into an empty
temporary directory so a stray ``.env`` in the repository root cannot satisfy a
variable the test means to leave missing. Optional settings keep their declared
defaults and are irrelevant to the missing-required-variable path under test.

**Validates: Requirements 1.3**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gateway.app.config import REQUIRED_ENV_VARS, load_settings

# A master key long enough to satisfy the 16-character minimum, used when a
# test wants that one variable present so it is *not* reported missing.
_VALID_MASTER_KEY = "x" * 16
_VALID_DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/tgo3"


@pytest.fixture(autouse=True)
def _isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Strip required vars and move to an empty cwd so no ``.env`` leaks in.

    Clearing the environment makes the "missing" scenarios deterministic
    regardless of the developer's shell, and ``chdir`` into a fresh temporary
    directory prevents the repository's own ``.env`` (if any) from supplying a
    value the test intends to omit.
    """
    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def _stderr_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Return the non-empty lines written to stderr by ``load_settings``."""
    captured = capsys.readouterr()
    return [line for line in captured.err.splitlines() if line.strip()]


def test_missing_master_key_lists_only_that_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing ``TGOS3_MASTER_KEY`` alone is reported on its own stderr line.

    ``DATABASE_URL`` is supplied, so the fail-fast report must name
    ``TGOS3_MASTER_KEY`` (on a line by itself) and must not name the variable
    that was present.
    """
    monkeypatch.setenv("DATABASE_URL", _VALID_DATABASE_URL)

    with pytest.raises(SystemExit) as exc_info:
        load_settings()

    # Non-zero exit before any port is bound.
    assert exc_info.value.code != 0

    lines = _stderr_lines(capsys)
    assert "TGOS3_MASTER_KEY" in lines
    # The supplied variable must not be reported as missing.
    assert "DATABASE_URL" not in lines


def test_missing_both_required_vars_lists_both(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With neither required variable set, both are listed, each on its own line.

    The autouse fixture already removed both variables, so this is the
    first-boot "nothing configured" case: the report must enumerate every
    missing required variable.
    """
    with pytest.raises(SystemExit) as exc_info:
        load_settings()

    assert exc_info.value.code != 0

    lines = _stderr_lines(capsys)
    assert "TGOS3_MASTER_KEY" in lines
    assert "DATABASE_URL" in lines
    # Each missing variable appears exactly once, on its own line.
    assert lines.count("TGOS3_MASTER_KEY") == 1
    assert lines.count("DATABASE_URL") == 1


def test_all_required_vars_present_loads_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control case: with both required vars set, ``load_settings`` succeeds.

    This guards against the missing-variable path firing spuriously and
    confirms the two negative tests above are exercising a real difference.
    """
    monkeypatch.setenv("TGOS3_MASTER_KEY", _VALID_MASTER_KEY)
    monkeypatch.setenv("DATABASE_URL", _VALID_DATABASE_URL)

    settings = load_settings()

    assert settings.TGOS3_MASTER_KEY == _VALID_MASTER_KEY
    assert settings.DATABASE_URL == _VALID_DATABASE_URL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
