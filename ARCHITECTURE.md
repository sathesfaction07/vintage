# StatusChecker — Technical Architecture

## Overview

Reads listing status across marketplaces and reports how old each reading actually is.

**Design principle:** a status reading is an observation about the PAST. A value served through a
two-hour cache describes the origin two hours ago, and nothing in the value says so. An unchanged
status and a stale reading of a changed status are byte-identical in the data.

So a reading does not describe an instant — it describes an **interval** of origin time, and every
downstream answer inherits it. No model: the accuracy question is how old a reading is, which is
arithmetic on response headers.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                       CLI (status-checker)                                    │
│                                                                              │
│  check      every reading with the origin window it describes                 │
│             EXIT 1 on an unreadable row or a LABEL CONFLICT -- both make a     │
│             reported status arbitrary. NOT on staleness or an unmapped label:  │
│             a cache serving old content is normal, and a check that fires      │
│             whenever a platform reworded something gets switched off           │
│  changes    changes as INTERVALS, plus any request-order inversions           │
│             --naive attributes by request time instead, for comparison         │
│  durations  time in each status as RANGES, plus echo counts                   │
│  audit      both strategies scored against known change instants               │
│             EXIT 1 only if the INTERVAL strategy invents a transition. The     │
│             naive baseline invents two every run and gating on that would      │
│             make the check unconditional                                      │
│  record     SQLite, with `after` / `at_or_before` rather than `changed_at`     │
│  explain    one listing, reading by reading, with every window                │
│  doctor     config, and how to read the vocabulary                            │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                            freshness.py                                       │
│                                                                              │
│  RFC 9111 section 4.2.3, with TWO deliberate departures from what a caching   │
│  proxy does with the same arithmetic:                                         │
│    * a proxy wants ONE NUMBER so it can decide whether to serve. We want a    │
│      BOUND, because we are deciding what a value PROVES                       │
│    * a proxy treats a missing Age as ZERO. We treat it as UNKNOWN              │
│                                                                              │
│  response                     provenance   age bound         datable          │
│  no-store / no-cache          origin       0s                yes              │
│  max-age=0                    origin       0s                yes              │
│  Age 3600 + max-age 7200      cached       3600-7200s        yes              │
│  Age 3600, no lifetime        cached       >=3600s           NO               │
│  no Age, no cache headers     undated      >=0s              NO               │
│  no Age, max-age 600          undated      0-600s            yes              │
│  Age 9000 + max-age 7200      cached       >=9000s           NO               │
│                                                                              │
│  A conforming cache MUST send Age, so absence is EVIDENCE of a direct         │
│    response and not proof of one. Treating it as zero converts "we cannot     │
│    tell" into "it is current", and the two errors do not cost the same.        │
│                                                                              │
│  A cache serving PAST its own lifetime FORFEITS the upper bound -- keeping it  │
│    would give a bound narrower than the evidence supports.                     │
│                                                                              │
│  Expires is deliberately IGNORED: an instant on the origin's clock would       │
│    import skew into a bound whose whole purpose is to be safe. A Date in the   │
│    future is skew, floored at zero, not a response from the future.            │
│                                                                              │
│  generation_of()  ETag > Date > Last-Modified. Returns None when nothing      │
│    identifies the response, and None is NOT a shared identity.                 │
│                                                                              │
│  stale_by()  uses the LOWER bound, so it fires only when staleness is         │
│    CERTAIN. An undated reading is not reported stale -- it is not provably     │
│    stale and not provably fresh, and conflating them would put an              │
│    unfalsifiable claim next to a demonstrable one.                             │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              models.py                                        │
│                                                                              │
│  AgeBound(min_seconds, max_seconds)   max None = UNBOUNDED above              │
│  Window(start, end)                   the span of ORIGIN time a reading       │
│                                         describes. start None = -infinity      │
│                                                                              │
│    fresh at 12:00              -> [12:00 .. 12:00]   an instant               │
│    12:00, age 1-2h             -> [10:00 .. 11:00]   a span in the past       │
│    12:00, undated              -> [-inf  .. 12:00]   unbounded below          │
│                                                                              │
│  Window.precedes(other)  the ONLY way to establish that one reading describes │
│    an earlier origin state. Request order does NOT establish it.               │
│                                                                              │
│  Ordering -- four cases, because the relationship has four                     │
│    PROVEN        earlier request's window ends before the later one's begins   │
│    REVERSED      the LATER request describes an EARLIER origin state           │
│    OVERLAPPING   windows overlap, so origin order is not established           │
│    UNBOUNDED     a reading is undated and cannot be placed at all              │
│                                                                              │
│  Change  after / at_or_before / ordering / confirmed_at                       │
│    naive_lateness = confirmed_at - at_or_before, and at_or_before is           │
│      confirmed_at MINUS the confirming reading's minimum age -- so this is     │
│      NEVER negative. The naive timestamp is late or exact, never early.        │
│                                                                              │
│  StateSpan  min_duration = latest entry -> earliest exit, CLAMPED at zero      │
│             max_duration = earliest entry -> latest exit                       │
│             naive_duration kept alongside for comparison                       │
│                                                                              │
│  ObservationCount  requests / distinct_generations / echoes                    │
│    Two requests answered from one cache entry tell you what the origin said    │
│    ONCE, and they read as corroboration -- which is the trap.                  │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│              attribute.py                        labels.py                    │
│                                                                              │
│  _origin_key()  sorts by the ORIGIN     normalise() = NFKC + casefold +       │
│    window, NOT by request time. This      whitespace collapse. BOTH are        │
│    is the defect the module exists to     required: fullwidth needs NFKC,      │
│    correct.                               sharp-s needs casefold since         │
│                                           lower() leaves it alone. Same        │
│  collapse_echoes()  one reading per       conclusion as CaseSearch -- a        │
│    generation, keeping the TIGHTEST       REPEAT, cited as one.                │
│    window. Unidentified readings are                                          │
│    never collapsed together.            conflicts()  a label mapping to        │
│                                           DIFFERENT canonical statuses in      │
│  changes()   intervals, origin-ordered    different marketplaces. Static,      │
│  naive_changes()  points, request-        checkable, and FAILS THE BUILD:      │
│    ordered. Not a straw man -- on         every fleet-wide count assumes the   │
│    fresh data it is exactly right.        mapping is a function of the label   │
│                                           alone, and where it is not the       │
│  inversions()  request pairs whose        count still computes to a number     │
│    origin order REVERSES their            that looks reasonable.               │
│    request order. Scoped PER LISTING.                                         │
│    Reported as an infrastructure        missing_statuses()  tables that        │
│    fact, not silently fixed by the        cannot express a status at all, so   │
│    sort.                                  a zero there is not a finding.       │
│                                                                              │
│  skip_unknown  a transition into UNKNOWN is a fact about our CONFIG, so it is │
│    excluded from attribution -- including it would manufacture two phantom     │
│    changes around every rewording, indistinguishable from real movements.      │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│              measure.py                          history.py                   │
│                                                                              │
│  The question is not "how close is    THE SCHEMA IS THE ARGUMENT.             │
│    the timestamp" but "does the                                              │
│    claim CONTAIN the truth".          The obvious schema --                    │
│                                         (listing_id, status, changed_at) --   │
│  containment_rate is the headline.      cannot hold "somewhere between 04:40  │
│  bounded_containment_rate is the        and 08:10". One column holds one       │
│    HONEST headline: an unbounded        instant, so the only thing that fits   │
│    interval contains EVERY instant,     is the poll timestamp -- and the       │
│    so counting it measures nothing.     schema has forced the naive answer     │
│                                         before any code is written.            │
│  early_count should be ZERO for                                              │
│    naive, and a test asserts it.      So: after (NULLABLE) + at_or_before,    │
│    A one-sided error is a BIAS and      confirmed_at kept separately and       │
│    does not cancel across a fleet.      labelled as WHEN WE LOOKED, and        │
│                                         age_max NULL meaning unbounded and     │
│  Ordered pairing, not nearest-match:    specifically not zero.                 │
│    nearest-match rewards over-                                               │
│    reporting. spurious / missed       INSERT OR IGNORE, not OR REPLACE: a     │
│    carry the discrepancy.               re-run must not rewrite what a         │
│                                         previous run observed.                 │
│  WidthBudget  the POLL INTERVAL is a                                          │
│    floor no arithmetic beats. Width                                          │
│    above it is bought with staleness                                         │
│    and is the part that is fixable                                           │
│    without polling harder.                                                   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## The Findings

