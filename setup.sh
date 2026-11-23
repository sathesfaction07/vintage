#!/usr/bin/env bash
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN} ok ${NC} $1"; }
fail() { echo -e "${RED}fail${NC} $1"; exit 1; }

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'USAGE'
Usage: bash setup.sh

No options, no credentials, no network, no model. The accuracy question here is how old a
reading is, which is arithmetic on response headers.
USAGE
            exit 0 ;;
        *) fail "unknown option: $arg (try --help)" ;;
    esac
done

info "Checking Python"
command -v python3 >/dev/null 2>&1 || fail "python3 not found (macOS: brew install python@3.12)"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
[ "$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')" = "1" ] \
    || fail "Python 3.11+ required (StrEnum). Found $PY_VER."
ok "Python $PY_VER"

info "Creating virtual environment"
[ -d .venv ] || python3 -m venv .venv || fail "could not create .venv"
ok ".venv ready"

info "Installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]" || fail "pip install failed"
ok "dependencies installed"

info "Verifying imports"
.venv/bin/python - <<'PYCHECK' || fail "core imports failed"
import sqlite3                                        # noqa: F401
import click                                          # noqa: F401
from pydantic_settings import BaseSettings            # noqa: F401
from status_checker.attribute import changes          # noqa: F401
from status_checker.freshness import age_from_headers  # noqa: F401
from status_checker.measure import score              # noqa: F401
PYCHECK
ok "imports resolve"

info "Generating the corpus"
[ -f data/readings.jsonl ] || .venv/bin/python data/generate.py || fail "could not generate data"
ok "corpus present"

info "Deriving age bounds from response headers rather than assuming them"
.venv/bin/python - <<'PYCHECK' || fail "the age arithmetic does not hold"
from datetime import UTC, datetime, timedelta

from status_checker.freshness import age_from_headers

now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
cases = [
    ("no-store",                     dict(cache_control="no-store")),
    ("max-age=0",                    dict(cache_control="max-age=0")),
    ("Age 1h, max-age 2h",           dict(age_header=3600, cache_control="max-age=7200")),
    ("Age 1h, NO lifetime",          dict(age_header=3600)),
    ("no Age, no cache headers",     dict()),
    ("no Age, max-age 600",          dict(cache_control="max-age=600")),
    ("served PAST its lifetime",     dict(age_header=9000, cache_control="max-age=7200")),
    ("s-maxage beats max-age",       dict(age_header=10, cache_control="max-age=99999, s-maxage=60")),
    ("Date 30m ago, no Age",         dict(date_header=now - timedelta(minutes=30))),
]
print(f"     {'response':<28}{'provenance':<10}{'age bound':<32}dated")
for label, kwargs in cases:
    provenance, age = age_from_headers(observed_at=now, **kwargs)
    print(f"     {label:<28}{provenance.value:<10}{age.describe():<32}{age.is_dated}")

# The one that matters: a missing Age header is UNDATED, not fresh.
provenance, age = age_from_headers(observed_at=now)
assert provenance.value == "undated", provenance
assert age.max_seconds is None
# And a cache that ignored its own lifetime forfeits the upper bound rather than lending a
# bound narrower than the evidence supports.
_, over = age_from_headers(observed_at=now, age_header=9000, cache_control="max-age=7200")
assert over.max_seconds is None
print()
print("     A conforming cache MUST send Age, so its absence is EVIDENCE of a direct response")
print("     and not proof of one. Treating it as zero converts 'we cannot tell' into 'it is")
print("     current', and the two errors do not cost the same.")
PYCHECK
ok "age bounds derived from headers"

info "Deriving the inversion: a later request can describe an EARLIER origin state"
.venv/bin/python - <<'PYCHECK' || fail "the inversion does not behave as documented"
from datetime import UTC, datetime, timedelta

from status_checker.attribute import changes, classify_ordering, inversions, naive_changes
from status_checker.models import AgeBound, Provenance, Reading, Status

base = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)

def read(minutes, status, lo=0, hi=0, gen=None):
    return Reading(
        marketplace="es", listing_id="L", raw_label=status.value, status=status,
        observed_at=base + timedelta(minutes=minutes),
        provenance=Provenance.CACHED if lo else Provenance.ORIGIN,
        age=AgeBound(min_seconds=lo, max_seconds=hi), generation=gen,
    )

# Truth: ACTIVE -> RESTRICTED at 08:00.
fresh_before = read(240, Status.ACTIVE, gen="a")                       # 04:00, fresh
fresh_after  = read(490, Status.RESTRICTED, gen="b")                   # 08:10, fresh
stale        = read(520, Status.ACTIVE, 10800, 14400, gen="c")         # 08:40, 3-4h old

print(f"     {'request':<10}{'status':<12}describes origin at")
for item in (fresh_before, fresh_after, stale):
    window = item.window
    print(f"     {item.observed_at:%H:%M}     {item.status.value:<12}"
          f"[{window.start:%H:%M} .. {window.end:%H:%M}]")

assert classify_ordering(fresh_after, stale).value == "reversed"
assert stale.window.precedes(fresh_after.window)
print()
print("     The 08:40 request describes 04:40-05:40. EVERY instant it could be describing falls")
print("     before the instant the 08:10 request describes, so the two ORIGIN states are in the")
print("     reverse of the two REQUEST orders.")
print()

stream = [fresh_before, fresh_after, stale]
naive = naive_changes(stream)
interval = changes(stream)
assert len(naive) == 2, len(naive)
assert len(interval) == 1, len(interval)
print(f"     naive    reports {len(naive)} change(s):")
for item in naive:
    print(f"                {item.from_status.value:>10} -> {item.to_status.value}")
print(f"     interval reports {len(interval)} change(s):")
for item in interval:
    print(f"                {item.from_status.value:>10} -> {item.to_status.value}"
          f"   ({item.after:%H:%M} .. {item.at_or_before:%H:%M}]")
assert interval[0].contains(base + timedelta(hours=8))
assert len(inversions(stream)) == 1

print()
print("     A PHANTOM FLAP. The listing never left RESTRICTED, and naive attribution reports a")
print("     round trip out of it and back. A wrong instant is a wrong number; an invented")
print("     transition pages someone.")
PYCHECK
ok "the inversion and the phantom flap both derived"

info "Deriving that naive attribution is late in ONE direction"
.venv/bin/python - <<'PYCHECK' || fail "the lateness theorem does not hold"
from datetime import UTC, datetime, timedelta

from status_checker.attribute import changes
from status_checker.models import AgeBound, Provenance, Reading, Status

base = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)

def read(hours, status, lo, hi, gen):
    return Reading(
        marketplace="de", listing_id="L", raw_label=status.value, status=status,
        observed_at=base + timedelta(hours=hours),
        provenance=Provenance.CACHED if lo else Provenance.ORIGIN,
        age=AgeBound(min_seconds=lo, max_seconds=hi), generation=gen,
    )

print(f"     {'confirming age':>16}{'naive lateness':>18}")
for age in (0, 60, 3600, 21600, 86400):
    found = changes([
        read(0, Status.ACTIVE, 0, 0, "a"),
        read(48, Status.RESTRICTED, age, age + 600, "b"),
    ])[0]
    lateness = found.naive_lateness
    assert lateness >= timedelta(0)
    assert lateness.total_seconds() == age
    print(f"     {age:>14}s{lateness.total_seconds():>16.0f}s")

print()
print("     `at_or_before` is `confirmed_at` minus the confirming reading's MINIMUM age, so the")
print("     naive timestamp is late or exact and never early. That matters more than its size:")
print("     a one-sided error is a BIAS, and a bias does not cancel across a fleet the way")
print("     noise does. An average built on it is wrong rather than merely noisy.")
PYCHECK
ok "the lateness theorem holds"

