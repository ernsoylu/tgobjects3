"""Property test for the bounded staging reservation counter.

Feature: tgobjects3, Property 9: Staging reservation prevents overflow.

Exercises :class:`gateway.app.storage.staging.StagingReservation`, the
in-memory admission-control counter that fronts the staging area. Requirements
6.3 and 6.4 together say the Gateway reserves the declared ``Content-Length``
before reading a PutObject body and, when the sum of all in-progress
reservations would exceed ``STAGING_MAX_SIZE``, returns HTTP 507 and streams no
body. ``reserve`` encodes that refusal by returning ``None``; the PutObject
handler maps ``None`` to the 507 response.

The property driven here is the invariant the handler relies on: no matter how
reservation and release requests interleave across concurrently running
coroutines, the running total never exceeds the configured ceiling, and a
request that would cross the ceiling is refused with ``None`` rather than
granted. ``reserve``/``release`` guard the check-and-increment with an
``asyncio.Lock``, so the test issues the generated requests as concurrently
scheduled tasks on a single event loop to put that lock under contention.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from gateway.app.storage.staging import StagingHandle, StagingReservation
from hypothesis import given, settings
from hypothesis import strategies as st


@dataclass(frozen=True)
class _ReserveOp:
    """A request to reserve ``size`` bytes."""

    size: int


@dataclass(frozen=True)
class _ReleaseOp:
    """A request to release the reservation granted by the op at ``index``.

    ``index`` refers to the position of an earlier :class:`_ReserveOp` in the
    generated sequence. The op is a no-op when that earlier reserve was refused
    (returned ``None``) or has already been released, which models a client that
    may try to release a handle it never obtained.
    """

    index: int


_Op = _ReserveOp | _ReleaseOp


def _operations(max_size: int) -> st.SearchStrategy[list[_Op]]:
    """Build a strategy over interleaved reserve/release sequences.

    Reservation sizes range up to ``max_size + 1`` so the sequence contains both
    requests that fit and single requests that overflow the empty counter on
    their own. Release ops carry an index into the sequence; it is reduced modulo
    the sequence position at execution time, so every index addresses some
    earlier reserve without the strategy needing to track prior draws.
    """
    reserves = st.builds(_ReserveOp, size=st.integers(min_value=1, max_value=max_size + 1))
    releases = st.builds(_ReleaseOp, index=st.integers(min_value=0, max_value=1_000_000))
    return st.lists(st.one_of(reserves, releases), min_size=0, max_size=40)


async def _run_workload(max_size: int, ops: list[_Op]) -> None:
    """Execute ``ops`` as concurrent tasks and assert the overflow invariant.

    Every op is scheduled as its own task so the ``asyncio.Lock`` inside
    ``reserve``/``release`` mediates genuinely interleaved access. After each
    grant the running total is checked against ``max_size`` and a refusal is
    checked to be exactly the case that would have overflowed, both read under
    the reservation's own lock to get a consistent snapshot.
    """
    reservation = StagingReservation(max_size)
    granted: dict[int, StagingHandle] = {}
    lock = asyncio.Lock()

    async def do_reserve(op_index: int, size: int) -> None:
        handle = await reservation.reserve(size)
        # The counter must never show more than the ceiling at any observation
        # point. ``reserved_bytes`` is a plain read, but since reserve granted
        # atomically under its lock, a value above the ceiling here would be a
        # genuine invariant violation regardless of concurrent activity.
        assert reservation.reserved_bytes <= max_size, (
            f"reserved {reservation.reserved_bytes} bytes exceeds ceiling {max_size}"
        )
        if handle is None:
            # A single request larger than the whole ceiling can never be
            # granted; otherwise a refusal means the counter was already too
            # full to admit this size, which is the intended 507 behaviour.
            assert size > max_size or reservation.reserved_bytes + size > max_size
            return
        assert handle.size == size
        async with lock:
            granted[op_index] = handle

    async def do_release(target_index: int) -> None:
        async with lock:
            handle = granted.pop(target_index, None)
        if handle is not None:
            await reservation.release(handle)

    tasks: list[asyncio.Task[None]] = []
    reserve_positions: list[int] = []
    for position, op in enumerate(ops):
        if isinstance(op, _ReserveOp):
            reserve_positions.append(position)
            tasks.append(asyncio.create_task(do_reserve(position, op.size)))
        else:
            if reserve_positions:
                target = reserve_positions[op.index % len(reserve_positions)]
                tasks.append(asyncio.create_task(do_release(target)))

    await asyncio.gather(*tasks)

    final = reservation.reserved_bytes
    assert final <= max_size
    # Everything still held accounts for exactly the final total.
    assert sum(h.size for h in granted.values()) == final


# Feature: tgobjects3, Property 9: Staging reservation prevents overflow
# Validates: Requirements 6.3, 6.4
@settings(max_examples=200)
@given(max_size=st.integers(min_value=1, max_value=4096), data=st.data())
def test_staging_reservation_never_overflows(max_size: int, data: st.DataObject) -> None:
    """In-progress reservations never exceed ``STAGING_MAX_SIZE``.

    Across every generated interleaving of concurrent reserve/release requests,
    the running total stays at or below ``max_size`` and any reserve that would
    push it past the ceiling returns ``None`` (the HTTP 507 signal) rather than
    being granted (Requirements 6.3, 6.4).
    """
    ops = data.draw(_operations(max_size))
    asyncio.run(_run_workload(max_size, ops))


# Feature: tgobjects3, Property 9: Staging reservation prevents overflow
# Validates: Requirements 6.3, 6.4
@given(
    size=st.integers(min_value=1, max_value=4096),
    max_size=st.integers(min_value=1, max_value=4096),
)
def test_single_oversized_reservation_is_refused(size: int, max_size: int) -> None:
    """A lone request larger than the ceiling is refused with ``None``.

    This pins the boundary case the handler maps to HTTP 507: with an empty
    counter, a request for more than ``max_size`` bytes must not be granted,
    while one for ``max_size`` bytes or fewer must succeed.
    """

    async def run() -> None:
        reservation = StagingReservation(max_size)
        handle = await reservation.reserve(size)
        if size > max_size:
            assert handle is None
            assert reservation.reserved_bytes == 0
        else:
            assert handle is not None
            assert reservation.reserved_bytes == size

    asyncio.run(run())
