# ADR 002: A later request can describe an earlier origin state, so request order is not evidence

## Status

Accepted

## Context

The project brief asks for history: *"historical status tracking (SQLite — show when status changed)."* The
obvious schema is

```sql
CREATE TABLE status_history (listing_id TEXT, status TEXT, changed_at TIMESTAMP)
```

and it cannot represent what we know. `changed_at` is one column, so it holds one instant, so the
only thing that fits is the poll timestamp. **The schema has forced the naive answer before any code
is written**, and every consumer inherits it: a dashboard, a report, an SLA calculation, all reading
a column whose name asserts more than the data supports.

Attributing a change to the moment we noticed it has two failure modes, and they are not the same
kind of problem.

## The measurement

Two strategies, both implemented and both scored against 17 known change instants:

| strategy | contains truth | rate | bounded rate | **spurious** | **late** | **early** | mean width | mean late |
|:--|--:|--:|--:|--:|--:|--:|--:|--:|
| **interval** | **17 / 17** | **100.0%** | **100.0%** | **0** | **0** | 0 | 4.40h | n/a |
| naive | 0 / 17 | 0.0% | 0.0% | **2** | **17** | 0 | 0.00h | 2.73h |

**Naive containment of 0% is near-tautological and is not the interesting number.** A point claim
contains an instant only on exact equality, so a point estimate essentially never "contains" a
change. Presenting 100% against 0% as a contest would be dishonest. The two numbers that matter are
in bold, and they are different in kind.

### Failure one: the error is a bias, not noise

Naive attribution was late on **all 17** changes and early on **none**. That is not an accident of
this corpus, it is arithmetic:

```
at_or_before = confirmed_at - (minimum age of the confirming reading)
```

so `confirmed_at ≥ at_or_before` always, and the naive timestamp is late or exact and never early.

```
confirming age      naive lateness
             0s                  0s
            60s                 60s
          3600s               3600s
         21600s              21600s
         86400s              86400s
```

A one-sided error **does not cancel across a fleet the way noise does**. A mean time-to-restriction
built on naive timestamps is wrong rather than merely imprecise, and it is wrong in the flattering
direction: everything looks like it happened more recently than it did.

### Failure two: the order of two readings can be provably reversed

This one is qualitative. A fresh read at 08:10 describes origin state at 08:10. A read at 08:40
served from a three-to-four-hour cache describes **04:40–05:40**. Every instant the later request
could be describing falls before the instant the earlier one describes:

```
request   status       describes origin at
04:00     active       [04:00 .. 04:00]
08:10     restricted   [08:10 .. 08:10]
08:40     active       [04:40 .. 05:40]     <- LATER request, EARLIER origin state
```

Truth: one transition, `active -> restricted` at 08:00. What each strategy reports:

```
naive    reports 2 changes:      active -> restricted
                                 restricted -> active     <- never happened
interval reports 1 change:       active -> restricted   (04:40 .. 08:10]
```

**A phantom flap.** The listing never left `restricted`, and naive attribution reports a round trip
out of it and back. A wrong instant is a wrong number; **an invented transition pages someone.** On
the shipped corpus this produces exactly 2 spurious transitions, both from the one inverted listing.

## Decision

**Attribution sorts by the origin window each reading describes, not by request time.**

`Ordering` has four values because the relationship between two readings has four cases:

| value | meaning |
|:--|:--|
| `PROVEN` | the earlier request's window ends before the later one's begins |
| `REVERSED` | the later request provably describes an earlier origin state |
| `OVERLAPPING` | the windows overlap, so origin order is not established at all |
| `UNBOUNDED` | a reading is undated and cannot be placed relative to anything |

A change is reported as an interval. Given the old status held at some instant in `[a, b]` and the
new status at some instant in `[c, d]`, the provable claim is **`change ∈ (a, d]`**.

`inversions()` reports request pairs whose origin order reverses their request order, **as a
first-class finding rather than something the sort quietly fixes**. An inversion means a cache in the
path is serving content older than the poll interval — an infrastructure fact the operator can act
on, and one no downstream arithmetic repairs.

### And durations inherit it

Both endpoints of a state span are intervals, so the duration is one too: the shortest run pairs the
latest possible entry with the earliest possible exit, the longest does the reverse. "Restricted for
8 hours" becomes "restricted for 0 to 12 hours", which is less satisfying and is what the evidence
supports. The naive single number is printed alongside for comparison.

