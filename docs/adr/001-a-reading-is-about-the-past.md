# ADR 001: A reading is an observation about the past, and its age is a bound rather than a number

## Status

Accepted

## Context

The brief names its own worst input:

> *"Platform returns stale cached status, status field renamed in UI update, marketplace-specific
> status labels."*

The first of those is not a parsing problem. A status served from a cache is a true statement about
the origin *at some earlier moment*, and nothing in the value says which moment. So:

- An unchanged status and a stale reading of a changed status are **byte-identical in the data**.
- Two consecutive identical readings feel like confirmation. If both came from one cache entry they
  are **one observation**, and the repetition is evidence of nothing.
- A history table recording *when we looked* will be read as recording *when it changed*.

A caching proxy computes an age too, but for a different purpose: it wants one number so it can
decide whether to serve. We are not deciding whether to serve. We are deciding what a value proves,
and for that the honest answer is frequently a range.

## Decision

**`AgeBound` is an interval, `Window` is the span of origin time a reading describes, and
`max_seconds is None` means unbounded above.**

Age is derived per RFC 9111 §4.2.3 with two deliberate departures:

| header state | provenance | age bound |
|:--|:--|:--|
| `no-store` or `no-cache` | `ORIGIN` | exactly 0 |
| `max-age=0`, no `Age` | `ORIGIN` | exactly 0 |
| `Age: 3600`, `max-age=7200` | `CACHED` | 3600–7200s |
| `Age: 3600`, no lifetime | `CACHED` | ≥3600s, **unbounded** |
| no `Age`, no cache headers | `UNDATED` | ≥0s, **unbounded** |
| no `Age`, `max-age=600` | `UNDATED` | 0–600s |
| `Age: 9000`, `max-age=7200` | `CACHED` | ≥9000s, **unbounded** |

**A missing `Age` header is `UNDATED`, not fresh.** A conforming cache must add `Age`, so its
absence is *evidence* of a direct response and not proof of one. Treating it as zero silently
converts "we cannot tell" into "it is current", and the two errors do not cost the same: calling a
stale reading fresh reports a status that is no longer true, while calling a fresh reading undated
only widens an interval.

**A cache that served past its own stated lifetime forfeits the upper bound.** The lifetime bounds
the age only if the cache respects it, and here it demonstrably did not. Keeping it would produce a
bound narrower than the evidence supports.

`Expires` is deliberately not consulted: it is an instant on the origin's clock, so using it would
import clock skew into a bound whose entire purpose is to be safe. A `Date` in the future is treated
as skew and floored at zero rather than read as a response from the future.

### Requests are not observations

`generation` identifies which origin response a reading came from — an `ETag`, falling back to
`Date`, falling back to `Last-Modified`. Two readings sharing one are **one observation**.

On the shipped corpus one listing is polled every 20 minutes against a six-hour cache: **216
requests, 12 distinct generations, 204 echoes, 5.6% independence.** Ninety-four percent of the
checks added nothing, and each of them read as corroboration.

`generation is None` must never be treated as a shared identity. Two unidentifiable readings are not
thereby the same reading, so they are never collapsed together.

### Staleness is claimed only when provable

`stale_by` uses the *lower* bound, so it fires only when staleness is certain. An undated reading is
not reported stale — it is not provably stale and not provably fresh, and conflating the two would
place an unfalsifiable claim beside a demonstrable one.

### The label mapping is checked statically

Localised labels resolve through a per-marketplace table, normalised with NFKC **and** casefold.
Both are required and neither substitutes for the other: a fullwidth form needs NFKC, the German
sharp s needs casefold since `lower()` leaves it alone. This is the same conclusion CaseSearch
reached for merchant names — **a repeat, cited as one**, repeated because the failure is real.

The static check that matters is `conflicts()`: a label mapping to *different* canonical statuses in
different marketplaces. The shipped corpus has one — `"under review"` is `PENDING` in three tables
and `RESTRICTED` in a fourth. Each table is individually plausible. **Any fleet-wide count spanning
them is undefined, and it will still compute to a number that looks entirely reasonable.** That
fails the run.

`missing_statuses()` reports tables that cannot express a status at all, so a zero there is read as
"this table cannot say" rather than as a finding.

### An unknown label is reported, not resolved

The brief asks for a vision-model fallback on unrecognised status text. **Not built, and the reason
is not cost.** An unrecognised label is *evidence that the platform changed its wording*, which is
the thing the operator needs to fix in the config. Resolving it silently answers the immediate
question and destroys the signal, after which the mapping rots undetected and nothing says so.

So `Status.UNKNOWN` is a first-class value, unmapped labels are reported with occurrence counts —
each one a line to add to the config — and they are **excluded from attribution**, because a
transition into `UNKNOWN` is a fact about our configuration and in the output would be
indistinguishable from a real status movement. On the corpus, one listing is reworded mid-run; it
yields **one** change rather than three.

## Validation

- `TestAgeIsABoundNotANumber` — all nine header shapes above, plus clock skew, malformed
  directives, round-trip delay, and `s-maxage` precedence
- `TestStalenessIsProvableOrItIsNotClaimed` — undated is not reported stale
- `TestWindow` / `TestOrderingHasFourCasesAndAllOfThemHappen` — instants, spans, unbounded windows
- `TestEchoesAreNotCorroboration` — nine requests one generation; unidentified never collapsed;
  an echo run cannot pad the evidence for a change
- `TestLabelNormalisation` / `TestLabelConflictsAreStaticDefects` — NFKC and casefold each load-
  bearing, conflicts on the normalised key, tables that cannot express a status
- `TestPropertiesOverTheShippedCorpus::test_every_provenance_occurs` — so nothing above is vacuous
- `setup.sh` and CI re-derive the whole table on a fresh clone

## Consequences

**Good**

- The common case — a response with no cache headers at all — reports as undatable rather than as
  current, which is the honest answer and was the whole point
- Echo counting turns "checked three times" into a number that reflects the evidence
- A config defect that would corrupt every aggregate is caught statically and fails the build
- Wording drift produces an actionable list instead of a silent resolution

**Bad**

- Most real responses land in `UNDATED`, so most intervals are unbounded below and the tool's answer
  is often "the change is detectable and not locatable". That is correct and unsatisfying, and no
  amount of arithmetic improves it — only the origin sending `Age` does
- `Provenance.UNDATED` conflates "no cache involved and no headers" with "a non-conforming cache".
  Nothing distinguishes them, and the name is about what we can conclude rather than what happened
- `MIN` age from `Date` assumes the origin clock is roughly right. Badly skewed clocks produce a
  floor that is too high, widening every interval
- `stale_after_seconds = 900` is a judgement, not a derivation. It affects only what gets flagged —
  the arithmetic uses the measured age either way — but a reader could mistake it for a threshold
  with meaning
- Refusing the VLM fallback means an unrecognised label yields no status at all until someone edits
  a config file. For an operator who wants an answer now, that is worse

**Neutral**

- `Status.UNKNOWN` covers both an unrecognised label and an unrecognised marketplace. `explain`
  distinguishes them; the status does not, because a caller acting on it must treat both alike
- `LabelMap.from_file` returns problems rather than raising, so one bad entry does not cost the
  whole table — and mapping a label *to* `unknown` is itself flagged, since that is what an absent
  entry already means
