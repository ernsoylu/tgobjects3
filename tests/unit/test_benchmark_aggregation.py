"""Unit and property tests for the benchmark aggregation layer.

Covers the pure, Telethon-free aggregation functions in :mod:`cli.benchmark`
required by Requirement 21:

* :func:`cli.benchmark.average_iterations` -- averaging latency, API-call count
  and items-per-second over at least :data:`cli.benchmark.MIN_ITERATIONS`
  (i.e. >= 10) consecutive iterations (Requirement 21.2).
* :func:`cli.benchmark.detect_flood_onset` -- identifying the lowest upload
  concurrency level at which Telegram returned a rate-limit response, i.e. the
  flood-onset point (Requirement 21.1).

These tests deliberately import only the aggregation layer, which must stay
importable without Telethon installed or any network access.
"""

from __future__ import annotations

import pytest
from cli.benchmark import (
    MIN_ITERATIONS,
    AggregatedMetric,
    FloodOnset,
    FloodProbe,
    IterationSample,
    average_iterations,
    detect_flood_onset,
    mean,
)
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# mean
# ---------------------------------------------------------------------------


def test_mean_empty_sequence_is_zero() -> None:
    assert mean([]) == 0.0


def test_mean_of_known_values() -> None:
    assert mean([2.0, 4.0, 6.0]) == 4.0


# ---------------------------------------------------------------------------
# average_iterations -- averaging over >= MIN_ITERATIONS iterations (Req 21.2)
# ---------------------------------------------------------------------------


def _uniform_samples(
    count: int,
    *,
    latency_ms: float = 100.0,
    api_calls: int = 2,
    items: int = 50,
) -> list[IterationSample]:
    """Build ``count`` identical iteration samples."""
    return [
        IterationSample(latency_ms=latency_ms, api_calls=api_calls, items=items)
        for _ in range(count)
    ]


def test_min_iterations_is_at_least_ten() -> None:
    """Requirement 21.2 mandates averaging over >= 10 consecutive iterations."""
    assert MIN_ITERATIONS >= 10


def test_average_iterations_rejects_fewer_than_minimum() -> None:
    samples = _uniform_samples(MIN_ITERATIONS - 1)
    with pytest.raises(ValueError, match=f"at least {MIN_ITERATIONS} iterations"):
        average_iterations(samples)


def test_average_iterations_accepts_exactly_minimum() -> None:
    result = average_iterations(_uniform_samples(MIN_ITERATIONS))
    assert result.iterations == MIN_ITERATIONS


def test_average_iterations_averages_uniform_samples() -> None:
    # Ten identical iterations: averages equal the per-sample values, and the
    # items/second rate derives from items / (latency_ms / 1000).
    result = average_iterations(
        _uniform_samples(10, latency_ms=200.0, api_calls=3, items=100)
    )
    assert result == AggregatedMetric(
        iterations=10,
        avg_latency_ms=200.0,
        avg_api_calls=3.0,
        avg_items_per_second=500.0,  # 100 items / 0.2 s
    )


def test_average_iterations_averages_mixed_latency_and_api_calls() -> None:
    # Alternate between two distinct latencies / api-call counts over 10 runs.
    samples = []
    for i in range(10):
        if i % 2 == 0:
            samples.append(IterationSample(latency_ms=100.0, api_calls=1, items=10))
        else:
            samples.append(IterationSample(latency_ms=300.0, api_calls=3, items=30))
    result = average_iterations(samples)
    assert result.iterations == 10
    assert result.avg_latency_ms == pytest.approx(200.0)  # mean(100, 300)
    assert result.avg_api_calls == pytest.approx(2.0)  # mean(1, 3)
    # per-second rates: 10/0.1 = 100 and 30/0.3 = 100, so mean is 100.
    assert result.avg_items_per_second == pytest.approx(100.0)


def test_average_iterations_ignores_zero_latency_for_rate() -> None:
    # A zero-latency sample must not divide by zero; it is skipped from the
    # items-per-second average while still counting toward latency/api means.
    samples = [IterationSample(latency_ms=0.0, api_calls=0, items=5)]
    samples += _uniform_samples(9, latency_ms=1000.0, api_calls=1, items=10)
    result = average_iterations(samples)
    assert result.iterations == 10
    # Only the nine non-zero-latency samples contribute: 10 items / 1 s = 10.
    assert result.avg_items_per_second == pytest.approx(10.0)


