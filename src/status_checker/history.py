"""SQLite history, where the schema is the argument.

The brief asks for "historical status tracking (SQLite -- show when status changed)". The obvious
schema is

    CREATE TABLE status_history (listing_id TEXT, status TEXT, changed_at TIMESTAMP)

and it cannot represent what we know. `changed_at` is one column, so it holds one instant, so the
only thing that fits is the poll timestamp -- and the schema has then forced the naive answer before
any code is written. Every consumer inherits it: a dashboard, a report, an SLA calculation, all
reading a column whose name asserts more than the data supports.

So `changes` here has `after` and `at_or_before` instead, `after` is nullable because a lower bound
is not always available, and `confirmed_at` is kept separately so the poll timestamp is still there
for anyone who wants it -- clearly labelled as when we LOOKED rather than when it happened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .models import Change, Ordering, Provenance, Reading, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    marketplace   TEXT    NOT NULL,
    listing_id    TEXT    NOT NULL,
    raw_label     TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    observed_at   TEXT    NOT NULL,
    provenance    TEXT    NOT NULL,
    age_min       REAL    NOT NULL,
    age_max       REAL,             -- NULL means unbounded, not zero
    generation    TEXT,
    PRIMARY KEY (marketplace, listing_id, observed_at)
);

CREATE TABLE IF NOT EXISTS changes (
    marketplace   TEXT    NOT NULL,
    listing_id    TEXT    NOT NULL,
    from_status   TEXT    NOT NULL,
    to_status     TEXT    NOT NULL,
    after         TEXT,             -- NULL means no lower bound was established
    at_or_before  TEXT    NOT NULL,
    ordering      TEXT    NOT NULL,
    confirmed_at  TEXT    NOT NULL, -- when we LOOKED, not when it changed
    PRIMARY KEY (marketplace, listing_id, at_or_before, to_status)
);

CREATE INDEX IF NOT EXISTS readings_by_listing ON readings (marketplace, listing_id, observed_at);
"""


class History:
    """Append-only store for readings and attributed changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialise(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)

    def record_readings(self, readings: Sequence[Reading]) -> int:
        """Insert readings, ignoring exact duplicates.

        `INSERT OR IGNORE` on the natural key rather than `OR REPLACE`: re-running a poll should not
        silently rewrite what a previous run observed, because the whole point of the store is to be
        a record of what was seen and when.
        """
        rows = [
            (
                item.marketplace,
                item.listing_id,
                item.raw_label,
                item.status.value,
                item.observed_at.isoformat(),
                item.provenance.value,
                item.age.min_seconds,
                item.age.max_seconds,
                item.generation,
            )
            for item in readings
        ]
        with closing(self._connect()) as connection, connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO readings "
                "(marketplace, listing_id, raw_label, status, observed_at, provenance, "
                " age_min, age_max, generation) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            return connection.total_changes - before

    def record_changes(self, changes: Sequence[Change]) -> int:
        rows = [
            (
                item.marketplace,
                item.listing_id,
                item.from_status.value,
                item.to_status.value,
                item.after.isoformat() if item.after else None,
                item.at_or_before.isoformat(),
                item.ordering.value,
                item.confirmed_at.isoformat(),
            )
            for item in changes
        ]
        with closing(self._connect()) as connection, connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO changes "
                "(marketplace, listing_id, from_status, to_status, after, at_or_before, "
                " ordering, confirmed_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            return connection.total_changes - before

    def readings_for(self, marketplace: str, listing_id: str) -> list[Reading]:
        from .models import AgeBound

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "SELECT raw_label, status, observed_at, provenance, age_min, age_max, generation "
                "FROM readings WHERE marketplace = ? AND listing_id = ? ORDER BY observed_at",
                (marketplace, listing_id),
            )
            return [
                Reading(
                    marketplace=marketplace,
                    listing_id=listing_id,
                    raw_label=row[0],
                    status=Status(row[1]),
                    observed_at=datetime.fromisoformat(row[2]),
                    provenance=Provenance(row[3]),
                    age=AgeBound(min_seconds=row[4], max_seconds=row[5]),
                    generation=row[6],
                )
                for row in cursor.fetchall()
            ]

    def changes_for(self, marketplace: str, listing_id: str) -> list[Change]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "SELECT from_status, to_status, after, at_or_before, ordering, confirmed_at "
                "FROM changes WHERE marketplace = ? AND listing_id = ? ORDER BY at_or_before",
                (marketplace, listing_id),
            )
            return [
                Change(
                    marketplace=marketplace,
                    listing_id=listing_id,
                    from_status=Status(row[0]),
                    to_status=Status(row[1]),
                    after=datetime.fromisoformat(row[2]) if row[2] else None,
                    at_or_before=datetime.fromisoformat(row[3]),
                    ordering=Ordering(row[4]),
                    confirmed_at=datetime.fromisoformat(row[5]),
                )
                for row in cursor.fetchall()
            ]

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            return {
                "readings": connection.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
                "changes": connection.execute("SELECT COUNT(*) FROM changes").fetchone()[0],
                "unbounded_changes": connection.execute(
                    "SELECT COUNT(*) FROM changes WHERE after IS NULL"
                ).fetchone()[0],
                "undated_readings": connection.execute(
                    "SELECT COUNT(*) FROM readings WHERE age_max IS NULL"
                ).fetchone()[0],
            }

    def listings(self) -> list[tuple[str, str]]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "SELECT DISTINCT marketplace, listing_id FROM readings ORDER BY 1, 2"
            )
            return [(row[0], row[1]) for row in cursor.fetchall()]
