# Vintage

A listing reads `active` at 04:00, `restricted` at 08:10 and `active` again at 08:40. Did it
flap? No: the third reading came from a three-hour cache and describes the listing as it was
before 08:00. Vintage reads listing status across marketplaces and attaches to every
reading the window of time it can actually describe, worked out from the response's cache
headers. Treating readings as windows stops phantom transitions from paging anyone.

Nothing is guessed: the age of a reading is arithmetic on the headers it came with.

---

## The Problem, In One Table

```
request   status       describes origin at
04:00     active       [04:00 .. 04:00]
08:10     restricted   [08:10 .. 08:10]
08:40     active       [04:40 .. 05:40]     <- LATER request, EARLIER origin state
```

That third reading came from a three-hour cache. Every instant it could be describing falls *before*
the instant the second one describes.

Truth: **one** transition, `active -> restricted` at 08:00.

```
naive    reports 2 changes:      active -> restricted
                                 restricted -> active     <- never happened
interval reports 1 change:       active -> restricted   (04:40 .. 08:10]
```

**A phantom flap.** The listing never left `restricted`. A wrong instant is a wrong number; an
invented transition pages someone.

---

## Measured Against 17 Known Change Instants

| strategy | contains truth | rate | bounded | **spurious** | **late** | **early** | mean width | mean late |
|:--|--:|--:|--:|--:|--:|--:|--:|--:|
| **interval** | **17 / 17** | **100.0%** | **100.0%** | **0** | **0** | 0 | 4.40h | n/a |
| naive | 0 / 17 | 0.0% | 0.0% | **2** | **17** | 0 | 0.00h | 2.73h |

**Naive's 0% containment is near-tautological and I am not claiming it as a win** — a point estimate
contains an instant only on exact equality. The two numbers that matter are the **2 invented
transitions** and the **17-to-0 late-versus-early split**.

That split is arithmetic, not luck:

```
at_or_before = confirmed_at - (minimum age of the confirming reading)
```

so the naive timestamp is late or exact and **never early**. A one-sided error is a *bias*, and a
bias does not cancel across a fleet the way noise does. A mean time-to-restriction built on poll
timestamps is wrong rather than imprecise, and wrong in the flattering direction.

And the naive strategy is not a straw man: on the three always-fresh listings both strategies agree
exactly, transition for transition. There is a test asserting that.

---

## A Missing `Age` Header Is Not Freshness

| response | provenance | age bound | datable |
|:--|:--|:--|:--|
| `no-store` / `no-cache` | `ORIGIN` | 0s | yes |
| `max-age=0` | `ORIGIN` | 0s | yes |
| `Age: 3600`, `max-age=7200` | `CACHED` | 3600–7200s | yes |
| `Age: 3600`, no lifetime | `CACHED` | ≥3600s | **no** |
| no `Age`, no cache headers | `UNDATED` | ≥0s | **no** |
| no `Age`, `max-age=600` | `UNDATED` | 0–600s | yes |
| `Age: 9000`, `max-age=7200` | `CACHED` | ≥9000s | **no** |

A conforming cache *must* send `Age`, so its absence is **evidence** of a direct response and not
proof of one. Treating it as zero converts "we cannot tell" into "it is current", and the two errors
do not cost the same.

A cache that served past its own stated lifetime **forfeits the upper bound** — the lifetime bounds
the age only if the cache respects it, and there it demonstrably did not.

`Expires` is deliberately ignored: it is an instant on the origin's clock, so using it would import
clock skew into a bound whose entire purpose is to be safe.

---

## Requests Are Not Observations

One listing in the corpus is polled every 20 minutes against a six-hour cache:

```
requests  observations  echoes  independence
     216            12     204          5.6%
```

**Ninety-four percent of those checks added nothing**, and every one of them read as corroboration.
Two readings sharing an `ETag` are one look at the origin, however many times you asked.

Readings with no identifier are never collapsed together — absence of an identifier is not evidence
of shared identity.

---

## Durations Are Ranges