info "Deriving that requests are not observations"
.venv/bin/python - <<'PYCHECK' || fail "echo counting does not behave as documented"
from datetime import UTC, datetime, timedelta

from status_checker.attribute import collapse_echoes, observation_count
from status_checker.models import AgeBound, Provenance, Reading, Status

base = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
shared = [
    Reading(
        marketplace="us", listing_id="L", raw_label="Active", status=Status.ACTIVE,
        observed_at=base + timedelta(minutes=20 * step), provenance=Provenance.CACHED,
        age=AgeBound(min_seconds=60, max_seconds=21600), generation="one-entry",
    )
    for step in range(9)
]
count = observation_count(shared)
print(f"     {count.requests} requests answered from {count.distinct_generations} cache entry")
print(f"     -> {count.echoes} echoes, independence {count.independence_ratio:.0%}")
assert count.distinct_generations == 1
assert count.echoes == 8
assert len(collapse_echoes(shared)) == 1

anonymous = [
    Reading(
        marketplace="us", listing_id="L", raw_label="Active", status=Status.ACTIVE,
        observed_at=base + timedelta(hours=step), provenance=Provenance.UNDATED,
        age=AgeBound.unbounded(), generation=None,
    )
    for step in range(3)
]
assert len(collapse_echoes(anonymous)) == 3
print()
print("     Nine checks returning the same answer are ONE observation, and they read as")
print("     corroboration -- which is the trap. Readings with no identifier are never collapsed:")
print("     absence of an identifier is not evidence of shared identity.")
PYCHECK
ok "requests are not observations"

info "Measuring both strategies against the shipped corpus"
.venv/bin/python - <<'PYCHECK' || fail "the corpus did not behave as documented"
from datetime import timedelta
from pathlib import Path

from status_checker.attribute import (
    changes, group_readings, inversions, naive_changes, observation_count,
)
from status_checker.labels import LabelMap
from status_checker.measure import compare, width_budget
from status_checker.models import Ordering, Provenance
from status_checker.sources import load_readings, load_truth, unknown_labels

labels, label_problems = LabelMap.from_file(Path("data/labels.json"))
readings, reading_problems = load_readings(Path("data/readings.jsonl"), labels)
truth, truth_problems = load_truth(Path("data/truth.jsonl"))
assert not label_problems and not truth_problems
assert len(reading_problems) == 1, reading_problems
print(f"     {len(readings)} readings, {len(truth)} known change instants, "
      f"{len(labels.marketplaces)} marketplaces")
print(f"     the one deliberately malformed row is reported: {reading_problems[0]}")

assert {item.provenance for item in readings} == set(Provenance)
print(f"     every provenance occurs, so no assertion below is vacuous")

grouped = group_readings(readings)
interval_found, naive_found = [], []
for group in grouped.values():
    interval_found.extend(changes(group))
    naive_found.extend(naive_changes(group))

scores = compare(interval_found, naive_found, truth)
print()
print(f"     {'strategy':<10}{'contains':>10}{'rate':>9}{'bounded':>10}{'spurious':>10}"
      f"{'late':>6}{'early':>7}{'mean width':>12}{'mean late':>11}")
for name, item in scores.items():
    width = f"{item.mean_width_hours:.2f}h" if item.mean_width_hours is not None else "n/a"
    late = f"{item.mean_lateness_hours:.2f}h" if item.mean_lateness_hours is not None else "n/a"
    bounded = (f"{item.bounded_containment_rate:.1%}"
               if item.bounded_containment_rate is not None else "n/a")
    print(f"     {name:<10}{item.containing:>10}{item.containment_rate:>8.1%}{bounded:>10}"
          f"{item.spurious:>10}{item.late_count:>6}{item.early_count:>7}{width:>12}{late:>11}")