### One: a later request can describe an earlier origin state

```
request   status       describes origin at
04:00     active       [04:00 .. 04:00]
08:10     restricted   [08:10 .. 08:10]
08:40     active       [04:40 .. 05:40]     <- LATER request, EARLIER origin state
```

Truth is **one** transition at 08:00.

```
naive    reports 2:   active -> restricted,  restricted -> active   <- never happened
interval reports 1:   active -> restricted   (04:40 .. 08:10]
```

**A phantom flap.** A wrong instant is a wrong number; an invented transition pages someone.

### Two: the naive error is a bias, not noise

| strategy | contains | rate | bounded | **spurious** | **late** | **early** | mean width | mean late |
|:--|--:|--:|--:|--:|--:|--:|--:|--:|
| **interval** | **17/17** | **100.0%** | **100.0%** | **0** | **0** | 0 | 4.40h | n/a |
| naive | 0/17 | 0.0% | 0.0% | **2** | **17** | 0 | 0.00h | 2.73h |

Naive's 0% containment is **near-tautological** — a point claim contains an instant only on exact
equality — and is explicitly not the interesting number. The interesting ones are the 2 invented
transitions and the 17-to-0 split, which is arithmetic:

```
confirming age      naive lateness
             0s                  0s
            60s                 60s
          3600s               3600s
         86400s              86400s
```