`min_duration` is clamped at zero. A negative bound means the evidence does not establish that the
state persisted at all, so the honest floor is zero rather than a negative duration.

Meridian, a sibling project, computes `time_in_state` as `total += span.duration` — exact point
subtraction. That is the right choice there, where events arrive from a feed with authoritative
timestamps. It is the wrong choice here, where every timestamp is an observation.

### The schema carries the bounds

`changes` has `after` and `at_or_before`, `after` nullable because a lower bound is not always
available, and `confirmed_at` kept separately and labelled as **when we looked**. `readings.age_max`
is nullable, where `NULL` means unbounded and specifically not zero.

### The width budget separates what is fixable from what is not

```
polling floor         4.00h    unavoidable: a change can only be placed between two polls
mean width            4.40h
excess over floor     0.40h    bought with cache staleness
staleness share        9.2%
```

**Ninety-one percent of the uncertainty on this corpus is cadence, not caching.** Reporting only the
total width would imply all of it is a caching problem. Polling harder is the only lever on the
floor; asking the origin not to serve stale is the lever on the rest.

## Validation

- `TestAttributionIsAnIntervalNotAnInstant` — two fresh readings bound a change to the poll
  interval; the interval contains a truth the naive point misses; **lateness is never negative,
  parametrised over five ages**; an undated predecessor leaves the change unlocatable
- `TestTheInversion` — naive invents the flap, interval reports one change in the right direction,
  the inversion is reported, and **inversions are scoped to one listing**
- `TestDurationsAreRanges` — min/max bracket the naive value; clamping at zero; open spans; spans
  that began before monitoring started; unbounded entries
- `TestMeasurement` — unbounded claims cannot inflate the headline; all `None` cases; spurious and
  missed reported rather than absorbed
- `TestHistorySchema` — round trip preserves `NULL` as unbounded rather than zero; `confirmed_at`
  survives as distinct from both bounds; re-running does not rewrite what was observed
- `TestPropertiesOverTheShippedCorpus` — interval contains **all 17**; naive is late on **17** and
  early on **0**; naive invents exactly **2**; exactly **one** inversion corpus-wide; every
  `Ordering` value occurs; and **both strategies agree exactly on the always-fresh listings**
- `setup.sh` and CI re-derive the inversion, the phantom flap, the lateness table and the budget

## Consequences

**Good**

- The reported claim contains the truth on every change in the corpus, and says how wide the claim is
- The phantom flap — the failure that would page someone — does not occur
- An inversion is surfaced as an infrastructure problem rather than silently absorbed
- The width budget tells an operator which part of the uncertainty their polling cadence owns
- The schema cannot express the over-confident answer, so no downstream consumer can inherit it

**Bad**

- An interval is harder to act on than an instant. "Restricted somewhere between Tuesday 04:40 and
  Tuesday 08:10" does not fit in a dashboard cell, and a reader who wants one number will take
  `at_or_before` and be back where they started
- Where readings are undated the interval is unbounded below and the tool reports a change it cannot
  locate. On the corpus that is 2 of 17; on a real fleet with no `Age` headers it would be most of
  them
- `OVERLAPPING` is reported and then attribution proceeds anyway, using the sorted order. The label
  is honest but the behaviour is a guess, and a stricter tool would refuse to report the change at all
- Sorting by window end is a choice, not a derivation. Two readings with identical ends and different
  starts are ordered by start, and nothing measures whether that tiebreak is the right one
- The naive strategy remains in the codebase and in the CLI (`--naive`). Someone will use it
- Scoring pairs reported changes with true ones **in sequence** per listing. If a strategy misses the
  first of three transitions, the remaining two pair against the wrong truths and the direction rate
  looks worse than it is. Nearest-match pairing would fix that and would reward over-reporting, so
  the ordered version is kept and `spurious`/`missed` carry the discrepancy

**Neutral**

- `Change.ordering` describes the origin-sorted pair, so a change derived from an inverted stream
  reports `PROVEN` — correctly, since after sorting the two windows *are* strictly ordered.
  `inversions()` is what surfaces that the request stream was inverted, and the two are separate
  questions
- `audit` fails only when the **interval** strategy invents a transition. The naive baseline invents
  two on every run, and gating on that would make the check fire unconditionally — a check that
  always fires carries no information. The naive count is reported as a finding instead
- A bad known-changes file is checked **before** any conclusion is drawn from the scores, because an
  empty truth set makes every claim look spurious and reporting "the strategy invented 17
  transitions" would blame the code for a broken fixture
