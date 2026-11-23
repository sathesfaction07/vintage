"""Text, JSON and CSV output.

Three rules, the same ones the rest of this portfolio uses: an unknown value renders as `n/a` in
text, `null` in JSON and an empty cell in CSV -- never as zero. A category that came back empty is
still printed, so "considered and found nothing" is distinguishable from "not examined". And a
lower bound of negative infinity is printed as such rather than as a date, because a date there
would be read as a measurement.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import datetime, timedelta

from .measure import AttributionScore, WidthBudget
from .models import Change, LabelConflict, ObservationCount, Ordering, Reading, StateSpan, Status

UNKNOWN_TEXT = "n/a"
UNBOUNDED_TEXT = "unbounded"


def pct(value: float | None) -> str:
    return UNKNOWN_TEXT if value is None else f"{value:.1%}"


def hours(value: float | None) -> str:
    return UNKNOWN_TEXT if value is None else f"{value:.2f}h"


def instant(value: datetime | None) -> str:
    return "-inf" if value is None else value.strftime("%Y-%m-%d %H:%M")


def span_text(value: timedelta | None) -> str:
    return UNKNOWN_TEXT if value is None else f"{value.total_seconds() / 3600:.2f}h"


def render_readings(readings: Sequence[Reading], *, stale_after: timedelta) -> str:
    out = io.StringIO()
    out.write(f"{len(readings)} reading(s)\n\n")
    out.write(f"{'marketplace':<13}{'listing':<14}{'status':<12}{'polled':<18}"
              f"{'provenance':<11}{'age':<26}{'describes origin at'}\n")
    out.write("-" * 118 + "\n")
    for item in sorted(readings, key=lambda entry: (entry.marketplace, entry.listing_id,
                                                    entry.observed_at)):
        window = item.window
        described = f"[{instant(window.start)} .. {window.end:%m-%d %H:%M}]"
        out.write(
            f"{item.marketplace:<13}{item.listing_id:<14}{item.status.value:<12}"
            f"{item.observed_at:%m-%d %H:%M}      {item.provenance.value:<11}"
            f"{item.age.describe():<26}{described}\n"
        )

    provable = [item for item in readings if item.age.min_seconds > stale_after.total_seconds()]
    undated = [item for item in readings if not item.is_dated]
    unmapped = [item for item in readings if item.status is Status.UNKNOWN]
    out.write(f"\n{len(provable)} reading(s) PROVABLY staler than "
              f"{stale_after.total_seconds() / 60:.0f} minutes\n")
    out.write(f"{len(undated)} reading(s) UNDATED -- age has no upper bound, so these are not\n"
              f"  provably stale and not provably fresh either\n")
    out.write(f"{len(unmapped)} reading(s) with a label the mapping does not know\n")
    return out.getvalue()


def render_changes(changes: Sequence[Change]) -> str:
    out = io.StringIO()
    out.write(f"{len(changes)} change(s)\n\n")
    out.write(f"{'marketplace':<13}{'listing':<14}{'transition':<26}"
              f"{'after':<18}{'at or before':<18}{'ordering':<13}{'width':<9}{'naive says'}\n")
    out.write("-" * 128 + "\n")
    for item in changes:
        transition = f"{item.from_status.value} -> {item.to_status.value}"
        out.write(
            f"{item.marketplace:<13}{item.listing_id:<14}{transition:<26}"
            f"{instant(item.after):<18}{item.at_or_before:%Y-%m-%d %H:%M}  "
            f"{item.ordering.value:<13}{span_text(item.width) if item.width else UNBOUNDED_TEXT:<9}"
            f"{item.confirmed_at:%m-%d %H:%M}\n"
        )

    counts = dict.fromkeys(Ordering, 0)
    for item in changes:
        counts[item.ordering] += 1
    out.write("\nordering:\n")
    for value, count in counts.items():
        out.write(f"  {value.value:<13}{count:>4}\n")

    unbounded = sum(1 for item in changes if not item.is_bounded)
    if unbounded:
        out.write(f"\n{unbounded} change(s) have NO lower bound: the change is detectable and not\n"
                  f"  locatable, because no dated reading established the previous value.\n")

    late = [item.naive_lateness for item in changes if item.naive_lateness > timedelta(0)]
    if late:
        out.write(f"\nnaive attribution would be late on {len(late)} of {len(changes)} change(s),\n"
                  f"  by up to {max(late).total_seconds() / 3600:.2f}h. Never early -- see ADR 002.\n")
    return out.getvalue()


def render_spans(spans: Sequence[StateSpan]) -> str:
    out = io.StringIO()
    out.write(f"{len(spans)} span(s)\n\n")
    out.write(f"{'marketplace':<13}{'listing':<14}{'status':<12}"
              f"{'shortest':<11}{'longest':<11}{'naive':<11}{'note'}\n")
    out.write("-" * 90 + "\n")
    for item in spans:
        note = ""
        if item.is_open:
            note = "still in this state"
        elif item.entered is None:
            note = "began before monitoring started"
        elif item.max_duration is None:
            note = "entry unbounded, so the run could be arbitrarily long"
        out.write(
            f"{item.marketplace:<13}{item.listing_id:<14}{item.status.value:<12}"
            f"{span_text(item.min_duration):<11}{span_text(item.max_duration):<11}"
            f"{span_text(item.naive_duration):<11}{note}\n"
        )
    out.write("\nA duration is a RANGE because both of its endpoints are ranges. The naive column is\n"
              "  a single number in the same place, which is why an SLA built on it is unfalsifiable.\n")
    return out.getvalue()


def render_observations(counts: dict[tuple[str, str], ObservationCount]) -> str:
    out = io.StringIO()
    out.write(f"{'marketplace':<13}{'listing':<14}{'requests':>10}{'observations':>14}"
              f"{'echoes':>9}{'independence':>14}\n")
    out.write("-" * 74 + "\n")
    for (marketplace, listing), count in sorted(counts.items()):
        out.write(
            f"{marketplace:<13}{listing:<14}{count.requests:>10}{count.distinct_generations:>14}"
            f"{count.echoes:>9}{pct(count.independence_ratio):>14}\n"
        )
    out.write("\nAn echo is a request answered from a cache entry already seen, so it adds no\n"
              "  evidence. Three checks returning the same answer are ONE observation if they\n"
              "  shared one generation -- and they read as corroboration, which is the trap.\n")
    return out.getvalue()


def render_conflicts(conflicts: Sequence[LabelConflict]) -> str:
    if not conflicts:
        return "no label conflicts: every shared label maps to one canonical status\n"
    out = io.StringIO()
    out.write(f"{len(conflicts)} label conflict(s) -- the same wording means different things\n\n")
    for item in conflicts:
        out.write(f"  {item.label!r}\n")
        for marketplace, status in item.mappings.items():
            out.write(f"      {marketplace:<6}{status.value}\n")
    out.write("\nAny count spanning these marketplaces is undefined, and it will still compute.\n")
    return out.getvalue()


def render_comparison(scores: dict[str, AttributionScore], budget: WidthBudget) -> str:
    out = io.StringIO()
    out.write(f"{'strategy':<11}{'matched':>9}{'contains':>10}{'rate':>9}{'bounded':>10}"
              f"{'spurious':>10}{'late':>6}{'early':>7}{'mean width':>12}{'mean late':>11}\n")
    out.write("-" * 95 + "\n")
    for name, item in scores.items():
        out.write(
            f"{name:<11}{item.matched:>9}{item.containing:>10}{pct(item.containment_rate):>9}"
            f"{pct(item.bounded_containment_rate):>10}{item.spurious:>10}{item.late_count:>6}"
            f"{item.early_count:>7}{hours(item.mean_width_hours):>12}"
            f"{hours(item.mean_lateness_hours):>11}\n"
        )

    out.write("\nwidth budget\n")
    out.write(f"  polling floor          {budget.poll_interval_hours:>7.2f}h   "
              f"no arithmetic beats this: a change can only be placed between two polls\n")
    out.write(f"  mean interval width    {hours(budget.mean_width_hours):>8}\n")
    out.write(f"  excess over the floor  {hours(budget.excess_hours):>8}   bought with cache staleness\n")
    out.write(f"  staleness share        {pct(budget.staleness_share):>8}\n")

    out.write("\nhow to read this\n")
    out.write("  A point estimate contains an instant only on exact equality, so naive containment\n")
    out.write("  is near-zero by construction and is not the interesting number. The interesting\n")
    out.write("  ones are SPURIOUS -- transitions that never happened -- and the fact that every\n")
    out.write("  naive error is late and none is early. A one-sided error is a bias, and a bias\n")
    out.write("  does not cancel across a fleet the way noise does.\n")
    return out.getvalue()


def changes_json(changes: Sequence[Change], scores: dict[str, AttributionScore],
                 budget: WidthBudget) -> str:
    return json.dumps(
        {
            "changes": [
                {
                    "marketplace": item.marketplace,
                    "listing_id": item.listing_id,
                    "from_status": item.from_status.value,
                    "to_status": item.to_status.value,
                    "after": item.after.isoformat() if item.after else None,
                    "at_or_before": item.at_or_before.isoformat(),
                    "ordering": item.ordering.value,
                    "confirmed_at": item.confirmed_at.isoformat(),
                    "width_hours": (
                        item.width.total_seconds() / 3600 if item.width is not None else None
                    ),
                    "naive_lateness_hours": item.naive_lateness.total_seconds() / 3600,
                }
                for item in changes
            ],
            "strategies": {
                name: {
                    "matched": item.matched,
                    "containing": item.containing,
                    "containment_rate": item.containment_rate,
                    "bounded_containment_rate": item.bounded_containment_rate,
                    "direction_rate": item.direction_rate,
                    "spurious": item.spurious,
                    "missed": item.missed,
                    "unbounded": item.unbounded,
                    "late_count": item.late_count,
                    "early_count": item.early_count,
                    "reversed_orderings": item.reversed_orderings,
                    "overlapping_orderings": item.overlapping_orderings,
                    "mean_width_hours": item.mean_width_hours,
                    "mean_lateness_hours": item.mean_lateness_hours,
                    "max_lateness_hours": item.max_lateness_hours,
                }
                for name, item in scores.items()
            },
            "width_budget": {
                "poll_interval_hours": budget.poll_interval_hours,
                "mean_width_hours": budget.mean_width_hours,
                "excess_hours": budget.excess_hours,
                "staleness_share": budget.staleness_share,
            },
        },
        indent=2,
    )


def changes_csv(changes: Sequence[Change]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["marketplace", "listing_id", "from_status", "to_status", "after", "at_or_before",
         "ordering", "confirmed_at", "width_hours"]
    )
    for item in changes:
        writer.writerow(
            [
                item.marketplace,
                item.listing_id,
                item.from_status.value,
                item.to_status.value,
                item.after.isoformat() if item.after else "",
                item.at_or_before.isoformat(),
                item.ordering.value,
                item.confirmed_at.isoformat(),
                f"{item.width.total_seconds() / 3600:.4f}" if item.width is not None else "",
            ]
        )
    return buffer.getvalue()