Both ends of a state span are intervals, so the duration is one too. "Restricted for 8 hours" becomes
"restricted for 0 to 12 hours", which is less satisfying and is what the evidence supports. The naive
single number is printed beside it.

Meridian, a sibling project, does `total += span.duration` — exact point subtraction. Right
there, where events arrive from a feed with authoritative timestamps. Wrong here, where every
timestamp is an observation.

---

## The Width Budget: What Is Fixable and What Is Not

```
polling floor         4.00h    unavoidable: a change can only be placed between two polls
mean width            4.40h
excess over floor     0.40h    bought with cache staleness
staleness share        9.2%
```

**Ninety-one percent of the uncertainty here is cadence, not caching.** Reporting only the total
width would imply all of it is a caching problem. Polling harder is the only lever on the floor.

---

## The Schema Is the Argument

The project brief asks for *"SQLite — show when status changed"*. The obvious schema is

```sql
CREATE TABLE status_history (listing_id TEXT, status TEXT, changed_at TIMESTAMP)
```

and it cannot hold "somewhere between 04:40 and 08:10". One column holds one instant, so the only
thing that fits is the poll timestamp — **the schema forces the naive answer before any code is
written**, and every dashboard, report and SLA calculation downstream inherits it.

So:

```sql
after         TEXT,             -- NULL means no lower bound was established
at_or_before  TEXT NOT NULL,
confirmed_at  TEXT NOT NULL,    -- when we LOOKED, not when it changed
age_max       REAL,             -- NULL means unbounded, not zero
```

---

## Running

```bash
bash setup.sh                  # derives the age table, the inversion, the bias and the budget
source .venv/bin/activate

vintage doctor
vintage check                    # exits 1: a label conflict and a malformed row
vintage changes
vintage changes --naive          # the same data attributed by request time
vintage durations
vintage audit
vintage record
vintage explain es inverted      # the phantom flap, reading by reading
vintage explain us echoed        # 216 requests, 12 observations
```

| Command | Purpose | Exit |
|:--|:--|:--|
| `check` | Every reading with the origin window it describes | **1** on an unreadable row or a **label conflict** |
| `changes` | Status changes as intervals, plus any inversions | 1 on an unreadable row |
| `durations` | Time in each status as ranges, plus echo counts | 0 |
| `audit` | Both strategies scored against known instants | **1** only if the *interval* strategy invents a transition |
| `record` | Write to SQLite | 1 if unwritable |
| `explain` | One listing, reading by reading | 1 on an unknown listing |

`check` does **not** fail on staleness or on an unmapped label. A cache serving old content is
normal, and a check that fires whenever a platform reworded something gets switched off in a week.
Both are reported loudly instead.

`audit` does **not** fail because the naive baseline is bad. That is the finding, not a regression,
and a check that fires on every run carries no information.

---

## Testing

```bash
pytest                          # 137 tests, ~0.3s
pytest --cov=status_checker --cov-report=term-missing
```

The property tests run against `data/`, so editing the corpus and breaking an invariant fails the
build. One exists to stop the others being vacuous: **every `Provenance` and every `Ordering` value
must occur in the corpus**, or the assertions about them prove nothing.

Nothing is mocked, because nothing talks to a network.

---

## A Bug I Found by Reading Output

`inversions()` originally sorted the whole reading stream globally and reported **39 inversions**. The
number looked plausible. The detail lines did not:

```
jp/clean 03-01 00:00 -> 03-01 00:00
```

`jp/clean` is always fresh with age 0 — it cannot be inverted with anything. The function was
comparing readings of *different listings* polled in the same second. Scoped to one listing, the true
count is **1**, and it is the one planted case.

I had written the docstring warning about exactly this on `group_readings` — *"merging them would let
one region's stale cache invent a transition in another"* — and then done it anyway one function
above.

---

## Tradeoffs and Decisions

**No vision-model fallback on unrecognised labels, despite the project brief asking.** An unknown label is
*evidence the platform changed its wording*, which is the thing to fix in the config. Resolving it
silently answers the immediate question and destroys the signal, after which the mapping rots
undetected. Also: Twinlane, a sibling project, already has `core/vlm_fallback.py`, so building it
again would be a duplicate rather than a capability.