assert scores["interval"].containment_rate == 1.0
assert scores["interval"].bounded_containment_rate == 1.0
assert scores["interval"].spurious == 0
assert scores["naive"].early_count == 0
assert scores["naive"].late_count == 17
assert scores["naive"].spurious == 2
print()
print("     Interval attribution contains EVERY true instant, 17 of 17, and invents nothing.")
print("     Naive is late on all 17 and early on none -- the bias -- and invents two.")
print()
print("     Naive containment of 0% is near-TAUTOLOGICAL and is not the interesting number: a")
print("     point claim contains an instant only on exact equality. The interesting numbers are")
print("     the 2 phantom transitions and the 17-0 late-to-early split.")

budget = width_budget(scores["interval"], timedelta(hours=4))
print()
print(f"     polling floor        {budget.poll_interval_hours:>6.2f}h   unavoidable: a change can only")
print(f"                                     be placed between two polls")
print(f"     mean width           {budget.mean_width_hours:>6.2f}h")
print(f"     excess over floor    {budget.excess_hours:>6.2f}h   bought with cache staleness")
print(f"     staleness share      {budget.staleness_share:>6.1%}")
assert budget.mean_width_hours >= 4.0
assert 0.0 < budget.staleness_share < 0.5
print()
print("     So 91% of the uncertainty here is CADENCE, not caching. Polling harder is the only")
print("     lever on that part, and the report says which part is which rather than implying all")
print("     of it is fixable.")

found = inversions(readings)
assert len(found) == 1, len(found)
print()
print(f"     {len(found)} inversion in the whole corpus, on {found[0][0].marketplace}/"
      f"{found[0][0].listing_id}")

count = observation_count(grouped[("us", "echoed")])
assert count.requests == 216 and count.distinct_generations == 12
print(f"     us/echoed: {count.requests} requests -> {count.distinct_generations} observations, "
      f"independence {count.independence_ratio:.1%}")

conflicts = labels.conflicts()
assert len(conflicts) == 1 and conflicts[0].label == "under review"
print()
print(f"     label conflict: {conflicts[0].label!r} means "
      + ", ".join(f"{m}={s.value}" for m, s in conflicts[0].mappings.items()))
print("     Any count spanning those marketplaces is undefined, and it will still compute to a")
print("     number that looks entirely reasonable.")

unmapped = unknown_labels(readings)
assert unmapped, "the wording-drift listing should produce an unmapped label"
for (marketplace, label), seen in unmapped.items():
    print(f"     unmapped label: {marketplace} {label!r} seen {seen}x -- a line to add to the config")

missing = labels.missing_statuses()
print(f"     {len(missing)} marketplace(s) cannot express every status, so a zero there is not a finding")

reworded = changes(grouped[("de", "reworded")])
assert len(reworded) == 1
print()
print("     The reworded listing yields ONE change, not three: a transition into UNKNOWN is a")
print("     fact about our config, so it is excluded from attribution rather than reported as a")
print("     status movement it would be indistinguishable from.")
PYCHECK
ok "the corpus behaves as documented"

info "Running the test suite"
.venv/bin/python -m pytest -q || fail "tests failed"
ok "tests pass"

cat <<'DONE'

Setup complete.

  source .venv/bin/activate

  status-checker doctor
  status-checker check                    # exits 1: a label conflict and a malformed row
  status-checker changes
  status-checker changes --naive          # the same data attributed by request time
  status-checker durations
  status-checker audit
  status-checker record                   # SQLite, with `after` and `at_or_before` columns
  status-checker explain es inverted      # the phantom flap, reading by reading
  status-checker explain us echoed        # 216 requests, 12 observations

`check` fails on an unreadable row or a conflicting label, because both make a reported status
arbitrary. It does NOT fail on staleness or on an unmapped label: a cache serving old content is
normal, and a check that fires whenever a platform reworded something gets switched off.

`audit` fails only if the INTERVAL strategy invents a transition. The naive baseline invents two
and that is the finding, not a regression.
DONE
