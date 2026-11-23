"""Generate a reproducible corpus with known change instants.

The important design choice: staleness is SIMULATED, not planted. Each listing has a true status
timeline, and a poll served from a cache of age A returns the status that was true at `poll_time - A`
-- so a wrong reading arises from the mechanism rather than from me deciding a row should be wrong.
The corpus therefore cannot flatter the interval method by construction; if the arithmetic is wrong,
containment drops.

Run: python data/generate.py
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
START = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
POLL_HOURS = 4

# Localised labels. Each marketplace prints its own wording for the same underlying state, which is
# the whole reason a canonical status exists.
LABELS: dict[str, dict[str, str]] = {
    "us": {"Active": "active", "Restricted": "restricted", "Under Review": "pending",
           "Suppressed": "suppressed"},
    "uk": {"Active": "active", "Restricted": "restricted", "Under Review": "pending",
           "Suppressed": "suppressed"},
    "de": {"Aktiv": "active", "Eingeschränkt": "restricted", "In Prüfung": "pending",
           "Ausgeblendet": "suppressed"},
    "fr": {"Actif": "active", "Restreint": "restricted", "En cours d'examen": "pending",
           "Masqué": "suppressed"},
    "jp": {"有効": "active", "制限あり": "restricted", "審査中": "pending", "非表示": "suppressed"},
    "es": {"Activo": "active", "Restringido": "restricted", "En revisión": "pending",
           "Oculto": "suppressed"},
    # A deliberate CONFLICT: "Under Review" means PENDING in us/uk but this table calls it
    # RESTRICTED. Both readings are plausible in isolation and any count spanning both is undefined.
    "ca": {"Active": "active", "Restricted": "restricted", "Under Review": "restricted",
           "Suppressed": "suppressed"},
    # A table that cannot express SUPPRESSED at all, so its zero for that status is not a finding.
    "au": {"Active": "active", "Restricted": "restricted", "Under Review": "pending"},
}

LABEL_FOR: dict[tuple[str, str], str] = {}
for marketplace, table in LABELS.items():
    for label, status in table.items():
        LABEL_FOR.setdefault((marketplace, status), label)


def status_at(timeline: list[tuple[datetime, str]], instant: datetime) -> str:
    """The true status at an instant. Timeline is (from_instant, status), ascending."""
    current = timeline[0][1]
    for boundary, status in timeline:
        if instant >= boundary:
            current = status
        else:
            break
    return current


def build() -> tuple[list[dict], list[dict]]:
    rng = random.Random(20260301)
    readings: list[dict] = []
    truth: list[dict] = []

    def poll(
        marketplace: str,
        listing: str,
        timeline: list[tuple[datetime, str]],
        *,
        at: datetime,
        actual_age: int,
        lifetime: int | None,
        send_age_header: bool,
        send_etag: bool,
        label_override: str | None = None,
    ) -> None:
        """Record one poll. The value served is the truth at `at - actual_age`."""
        origin_instant = at - timedelta(seconds=actual_age)
        status = status_at(timeline, origin_instant)
        label = label_override or LABEL_FOR.get((marketplace, status))
        if label is None:
            return
        row: dict = {
            "marketplace": marketplace,
            "listing_id": listing,
            "raw_label": label,
            "observed_at": at.isoformat(),
        }
        if send_age_header:
            row["age_header"] = actual_age
        if lifetime is not None:
            row["cache_control"] = f"max-age={lifetime}"
        if send_etag:
            # The ETag names the origin response, so an unchanged origin state served repeatedly
            # carries the same one -- which is what makes echo detection possible.
            row["etag"] = f'W/"{marketplace}-{listing}-{origin_instant.isoformat()}"'
        readings.append(row)

    def record_truth(marketplace: str, listing: str, timeline: list[tuple[datetime, str]]) -> None:
        for index in range(1, len(timeline)):
            truth.append(
                {
                    "marketplace": marketplace,
                    "listing_id": listing,
                    "at": timeline[index][0].isoformat(),
                    "from_status": timeline[index - 1][1],
                    "to_status": timeline[index][1],
                }
            )

    # ---------------------------------------------------------------- clean, always fresh
    # Baseline: no caching anywhere. Both strategies should do well, and the interval's width should
    # be exactly the poll interval -- the floor no arithmetic beats.
    for marketplace in ("us", "de", "jp"):
        listing = "clean"
        timeline = [
            (START, "active"),
            (START + timedelta(hours=17, minutes=20), "restricted"),
            (START + timedelta(hours=53, minutes=5), "active"),
        ]
        record_truth(marketplace, listing, timeline)
        for step in range(24):
            poll(marketplace, listing, timeline, at=START + timedelta(hours=POLL_HOURS * step),
                 actual_age=0, lifetime=0, send_age_header=False, send_etag=True)

    # ---------------------------------------------------------------- consistently stale cache
    # A CDN serving ~2h old content. Naive attribution is late by roughly the staleness on every
    # change, which is a BIAS: it does not cancel across listings the way noise would.
    for marketplace in ("uk", "fr"):
        listing = "stale-cdn"
        timeline = [
            (START, "active"),
            (START + timedelta(hours=25, minutes=40), "pending"),
            (START + timedelta(hours=61, minutes=10), "restricted"),
        ]
        record_truth(marketplace, listing, timeline)
        for step in range(24):
            age = rng.randint(5400, 9000)
            poll(marketplace, listing, timeline, at=START + timedelta(hours=POLL_HOURS * step),
                 actual_age=age, lifetime=10800, send_age_header=True, send_etag=True)

    # ---------------------------------------------------------------- an inversion
    # A fresh read, then a heavily stale read half an hour later. The later REQUEST describes an
    # earlier ORIGIN state, so request order is the reverse of origin order.
    for marketplace in ("es",):
        listing = "inverted"
        change_at = START + timedelta(hours=8)
        timeline = [(START, "active"), (change_at, "restricted")]
        record_truth(marketplace, listing, timeline)
        poll(marketplace, listing, timeline, at=START + timedelta(hours=4),
             actual_age=0, lifetime=0, send_age_header=False, send_etag=True)
        # Fresh, after the change -> reads RESTRICTED.
        poll(marketplace, listing, timeline, at=change_at + timedelta(minutes=10),
             actual_age=0, lifetime=0, send_age_header=False, send_etag=True)
        # Half an hour later, from a 3h cache -> reads the pre-change ACTIVE.
        poll(marketplace, listing, timeline, at=change_at + timedelta(minutes=40),
             actual_age=10800, lifetime=14400, send_age_header=True, send_etag=True)
        for step in range(4, 20):
            poll(marketplace, listing, timeline, at=START + timedelta(hours=POLL_HOURS * step),
                 actual_age=0, lifetime=0, send_age_header=False, send_etag=True)

    # ---------------------------------------------------------------- undated responses
    # No Age, no Cache-Control. Nothing bounds the age, so every interval is unbounded below: the
    # change is detectable and not locatable. This is the common real case, not an edge case.
    for marketplace in ("de", "au"):
        listing = "undated"
        timeline = [
            (START, "active"),
            (START + timedelta(hours=33, minutes=25), "restricted"),
        ]
        record_truth(marketplace, listing, timeline)
        for step in range(24):
            poll(marketplace, listing, timeline, at=START + timedelta(hours=POLL_HOURS * step),
                 actual_age=0, lifetime=None, send_age_header=False, send_etag=False)

    # ---------------------------------------------------------------- echo storm
    # Polled every 20 minutes against a cache with a 6h lifetime. Most requests return the same
    # origin generation, so the request count wildly overstates the evidence.
    for marketplace in ("us",):
        listing = "echoed"
        timeline = [
            (START, "active"),
            (START + timedelta(hours=30), "suppressed"),
        ]
        record_truth(marketplace, listing, timeline)
        for step in range(216):
            at = START + timedelta(minutes=20 * step)
            # Age grows within a 6h window then resets, as a cache entry expires and is refilled.
            age = int((at - START).total_seconds()) % 21600
            poll(marketplace, listing, timeline, at=at, actual_age=age, lifetime=21600,
                 send_age_header=True, send_etag=True)

    # ---------------------------------------------------------------- the platform changes wording
    # Mid-run, the label becomes something the mapping has never seen. The status becomes UNKNOWN,
    # which is a fact about our config -- so it must not manufacture two status transitions.
    for marketplace in ("de",):
        listing = "reworded"
        timeline = [(START, "active"), (START + timedelta(hours=45), "restricted")]
        record_truth(marketplace, listing, timeline)
        for step in range(24):
            at = START + timedelta(hours=POLL_HOURS * step)
            override = "Eingeschränkt (neu)" if step >= 14 else None
            poll(marketplace, listing, timeline, at=at, actual_age=0, lifetime=0,
                 send_age_header=False, send_etag=True, label_override=override)

    # ---------------------------------------------------------------- labels needing normalisation
    # Fullwidth digits and a non-breaking space, both of which look identical to a reader and compare
    # unequal without NFKC and whitespace folding.
    for marketplace in ("jp",):
        listing = "widechars"
        timeline = [(START, "active"), (START + timedelta(hours=21), "restricted")]
        record_truth(marketplace, listing, timeline)
        for step in range(18):
            at = START + timedelta(hours=POLL_HOURS * step)
            status = status_at(timeline, at)
            label = LABEL_FOR[(marketplace, status)]
            poll(marketplace, listing, timeline, at=at, actual_age=0, lifetime=0,
                 send_age_header=False, send_etag=True,
                 label_override=f"\u3000{label}\u00a0")

    # ---------------------------------------------------------------- overlapping windows
    # Two consecutive polls whose staleness windows overlap, so neither is provably first and the
    # transition between them cannot be ordered from the evidence.
    for marketplace in ("fr",):
        listing = "overlapping"
        timeline = [(START, "active"), (START + timedelta(hours=12), "pending")]
        record_truth(marketplace, listing, timeline)
        schedule = [(4, 3600), (8, 14400), (12, 3600), (16, 14400), (20, 3600), (24, 10800)]
        for hours, age in schedule:
            poll(marketplace, listing, timeline, at=START + timedelta(hours=hours),
                 actual_age=age, lifetime=18000, send_age_header=True, send_etag=True)

    return readings, truth


def main() -> None:
    readings, truth = build()

    (HERE / "labels.json").write_text(
        json.dumps(LABELS, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    lines = [json.dumps(row, ensure_ascii=False) for row in readings]
    # One unreadable row, so the loader's problem reporting is exercised by the shipped corpus rather
    # than only by a unit test.
    lines.append(json.dumps({"marketplace": "us", "listing_id": "broken", "raw_label": "Active"}))
    (HERE / "readings.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    (HERE / "truth.jsonl").write_text(
        "\n".join(json.dumps(row) for row in truth) + "\n", encoding="utf-8"
    )

    print(f"{len(readings)} readings (+1 deliberately malformed)")
    print(f"{len(truth)} true changes")
    print(f"{len(LABELS)} marketplaces, {sum(len(t) for t in LABELS.values())} labels")


if __name__ == "__main__":
    main()
