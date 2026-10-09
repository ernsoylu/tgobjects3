"""Property test for the record-cache ranking score and eviction priority.

Feature: tgobjects3, Property 13: Cache ranking score decays over time and
scales with access frequency.

Exercises :func:`gateway.app.cache.eviction.decay_score` and
:func:`gateway.app.cache.eviction.priority`, the two pure functions that encode
the design's cache ranking policy (Requirement 12.2). The score recurrence is::

    s(t)        = s_prev * 2^(-Delta_t / H) + 1
    priority(t) = s(t) / size^alpha

where ``Delta_t`` is the elapsed seconds since the stored score was captured,
``H`` is the half-life (``CACHE_HALF_LIFE``), and ``alpha`` is the size bias
(``CACHE_SIZE_BIAS``). Records with lower priority are ranked lower and evicted
first, so a hotter (higher cumulative score) record of equal size must out-rank
a colder one.

Two universal properties are driven here:

* **Decay**: with ``Delta_t > 0`` and ``H > 0`` the decay factor is strictly
  below one, so the decayed-and-incremented score ``s(t)`` is strictly less than
  the hypothetical fresh score ``s_prev + 1`` the same record would have earned
  with no elapsed time. The score strictly decreases as elapsed time grows.
* **Frequency ordering**: for two records of equal size scored over equal
  elapsed time, the one carrying the larger cumulative score ``s_prev`` ranks
  strictly higher in priority.

Domains are kept small and bounded so each example is O(1) and never trips the
Hypothesis deadline.

Floating-point note
-------------------
The strict comparisons the properties assert hold mathematically for every
positive input, but IEEE-754 doubles lose them at the far tail of the decay
curve: once ``Delta_t / H`` grows past roughly the mantissa width, the factor
``2^(-Delta_t / H)`` shrinks below the ULP of the ``+ 1`` increment and
``s_prev * factor + 1.0`` rounds to exactly ``1.0`` for any ``s_prev`` in range,
collapsing the strict order. That is a representational limit of the formula,
not a defect in the policy. The generators below therefore bound the elapsed
time to a few half-lives (``Delta_t / H`` within :data:`_MAX_HALF_LIVES`), which
is the regime the eviction loop actually ranks over -- records decayed far past
that are already effectively cold and tie near the ``+ 1`` floor by design.
"""

from __future__ import annotations

import math

from gateway.app.cache.eviction import decay_score, priority
from hypothesis import given, settings
from hypothesis import strategies as st

# Keep the decay exponent well inside the regime where ``s_prev * 2^(-ratio)``
# stays representable above the ULP of the ``+ 1`` increment, so the strict
# score ordering survives in double precision. Ten half-lives decays a score to
# ~0.1% of its value -- comfortably hot-vs-cold without underflowing the order.
_MAX_HALF_LIVES = 10.0

# Bounded, realistic domains. ``allow_subnormal=False`` keeps the generated
# floats away from denormal values whose arithmetic would muddy the strict
# comparisons without exercising anything the policy cares about.
_SCORES = st.floats(
    min_value=1.0e-3,
    max_value=1.0e6,
    allow_nan=False,
    allow_infinity=False,
    allow_subnormal=False,
)
_HALF_LIVES = st.floats(
    min_value=1.0,
    max_value=1.0e7,
    allow_nan=False,
    allow_infinity=False,
    allow_subnormal=False,
)
# Elapsed time is drawn as a positive multiple of the half-life so the decay
# exponent stays bounded regardless of the half-life magnitude.
_HALF_LIFE_MULTIPLES = st.floats(
    min_value=1.0e-3,
    max_value=_MAX_HALF_LIVES,
    allow_nan=False,
    allow_infinity=False,
    allow_subnormal=False,
)
_SIZES = st.integers(min_value=1, max_value=16 * 1024 * 1024)
_ALPHAS = st.floats(
    min_value=0.0,
    max_value=4.0,
    allow_nan=False,
    allow_infinity=False,
    allow_subnormal=False,
)