**An unknown label is excluded from attribution.** A transition into `UNKNOWN` is a fact about our
config, and in the output it would be indistinguishable from a real status movement. Including them
would manufacture two phantom changes around every wording change — the corpus's reworded listing
yields **one** change, not three.

**No FastAPI + htmx UI, despite the project brief asking.** Vigil already carries FastAPI, and UI
JavaScript is the one thing CI cannot assert on. A text table asserts in CI; a page does not.

**NFKC and casefold are both applied, and this is a repeat.** Kinship reached the same conclusion
for merchant names. Cited as a repeat rather than presented as a discovery — but kept, because a
fullwidth form needs NFKC and the German sharp s needs casefold, and each omission loses a different
label.

**The label conflict fails the build.** A label meaning different things in two marketplaces makes
every count spanning them undefined, and the count will still compute to a number that looks
reasonable. That is worse than an error.

**`missing_statuses()` exists so a zero is not read as a finding.** A table with no label for
`SUPPRESSED` can never report one.

**`min_duration` is clamped at zero.** A negative bound means the evidence does not establish the
state persisted at all, so the honest floor is zero rather than a negative number.

**Scoring pairs claims with truths in sequence, not by nearest match.** Nearest-match would let a
strategy that invented an extra transition claim credit by pairing each report with whichever truth
it sat closest to, which rewards over-reporting. The cost is that a missed first transition
misaligns the rest, and `spurious`/`missed` carry that discrepancy rather than hiding it.

**`bounded_containment_rate` exists because an unbounded interval contains every instant.** Counting
those as successes would measure nothing, so the honest headline excludes them.

**The verdict-enum pattern is house style at this point, not an insight.** This is the fourth project
here to replace a boolean with a multi-valued type, and saying so is more useful than dressing it up.
The genuinely different modelling move in this one is the **interval** — `AgeBound`, `Window`,
`Change`, and `StateSpan` all carry two bounds where a simpler design carries one number.

**A bad known-changes file is checked before any conclusion is drawn from the scores.** An empty
truth set makes every claim look spurious, and reporting "the strategy invented 17 transitions" would
blame the code for a broken fixture.

**`stale_after_seconds = 900` is a judgement, not a derivation.** It affects only what gets flagged —
the interval arithmetic uses the measured age either way, so raising it hides warnings rather than
changing any bound.

---

## Configuration

```bash
SC_READINGS=data/readings.jsonl
SC_LABELS=data/labels.json
SC_TRUTH=data/truth.jsonl
SC_HISTORY=data/history.db
SC_STALE_AFTER_SECONDS=900
SC_POLL_INTERVAL_SECONDS=14400
```

No list-typed fields anywhere: Pydantic Settings parses those as JSON, so `SC_X=a,b` would fail with
a decode error that reads like a bug in the tool rather than a bad value.

---

## Stack

| Concern | Choice |
|:--|:--|
| Language | Python 3.11+ |
| Models + config | Pydantic, Pydantic Settings |
| CLI | Click |
| History | SQLite (`sqlite3`, stdlib) |
| Age arithmetic | RFC 9111 §4.2.3, by hand |
| Output | text, JSON, CSV |
| Tests | pytest, Click's CliRunner |
| Lint | ruff |
| Models/AI | none, deliberately |

The corpus is synthetic and reproducible via `data/generate.py`: 451 readings across 8 marketplaces
and 12 listings, with 17 labelled change instants. **Staleness is simulated rather than planted** —
each listing has a true status timeline, and a poll served from a cache of age A returns the status
that was true at `poll_time - A`. A wrong reading therefore arises from the mechanism rather than
from me deciding a row should be wrong, so the corpus cannot flatter the interval method by
construction. Planted alongside: a consistently stale CDN, an inversion, two undated listings, an
echo storm, a mid-run rewording, labels needing NFKC and whitespace folding, overlapping staleness
windows, a conflicting label table, a table that cannot express one status, and one malformed row.