### Three: requests are not observations

```
requests  observations  echoes  independence
     216            12     204          5.6%
```

Ninety-four percent of those checks added nothing, and every one read as corroboration.

### Four: most of the uncertainty is not the cache's fault

```
polling floor      4.00h    unavoidable
mean width         4.40h
excess over floor  0.40h    bought with staleness
staleness share     9.2%
```

**Ninety-one percent is cadence.** Reporting only the total width would imply all of it is fixable
by fixing caching.

---

## Data Flow

```
1.  status-checker audit
2.  load 8 label tables, 451 readings, 17 known change instants
      1 malformed row              -> reported, and it fails `check`
      1 label conflict             -> "under review" is PENDING in 3 tables, RESTRICTED in a
                                      4th. FAILS `check`: any count spanning them is undefined
      1 unmapped label x10         -> reported as config drift, EXCLUDED from attribution
      2 tables missing a status    -> so a zero there is not read as a finding
3.  derive provenance + age bound per reading from RAW headers at LOAD time
      all three Provenance values occur, so no corpus assertion is vacuous
4.  group by (marketplace, listing) -- 12 groups. Never merged: one region's stale cache
      must not invent a transition in another
5.  per group: collapse echoes -> sort by ORIGIN window -> walk transitions
6.  score both strategies against the known instants, pairing IN SEQUENCE per listing
7.  width budget: 4.40h mean against a 4.00h polling floor -> 9.2% staleness
8.  exit 0: the interval strategy invented nothing
      the naive baseline invented 2, reported as a FINDING rather than a failure
```

---

## Testing

```bash
pytest                          # 137 tests, ~0.3s
pytest --cov=status_checker --cov-report=term-missing
```

| File | Covers |
|:--|:--|
| `conftest.py` | corpus as session fixtures; `reading()` takes an hour offset and an age bound, because every interesting case here is the relationship between when we asked and how old the answer was |
| `test_freshness_labels.py` | all nine header shapes; clock skew; malformed directives; `s-maxage` precedence; a cache exceeding its own lifetime; windows; **all four `Ordering` cases**; generation identity; NFKC and casefold each load-bearing; label conflicts on the normalised key |
| `test_attribute_measure.py` | intervals contain what points miss; **lateness never negative over five ages**; the inversion and the phantom flap; **inversions scoped to one listing**; echoes; interval-valued durations with clamping; unknown labels not manufacturing transitions; every `None` case; SQLite round-trip preserving `NULL` as unbounded; **corpus properties** |
| `integration/test_cli.py` | all seven commands, every exit-code reason, JSON and CSV contents |

The property tests run against `data/`, so editing the corpus and breaking an invariant fails the
build. Two exist to stop the others being vacuous: **every `Provenance` and every `Ordering` value
must occur**. Nothing is mocked, because nothing talks to a network.

---

## Configuration

`SC_`-prefixed environment variables or a `.env` file. No list-typed fields, since Pydantic Settings
parses those as JSON and `SC_X=a,b` would fail with a decode error reading like a bug in the tool.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SC_", env_file=".env", extra="ignore")

    readings: Path = Path("data/readings.jsonl")
    labels: Path = Path("data/labels.json")
    truth: Path = Path("data/truth.jsonl")
    history: Path = Path("data/history.db")
    stale_after_seconds: int = Field(default=900, ge=0)
    poll_interval_seconds: int = Field(default=14400, ge=1)
```

`stale_after_seconds` affects only what gets **flagged** — the interval arithmetic uses the measured
age either way, so raising it hides warnings rather than changing any bound.
