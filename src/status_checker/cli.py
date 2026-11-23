"""CLI.

Exit codes gate on what is demonstrable:

  `check`   1 on an unreadable row, or on a LABEL CONFLICT. Both make a reported status arbitrary --
            an unreadable row is a listing we know nothing about, and a conflicting label means the
            canonical status depends on which table you consulted.
            NOT on staleness or on an unmapped label. A cache serving old content is normal, and a
            check that fails whenever a platform reworded something would be switched off in a week.
            Both are reported loudly instead.

  `history` 1 only if the store cannot be written.

  `audit`   1 only if the INTERVAL strategy reports a change that never happened. The naive
            baseline invents two on the shipped corpus and that does NOT fail the run, because it
            is the finding rather than a regression -- and a check that fires on every run carries
            no information.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import click

from .attribute import (
    changes as attribute_changes,
)
from .attribute import (
    group_readings,
    inversions,
    naive_changes,
    observation_count,
    spans,
)
from .config import Settings
from .history import History
from .labels import LabelMap
from .measure import compare, width_budget
from .models import Provenance, Status
from .render import (
    changes_csv,
    changes_json,
    render_changes,
    render_comparison,
    render_conflicts,
    render_observations,
    render_readings,
    render_spans,
)
from .sources import load_readings, load_truth, unknown_labels


def _load(settings: Settings) -> tuple[LabelMap, list, list[str]]:
    labels, label_problems = LabelMap.from_file(settings.labels)
    readings, reading_problems = load_readings(settings.readings, labels)
    return labels, readings, label_problems + reading_problems


@click.group()
@click.version_option()
def cli() -> None:
    """Read listing status across marketplaces and report how old each reading actually is."""


@cli.command()
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Write JSON and CSV here")
def check(output: Path | None) -> None:
    """Report every reading with the origin window it describes."""
    settings = Settings()
    labels, readings, problems = _load(settings)

    click.echo(render_readings(readings, stale_after=timedelta(seconds=settings.stale_after_seconds)))

    conflicts = labels.conflicts()
    click.echo(render_conflicts(conflicts))

    missing = labels.missing_statuses()
    if missing:
        click.echo(f"{len(missing)} marketplace(s) cannot express every status:")
        for marketplace, statuses in sorted(missing.items()):
            names = ", ".join(sorted(status.value for status in statuses))
            click.echo(f"  {marketplace:<6}cannot report: {names}")
        click.echo("  A zero for one of these is not a finding -- the table cannot say otherwise.\n")

    unmapped = unknown_labels(readings)
    if unmapped:
        click.echo(f"{len(unmapped)} unmapped label(s) -- the platform's wording has drifted:")
        for (marketplace, label), count in sorted(unmapped.items()):
            click.echo(f"  {marketplace:<6}{label!r} seen {count}x")
        click.echo("  Each is a line to add to the mapping. Reported rather than resolved by a\n"
                   "  model: an unknown label IS the signal that the config needs updating, and\n"
                   "  resolving it silently would let the mapping rot undetected.\n")

    if problems:
        click.echo(f"{len(problems)} unreadable row(s):")
        for problem in problems:
            click.echo(f"  {problem}")

    provenance_counts = dict.fromkeys(Provenance, 0)
    for reading in readings:
        provenance_counts[reading.provenance] += 1
    click.echo("\nprovenance:")
    for value, count in provenance_counts.items():
        click.echo(f"  {value.value:<9}{count:>5}")

    if problems or conflicts:
        click.echo("\nFAIL: an unreadable row or a conflicting label makes a reported status arbitrary")
        sys.exit(1)


@cli.command()
@click.option("--naive", is_flag=True, help="Attribute by request time instead of origin window")
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Write JSON and CSV here")
def changes(naive: bool, output: Path | None) -> None:
    """Locate status changes, as intervals by default."""
    settings = Settings()
    _, readings, problems = _load(settings)

    strategy = naive_changes if naive else attribute_changes
    found = []
    for group in group_readings(readings).values():
        found.extend(strategy(group))
    found.sort(key=lambda item: (item.marketplace, item.listing_id, item.at_or_before))

    click.echo(f"strategy: {'naive (request time)' if naive else 'interval (origin window)'}\n")
    click.echo(render_changes(found))

    stream_inversions = inversions(readings)
    if stream_inversions:
        click.echo(f"\n{len(stream_inversions)} request pair(s) INVERTED: the later request provably\n"
                   f"  describes an earlier origin state. A cache is serving content older than the\n"
                   f"  poll interval, which is an infrastructure fact no downstream arithmetic fixes.")
        for earlier, later in stream_inversions[:3]:
            click.echo(f"    {earlier.marketplace}/{earlier.listing_id} "
                       f"{earlier.observed_at:%m-%d %H:%M} -> {later.observed_at:%m-%d %H:%M}")
        if len(stream_inversions) > 3:
            click.echo(f"    ... and {len(stream_inversions) - 3} more")

    if output:
        output.mkdir(parents=True, exist_ok=True)
        (output / "changes.csv").write_text(changes_csv(found), encoding="utf-8")
        click.echo(f"\nwrote {output / 'changes.csv'}")

    if problems:
        click.echo(f"\n{len(problems)} unreadable row(s)")
        sys.exit(1)


@cli.command()
def durations() -> None:
    """Time spent in each status, as ranges."""
    settings = Settings()
    _, readings, _ = _load(settings)

    all_spans = []
    for group in group_readings(readings).values():
        all_spans.extend(spans(group))
    all_spans.sort(key=lambda item: (item.marketplace, item.listing_id))
    click.echo(render_spans(all_spans))

    counts = {key: observation_count(group) for key, group in group_readings(readings).items()}
    click.echo(render_observations(counts))


@cli.command()
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Write JSON here")
def audit(output: Path | None) -> None:
    """Score both attribution strategies against known change instants."""
    settings = Settings()
    _, readings, _ = _load(settings)
    truth, truth_problems = load_truth(settings.truth)

    interval_found, naive_found = [], []
    for group in group_readings(readings).values():
        interval_found.extend(attribute_changes(group))
        naive_found.extend(naive_changes(group))

    scores = compare(interval_found, naive_found, truth)
    budget = width_budget(scores["interval"], timedelta(seconds=settings.poll_interval_seconds))

    click.echo(f"{len(readings)} readings, {len(truth)} known change instants\n")
    click.echo(render_comparison(scores, budget))

    if output:
        output.mkdir(parents=True, exist_ok=True)
        (output / "audit.json").write_text(changes_json(interval_found, scores, budget),
                                           encoding="utf-8")
        (output / "changes.csv").write_text(changes_csv(interval_found), encoding="utf-8")
        click.echo(f"\nwrote {output / 'audit.json'} and {output / 'changes.csv'}")

    # Checked BEFORE any conclusion is drawn from the scores. An unreadable known-changes file makes
    # every claim look spurious, and reporting that as "the strategy invented 17 transitions" blames
    # the code for a broken fixture.
    if truth_problems:
        click.echo(f"\nFAIL: {len(truth_problems)} unreadable row(s) in the known-changes file, so")
        click.echo("  nothing above can be scored against it")
        for problem in truth_problems:
            click.echo(f"    {problem}")
        sys.exit(1)

    naive_phantoms = scores["naive"].spurious
    if naive_phantoms:
        click.echo(f"\nnaive attribution invented {naive_phantoms} transition(s) that never happened.")
        click.echo("  Not a failure of this run -- it IS the finding. A cache serving content older")
        click.echo("  than the poll interval makes a listing appear to flap, and a phantom flap is")
        click.echo("  worse than a late timestamp because it pages someone.")

    # Gate on the strategy actually shipped. Failing because the BASELINE is bad would make this
    # check fire on every run, and a check that always fires carries no information.
    if scores["interval"].spurious:
        click.echo(f"\nFAIL: interval attribution reported {scores['interval'].spurious} "
                   f"transition(s) with no counterpart in the known changes")
        sys.exit(1)


@cli.command()
def record() -> None:
    """Write readings and attributed changes to the SQLite history."""
    settings = Settings()
    _, readings, _ = _load(settings)

    found = []
    for group in group_readings(readings).values():
        found.extend(attribute_changes(group))

    store = History(settings.history)
    store.initialise()
    inserted_readings = store.record_readings(readings)
    inserted_changes = store.record_changes(found)
    counts = store.counts()

    click.echo(f"history: {settings.history}")
    click.echo(f"  inserted {inserted_readings} reading(s), {inserted_changes} change(s)")
    click.echo(f"  stored   {counts['readings']} reading(s), {counts['changes']} change(s)")
    click.echo(f"  {counts['undated_readings']} reading(s) with age_max NULL -- unbounded, not zero")
    click.echo(f"  {counts['unbounded_changes']} change(s) with after NULL -- detectable, not locatable")
    click.echo("\nThe schema has `after` and `at_or_before`, not `changed_at`. A single timestamp\n"
               "column cannot hold 'somewhere between 04:40 and 08:10', so it forces the poll time\n"
               "-- and every consumer downstream inherits that as if it were the event time.")


@cli.command()
def doctor() -> None:
    """Config, inputs, and how to read the vocabulary."""
    settings = Settings()
    click.echo("configuration")
    for field in ("readings", "labels", "truth", "history"):
        path = getattr(settings, field)
        mark = "ok" if path.exists() else "MISSING"
        click.echo(f"  {field:<12}{path!s:<28}{mark}")
    click.echo(f"  {'stale after':<12}{settings.stale_after_seconds}s")
    click.echo(f"  {'poll interval':<12}{settings.poll_interval_seconds}s")

    click.echo("\nno credentials, no network, no model. The accuracy question here is how old a")
    click.echo("reading is, which is arithmetic on response headers.")

    click.echo("\nprovenance")
    click.echo("  origin    no cache in the path we can detect -- age is zero")
    click.echo("  cached    a cache told us how long it held the response")
    click.echo("  undated   no Age header, so we cannot confirm either way. NOT the same as fresh")

    click.echo("\nordering")
    click.echo("  proven        the earlier request's window ends before the later one's begins")
    click.echo("  reversed      the LATER request describes an EARLIER origin state")
    click.echo("  overlapping   the windows overlap, so origin order is not established")
    click.echo("  unbounded     a reading is undated, so it cannot be placed at all")

    click.echo("\nwhat a reading proves")
    click.echo("  A reading is an observation about the PAST. A value read through a two-hour cache")
    click.echo("  describes the origin two hours ago, and nothing in the value says so. An unchanged")
    click.echo("  status and a stale reading of a changed status are identical in the data.")

    missing = [
        field for field in ("readings", "labels", "truth") if not getattr(settings, field).exists()
    ]
    if missing:
        click.echo(f"\nFAIL: missing input(s): {', '.join(missing)}")
        click.echo("Run: python data/generate.py")
        sys.exit(1)


@cli.command()
@click.argument("marketplace")
@click.argument("listing_id")
def explain(marketplace: str, listing_id: str) -> None:
    """Every reading for one listing, with the window each describes."""
    settings = Settings()
    labels, readings, _ = _load(settings)

    group = [
        item for item in readings
        if item.marketplace == marketplace and item.listing_id == listing_id
    ]
    if not group:
        click.echo(f"no readings for {marketplace}/{listing_id}")
        if not labels.knows_marketplace(marketplace):
            click.echo(f"  and {marketplace!r} is not in the label mapping at all, so every reading")
            click.echo("  from it would resolve to UNKNOWN")
        sys.exit(1)

    click.echo(f"{marketplace}/{listing_id}: {len(group)} reading(s)\n")
    for item in sorted(group, key=lambda entry: entry.observed_at):
        window = item.window
        start = "-inf" if window.start is None else f"{window.start:%m-%d %H:%M}"
        click.echo(
            f"  polled {item.observed_at:%m-%d %H:%M}  {item.raw_label[:24]:<26}"
            f"{item.status.value:<11}{item.provenance.value:<9}"
            f"age {item.age.describe():<28}describes [{start} .. {window.end:%m-%d %H:%M}]"
        )

    count = observation_count(group)
    click.echo(f"\n  {count.requests} request(s) -> {count.distinct_generations} distinct origin "
               f"generation(s), {count.echoes} echo(es)")
    if count.echoes:
        click.echo(f"  Independence {count.independence_ratio:.0%}: most checks re-read one cache")
        click.echo("  entry. A repeated answer here is not corroboration.")

    click.echo()
    click.echo(render_changes(attribute_changes(group)))
    click.echo(render_spans(spans(group)))

    unmapped = {item.raw_label for item in group if item.status is Status.UNKNOWN}
    if unmapped:
        click.echo(f"  unmapped label(s): {sorted(unmapped)}")
        click.echo("  Excluded from attribution: a transition into UNKNOWN is a fact about our")
        click.echo("  config, and including it would manufacture two phantom status changes.")
