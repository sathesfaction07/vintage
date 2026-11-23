"""Shared fixtures.

The corpus is loaded once per session. Tests that need a hand-built scenario use `reading()`, which
takes an offset in hours and an age bound, because every interesting case in this project is about
the relationship between when we asked and how old the answer was.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from status_checker.attribute import group_readings
from status_checker.labels import LabelMap
from status_checker.models import AgeBound, Provenance, Reading, Status
from status_checker.sources import load_readings, load_truth

DATA = Path(__file__).resolve().parents[1] / "data"
BASE = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)


def at(hours: float = 0.0) -> datetime:
    return BASE + timedelta(hours=hours)


def reading(
    *,
    hours: float = 0.0,
    status: Status = Status.ACTIVE,
    age_min: float = 0.0,
    age_max: float | None = 0.0,
    generation: str | None = None,
    marketplace: str = "de",
    listing_id: str = "L1",
    raw_label: str | None = None,
) -> Reading:
    """One reading. `age_max=None` means the age is unbounded above."""
    provenance = Provenance.ORIGIN
    if age_max is None:
        provenance = Provenance.UNDATED
    elif age_min > 0:
        provenance = Provenance.CACHED
    return Reading(
        marketplace=marketplace,
        listing_id=listing_id,
        raw_label=raw_label if raw_label is not None else status.value,
        status=status,
        observed_at=at(hours),
        provenance=provenance,
        age=AgeBound(min_seconds=age_min, max_seconds=age_max),
        generation=generation,
    )


@pytest.fixture(scope="session")
def labels() -> LabelMap:
    mapping, problems = LabelMap.from_file(DATA / "labels.json")
    assert not problems, problems
    return mapping


@pytest.fixture(scope="session")
def corpus(labels: LabelMap) -> list[Reading]:
    readings, problems = load_readings(DATA / "readings.jsonl", labels)
    # Exactly one row is deliberately malformed, so this asserts the count rather than emptiness.
    assert len(problems) == 1, problems
    return readings


@pytest.fixture(scope="session")
def truth() -> list:
    entries, problems = load_truth(DATA / "truth.jsonl")
    assert not problems, problems
    return entries


@pytest.fixture(scope="session")
def grouped(corpus: list[Reading]) -> dict[tuple[str, str], list[Reading]]:
    return group_readings(corpus)