# Feature: tgobjects3, Property 13: Cache ranking score decays over time and
# scales with access frequency
# Validates: Requirements 12.2
@settings(max_examples=300)
@given(s_prev=_SCORES, multiple=_HALF_LIFE_MULTIPLES, half_life=_HALF_LIVES)
def test_score_strictly_decays_with_elapsed_time(
    s_prev: float, multiple: float, half_life: float
) -> None:
    """A positive elapsed time yields a score below the no-decay baseline.

    With ``Delta_t > 0`` and ``H > 0`` the factor ``2^(-Delta_t / H)`` is
    strictly below one, so ``s(t) = s_prev * factor + 1`` is strictly less than
    the fresh score ``s_prev + 1`` the record would hold with no elapsed time
    (Requirement 12.2). ``s_prev`` is always positive here, so there is a prior
    score to erode and the inequality is strict.
    """
    delta_t = half_life * multiple  # bounded exponent: multiple <= _MAX_HALF_LIVES
    decayed = decay_score(s_prev, delta_t, half_life)
    baseline = decay_score(s_prev, 0.0, half_life)  # == s_prev + 1

    assert decayed < baseline
    # The increment keeps the score positive regardless of how far it decays.
    assert decayed > 0.0


# Feature: tgobjects3, Property 13: Cache ranking score decays over time and
# scales with access frequency
# Validates: Requirements 12.2
@settings(max_examples=300)
@given(
    s_prev=_SCORES,
    half_life=_HALF_LIVES,
    early_mult=_HALF_LIFE_MULTIPLES,
    extra_mult=_HALF_LIFE_MULTIPLES,
)
def test_score_is_monotonically_decreasing_in_elapsed_time(
    s_prev: float, half_life: float, early_mult: float, extra_mult: float
) -> None:
    """Later observations score strictly below earlier ones for a fixed record.

    Decaying the same positive stored score over a longer elapsed time strictly
    lowers the result, so ranking a stale record never inflates its priority
    (Requirement 12.2). Both elapsed times are kept within a bounded number of
    half-lives so the strict order survives double precision.
    """
    # Place both samples inside the bounded regime: scale the two multiples so
    # their sum never exceeds _MAX_HALF_LIVES, keeping the exponent small while
    # preserving a strict gap between the earlier and later elapsed times.
    span = early_mult + extra_mult
    scale = _MAX_HALF_LIVES / span
    early = half_life * early_mult * scale
    later = half_life * span * scale  # == half_life * _MAX_HALF_LIVES
    assert later > early

    score_early = decay_score(s_prev, early, half_life)
    score_later = decay_score(s_prev, later, half_life)

    assert score_later < score_early


# Feature: tgobjects3, Property 13: Cache ranking score decays over time and
# scales with access frequency
# Validates: Requirements 12.2
@settings(max_examples=300)
@given(
    s_low=_SCORES,
    gap=st.floats(
        min_value=1.0,
        max_value=1.0e6,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
    multiple=_HALF_LIFE_MULTIPLES,
    half_life=_HALF_LIVES,
    size=_SIZES,
    alpha=_ALPHAS,
)
def test_higher_cumulative_score_wins_priority_at_equal_size_and_time(
    s_low: float,
    gap: float,
    multiple: float,
    half_life: float,
    size: int,
    alpha: float,
) -> None:
    """A hotter record out-ranks a colder one of equal size and elapsed time.

    For two records sharing ``size`` and ``Delta_t``, priority reduces to
    dividing each decayed score by the same positive ``size^alpha``. The
    recurrence is strictly increasing in ``s_prev`` (the decay factor is
    positive), so the record with the larger cumulative score has the strictly
    larger decayed score and therefore strictly higher priority (Requirement
    12.2). This is exactly the frequency bias the policy relies on to retain hot
    records.
    """
    s_high = s_low + gap  # gap >= 1, so s_high is meaningfully above s_low
    delta_t = half_life * multiple  # bounded exponent: multiple <= _MAX_HALF_LIVES

    score_low = decay_score(s_low, delta_t, half_life)
    score_high = decay_score(s_high, delta_t, half_life)
    assert score_high > score_low

    prio_low = priority(score_low, size, alpha)
    prio_high = priority(score_high, size, alpha)

    assert prio_high > prio_low
    # Priority divides by a strictly positive, finite normaliser, so it stays a
    # well-defined positive number for both records.
    assert math.isfinite(prio_low)
    assert prio_low > 0.0