def test_average_iterations_all_zero_latency_yields_zero_rate() -> None:
    samples = _uniform_samples(10, latency_ms=0.0, api_calls=1, items=5)
    result = average_iterations(samples)
    assert result.avg_items_per_second == 0.0


@given(
    count=st.integers(min_value=MIN_ITERATIONS, max_value=200),
    latency_ms=st.floats(min_value=1.0, max_value=10_000.0),
    api_calls=st.integers(min_value=0, max_value=50),
    items=st.integers(min_value=0, max_value=10_000),
)
def test_average_iterations_uniform_property(
    count: int, latency_ms: float, api_calls: int, items: int
) -> None:
    """Averaging identical samples returns those same per-sample values.

    Validates: Requirements 21.2
    """
    result = average_iterations(
        _uniform_samples(
            count, latency_ms=latency_ms, api_calls=api_calls, items=items
        )
    )
    assert result.iterations == count
    assert result.avg_latency_ms == pytest.approx(latency_ms)
    assert result.avg_api_calls == pytest.approx(float(api_calls))
    assert result.avg_items_per_second == pytest.approx(
        items / (latency_ms / 1000.0)
    )


@given(
    samples=st.lists(
        st.builds(
            IterationSample,
            latency_ms=st.floats(min_value=1.0, max_value=10_000.0),
            api_calls=st.integers(min_value=0, max_value=50),
            items=st.integers(min_value=0, max_value=10_000),
        ),
        min_size=MIN_ITERATIONS,
        max_size=100,
    )
)
def test_average_latency_within_sample_bounds_property(
    samples: list[IterationSample],
) -> None:
    """The average latency never falls outside the min/max observed latency.

    Validates: Requirements 21.2
    """
    result = average_iterations(samples)
    latencies = [s.latency_ms for s in samples]
    assert min(latencies) <= result.avg_latency_ms <= max(latencies)


# ---------------------------------------------------------------------------
# detect_flood_onset -- lowest flooded concurrency level (Req 21.1)
# ---------------------------------------------------------------------------


def test_detect_flood_onset_no_flood() -> None:
    probes = [FloodProbe(concurrency=c, flooded=False) for c in (1, 2, 4, 8)]
    result = detect_flood_onset(probes)
    assert result == FloodOnset(concurrency=None, probed_levels=(1, 2, 4, 8))


def test_detect_flood_onset_first_flood_level() -> None:
    probes = [
        FloodProbe(concurrency=1, flooded=False),
        FloodProbe(concurrency=2, flooded=False),
        FloodProbe(concurrency=4, flooded=True),
    ]
    result = detect_flood_onset(probes)
    assert result.concurrency == 4
    assert result.probed_levels == (1, 2, 4)


def test_detect_flood_onset_picks_lowest_flooded_regardless_of_order() -> None:
    # Even if multiple levels flooded and probes arrive out of order, the onset
    # is the smallest flooded concurrency.
    probes = [
        FloodProbe(concurrency=16, flooded=True),
        FloodProbe(concurrency=4, flooded=True),
        FloodProbe(concurrency=2, flooded=False),
        FloodProbe(concurrency=8, flooded=True),
    ]
    result = detect_flood_onset(probes)
    assert result.concurrency == 4
    assert result.probed_levels == (16, 4, 2, 8)


def test_detect_flood_onset_empty_probes() -> None:
    result = detect_flood_onset([])
    assert result == FloodOnset(concurrency=None, probed_levels=())


def test_detect_flood_onset_single_flooded_probe() -> None:
    result = detect_flood_onset([FloodProbe(concurrency=1, flooded=True)])
    assert result.concurrency == 1
    assert result.probed_levels == (1,)


@given(
    probes=st.lists(
        st.builds(
            FloodProbe,
            concurrency=st.integers(min_value=1, max_value=1024),
            flooded=st.booleans(),
        ),
        min_size=1,
        max_size=20,
    )
)
def test_detect_flood_onset_is_min_flooded_property(
    probes: list[FloodProbe],
) -> None:
    """Onset equals the minimum flooded concurrency, else None.

    The result is independent of probe ordering, and the probed-levels tuple
    preserves the input order exactly.

    Validates: Requirements 21.1
    """
    result = detect_flood_onset(probes)
    flooded = [p.concurrency for p in probes if p.flooded]
    expected = min(flooded) if flooded else None
    assert result.concurrency == expected
    assert result.probed_levels == tuple(p.concurrency for p in probes)
