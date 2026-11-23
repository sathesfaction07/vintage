"""Loading readings and truth from JSON Lines.

The corpus stores RAW response metadata -- the `Age` header, the `Date`, the `Cache-Control` string
-- and the age arithmetic runs at load time. Storing a pre-computed age bound instead would bake the
answer into the fixture, and the property tests would then be checking that a number I wrote down
equals itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from .freshness import age_from_headers, generation_of
from .labels import LabelMap
from .measure import TruthChange
from .models import Reading, Status


def _instant(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def _rows(path: Path) -> Iterator[tuple[int, dict]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            yield number, json.loads(text)


def load_readings(path: Path, labels: LabelMap) -> tuple[list[Reading], list[str]]:
    """Build readings from raw response metadata, resolving labels through the mapping.

    Problems are collected rather than raised. A row that cannot be read is a row we know nothing
    about, which is different from a row whose label we cannot map -- the second still yields a
    reading, carrying `Status.UNKNOWN` so the drift is visible downstream.
    """
    readings: list[Reading] = []
    problems: list[str] = []

    for number, row in _rows(path):
        try:
            observed_at = datetime.fromisoformat(row["observed_at"])
            marketplace = row["marketplace"]
            listing_id = row["listing_id"]
            raw_label = row["raw_label"]
        except (KeyError, ValueError) as error:
            problems.append(f"{path.name}:{number}: {error}")
            continue

        date_header = _instant(row.get("date_header"))
        last_modified = _instant(row.get("last_modified"))
        provenance, age = age_from_headers(
            observed_at=observed_at,
            age_header=row.get("age_header"),
            date_header=date_header,
            cache_control=row.get("cache_control"),
            request_sent_at=_instant(row.get("request_sent_at")),
        )
        readings.append(
            Reading(
                marketplace=marketplace,
                listing_id=listing_id,
                raw_label=raw_label,
                status=labels.resolve(marketplace, raw_label),
                observed_at=observed_at,
                provenance=provenance,
                age=age,
                generation=generation_of(
                    etag=row.get("etag"), date_header=date_header, last_modified=last_modified
                ),
            )
        )
    return readings, problems


def load_truth(path: Path) -> tuple[list[TruthChange], list[str]]:
    truth: list[TruthChange] = []
    problems: list[str] = []
    for number, row in _rows(path):
        try:
            truth.append(
                TruthChange(
                    marketplace=row["marketplace"],
                    listing_id=row["listing_id"],
                    at=datetime.fromisoformat(row["at"]),
                    from_status=Status(row["from_status"]),
                    to_status=Status(row["to_status"]),
                )
            )
        except (KeyError, ValueError) as error:
            problems.append(f"{path.name}:{number}: {error}")
    return truth, problems


def unknown_labels(readings: list[Reading]) -> dict[tuple[str, str], int]:
    """Raw labels that did not map, counted per marketplace.

    The output the operator acts on: each entry is a line to add to the mapping. Counts are included
    because a label seen once may be a rendering quirk while a label seen on every poll is the
    platform's new wording.
    """
    counts: dict[tuple[str, str], int] = {}
    for reading in readings:
        if reading.status is Status.UNKNOWN:
            key = (reading.marketplace, reading.raw_label)
            counts[key] = counts.get(key, 0) + 1
    return counts
