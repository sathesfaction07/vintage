"""Locating status changes in time, and admitting how wide the window is.

Two attribution strategies are implemented so the difference is measurable rather than argued:

  * `naive_changes` sorts by request time and records each change at the instant the request was
    made. This is what a `last_checked` column in a database gives you, and it is what the brief's
    "show when status changed" asks for.

  * `changes` sorts by the ORIGIN window each reading describes, collapses cache echoes, and
    reports each change as an interval with the ordering it could establish.

The naive version is not a straw man -- it is the obvious implementation, it is what most status
history tables contain, and on fresh data it is exactly right. It fails in one direction and one
of its failures is qualitative.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import timedelta
from itertools import pairwise

from .models import (
    Change,
    ObservationCount,
    Ordering,
    Reading,
    StateSpan,
    Status,
)


def collapse_echoes(readings: Iterable[Reading]) -> list[Reading]:
    """Keep one reading per origin generation.

    Three requests answered from one cache entry are one observation. Keeping all three would let
    a repeated value look like corroboration, and would let a long echo run pad the evidence for a
    status that was read once.

    Where several readings share a generation the one with the TIGHTEST window survives, since a
    narrower bound on the same origin response is strictly better evidence. Readings with no
    generation are all kept: absence of an identifier is not evidence of shared identity.
    """
    best: dict[str, Reading] = {}
    unidentified: list[Reading] = []
    for reading in readings:
        if reading.generation is None:
            unidentified.append(reading)
            continue
        held = best.get(reading.generation)
        if held is None or _window_rank(reading) < _window_rank(held):
            best[reading.generation] = reading
    return sorted([*best.values(), *unidentified], key=_origin_key)


def _window_rank(reading: Reading) -> tuple[int, float]:
    """Sort key preferring a narrower window. Unbounded windows rank worst."""
    width = reading.window.width
    if width is None:
        return (1, 0.0)
    return (0, width.total_seconds())


def _origin_key(reading: Reading) -> tuple[float, float]:
    """Order readings by the origin time they describe, not by when we asked.

    Sorting by request time is the defect this module exists to correct: a stale reading arriving
    after a fresh one describes an earlier origin state, so request order can invert origin order.

    Keyed on the window's end -- the latest instant the reading could describe -- with the start as
    a tiebreak. An unbounded start sorts before a bounded one at the same end, because a reading
    that could be arbitrarily old is the weaker claim about the present.
    """
    window = reading.window
    start = window.start.timestamp() if window.start is not None else float("-inf")
    return (window.end.timestamp(), start)


def classify_ordering(earlier: Reading, later: Reading) -> Ordering:
    """How the two readings' origin windows relate.

    `earlier` and `later` are named for their REQUEST order, which is the only order available
    before this function runs. The return value says what the origin order actually is.
    """
    first, second = earlier.window, later.window
    if not first.is_bounded or not second.is_bounded:
        return Ordering.UNBOUNDED
    if first.precedes(second):
        return Ordering.PROVEN
    if second.precedes(first):
        return Ordering.REVERSED
    return Ordering.OVERLAPPING


def inversions(readings: Sequence[Reading]) -> list[tuple[Reading, Reading]]:
    """Consecutive request pairs, WITHIN ONE LISTING, whose origin order reverses their request order.

    Grouped by listing for the same reason attribution is: two readings of different listings have no
    ordering relationship to violate, and comparing them across a shared timestamp manufactures
    inversions out of nothing. An earlier version of this function sorted the whole stream globally
    and reported 39 on the shipped corpus, most of them between unrelated listings polled in the same
    second -- including one whose every reading is fresh and therefore cannot be inverted with
    anything. The number looked plausible; only the detail lines gave it away.

    Reported as a first-class finding rather than silently corrected by the sort, because an inversion
    means a cache in the path is serving content older than the poll interval -- an infrastructure
    fact the operator can act on, and one no downstream arithmetic fixes.
    """
    found: list[tuple[Reading, Reading]] = []
    for group in group_readings(readings).values():
        by_request = sorted(group, key=lambda item: item.observed_at)
        for earlier, later in pairwise(by_request):
            if classify_ordering(earlier, later) is Ordering.REVERSED:
                found.append((earlier, later))
    return found


def observation_count(readings: Sequence[Reading]) -> ObservationCount:
    """How many independent looks at the origin these requests actually represent."""
    identified = [item for item in readings if item.generation is not None]
    return ObservationCount(
        requests=len(readings),
        distinct_generations=len({item.generation for item in identified}),
        undated_readings=len(readings) - len(identified),
    )


def changes(readings: Sequence[Reading], *, skip_unknown: bool = True) -> list[Change]:
    """Status changes as intervals, ordered by origin time.

    `skip_unknown` drops readings whose label did not map, because a transition into UNKNOWN is a
    statement about our config rather than about the listing. Including them would manufacture two
    spurious changes -- one in, one out -- around every wording change the platform made, and those
    would be indistinguishable in the output from real status movements.

    The unknown readings are still counted and reported elsewhere; they are excluded from
    attribution, not from the record.
    """
    usable = [item for item in readings if not (skip_unknown and item.status is Status.UNKNOWN)]
    ordered = collapse_echoes(usable)
    if len(ordered) < 2:
        return []

    found: list[Change] = []
    for previous, current in pairwise(ordered):
        if previous.status == current.status:
            continue
        found.append(
            Change(
                marketplace=current.marketplace,
                listing_id=current.listing_id,
                from_status=previous.status,
                to_status=current.status,
                after=previous.window.start,
                at_or_before=current.window.end,
                ordering=classify_ordering(previous, current),
                confirmed_at=current.observed_at,
            )
        )
    return found


def naive_changes(readings: Sequence[Reading], *, skip_unknown: bool = True) -> list[Change]:
    """Status changes as instants, ordered by request time.

    Kept in the codebase, and measured, because it is the implementation everyone writes first. The
    interval collapses to a point at `confirmed_at`, echoes are not collapsed, and the ordering is
    whatever the request timestamps imply.
    """
    usable = [item for item in readings if not (skip_unknown and item.status is Status.UNKNOWN)]
    ordered = sorted(usable, key=lambda item: item.observed_at)
    found: list[Change] = []
    for previous, current in pairwise(ordered):
        if previous.status == current.status:
            continue
        found.append(
            Change(
                marketplace=current.marketplace,
                listing_id=current.listing_id,
                from_status=previous.status,
                to_status=current.status,
                after=current.observed_at,
                at_or_before=current.observed_at,
                ordering=Ordering.PROVEN,
                confirmed_at=current.observed_at,
            )
        )
    return found


def spans(readings: Sequence[Reading], *, skip_unknown: bool = True) -> list[StateSpan]:
    """Periods spent in each status, with interval-valued durations.

    The first span has no entry change, so its duration is unbounded below in principle -- we did
    not observe the listing before we started looking. It is reported open-ended rather than
    measured from the first reading, which would silently date the state to when monitoring began.
    """
    usable = [item for item in readings if not (skip_unknown and item.status is Status.UNKNOWN)]
    ordered = collapse_echoes(usable)
    if not ordered:
        return []

    transitions = changes(readings, skip_unknown=skip_unknown)
    built: list[StateSpan] = []
    current_status = ordered[0].status
    entered: Change | None = None

    for change in transitions:
        built.append(
            StateSpan(
                marketplace=ordered[0].marketplace,
                listing_id=ordered[0].listing_id,
                status=current_status,
                entered=entered,
                exited=change,
            )
        )
        current_status = change.to_status
        entered = change

    built.append(
        StateSpan(
            marketplace=ordered[0].marketplace,
            listing_id=ordered[0].listing_id,
            status=current_status,
            entered=entered,
            exited=None,
        )
    )
    return built


def group_readings(readings: Iterable[Reading]) -> dict[tuple[str, str], list[Reading]]:
    """Split readings by (marketplace, listing).

    Attribution across marketplaces would be meaningless -- a change in one region's listing has
    nothing to do with another's -- and merging them would let one region's stale cache invent a
    transition in another.
    """
    grouped: dict[tuple[str, str], list[Reading]] = {}
    for reading in readings:
        grouped.setdefault((reading.marketplace, reading.listing_id), []).append(reading)
    return grouped


def widest_interval(found: Sequence[Change]) -> timedelta | None:
    """The widest bounded change interval, or None when every change is unbounded."""
    widths = [change.width for change in found if change.width is not None]
    return max(widths) if widths else None
