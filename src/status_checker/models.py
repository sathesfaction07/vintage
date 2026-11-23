"""Types for status readings, and for how old a reading actually is.

The central idea: a status reading is an observation about the PAST. A cache can serve a value
that was true an hour ago, and nothing in the value itself says so. An unchanged status and a
stale reading of a changed status are byte-identical in the data.

So a reading does not describe an instant. It describes an INTERVAL of origin time, and every
downstream answer -- when did it change, how long was it restricted -- inherits that interval.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Status(StrEnum):
    """Canonical status, independent of the label any one marketplace prints."""

    ACTIVE = "active"
    RESTRICTED = "restricted"
    PENDING = "pending"
    SUPPRESSED = "suppressed"
    UNKNOWN = "unknown"
    """The raw label is not in the mapping.

    This is not a parse failure to be smoothed over. An unrecognised label is EVIDENCE that the
    platform changed its wording, which is a thing the operator needs to know and fix in the
    config. Resolving it silently -- by a vision model or anything else -- answers the immediate
    question and destroys the signal, after which the mapping rots undetected.
    """


class Provenance(StrEnum):
    """Where a response came from, which determines whether its age can be bounded at all."""

    ORIGIN = "origin"
    """Served by the origin with no cache in the path we can detect. Age is zero."""

    CACHED = "cached"
    """Served by a cache that told us how long it had been sitting there."""

    UNDATED = "undated"
    """No `Age` header, so we cannot confirm whether a cache was in the path.

    Distinct from ORIGIN on purpose. A conforming cache must add `Age`, so its absence is evidence
    of a direct response but not proof of one -- and treating it as proof would silently convert
    'we cannot tell' into 'it is current', which is the exact error this project exists to make
    visible.

    A stated freshness lifetime can still bound the age from above even here; absence of both
    leaves it unbounded, and that is the common case rather than an edge case.
    """


class AgeBound(BaseModel):
    """How old a reading is, in seconds, as an interval.

    `max_seconds is None` means unbounded above: the reading could be arbitrarily old. That is
    the honest reading of a response with no Age and no freshness lifetime, and it is the common
    case rather than an edge case.
    """

    model_config = ConfigDict(frozen=True)

    min_seconds: float = Field(ge=0.0)
    max_seconds: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_seconds is not None and self.max_seconds < self.min_seconds:
            raise ValueError(
                f"max_seconds {self.max_seconds} is below min_seconds {self.min_seconds}"
            )
        return self

    @classmethod
    def exact(cls, seconds: float = 0.0) -> AgeBound:
        return cls(min_seconds=seconds, max_seconds=seconds)

    @classmethod
    def unbounded(cls, min_seconds: float = 0.0) -> AgeBound:
        return cls(min_seconds=min_seconds, max_seconds=None)

    @property
    def is_dated(self) -> bool:
        """True when the age has an upper bound, so the reading can be placed in time at all."""
        return self.max_seconds is not None

    @property
    def is_exact(self) -> bool:
        return self.max_seconds is not None and self.max_seconds == self.min_seconds

    @property
    def width_seconds(self) -> float | None:
        if self.max_seconds is None:
            return None
        return self.max_seconds - self.min_seconds

    def describe(self) -> str:
        if self.is_exact:
            return f"{self.min_seconds:.0f}s"
        if self.max_seconds is None:
            return f"at least {self.min_seconds:.0f}s, no upper bound"
        return f"{self.min_seconds:.0f}-{self.max_seconds:.0f}s"


class Window(BaseModel):
    """The span of origin time a single reading describes.

    A reading taken at `observed_at` whose age is in [lo, hi] describes origin state at some
    instant in [observed_at - hi, observed_at - lo]. `start is None` means unbounded into the
    past, which is what an undated reading gives you: it proves the value existed at some point
    at or before `end`, and says nothing about when.
    """

    model_config = ConfigDict(frozen=True)

    start: datetime | None
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start is not None and self.start > self.end:
            raise ValueError(f"window start {self.start} is after end {self.end}")
        return self

    @property
    def is_bounded(self) -> bool:
        return self.start is not None

    @property
    def is_instant(self) -> bool:
        return self.start is not None and self.start == self.end

    @property
    def width(self) -> timedelta | None:
        if self.start is None:
            return None
        return self.end - self.start

    def precedes(self, other: Window) -> bool:
        """True when every instant in this window is strictly before every instant in `other`.

        This is the only way to establish that one reading describes an EARLIER origin state than
        another. Request order does not establish it: two requests can be minutes apart and still
        both be served from one cache entry, in which case neither is provably older than the
        other and any change between them is unattributable.
        """
        if other.start is None:
            return False
        return self.end < other.start


class Reading(BaseModel):
    """One status observation, with everything needed to date it."""

    model_config = ConfigDict(frozen=True)

    marketplace: str
    listing_id: str
    raw_label: str
    status: Status
    observed_at: datetime
    provenance: Provenance
    age: AgeBound
    generation: str | None = None
    """Identifies the origin response this reading came from -- an ETag, or the Date header.

    Two readings sharing a generation are ONE observation of the origin, however many requests
    were made. Without it we cannot tell whether a repeat reading is confirmation or an echo.
    """

    @property
    def window(self) -> Window:
        end = self.observed_at - timedelta(seconds=self.age.min_seconds)
        if self.age.max_seconds is None:
            return Window(start=None, end=end)
        return Window(start=self.observed_at - timedelta(seconds=self.age.max_seconds), end=end)

    @property
    def is_dated(self) -> bool:
        return self.age.is_dated

    @property
    def label_is_mapped(self) -> bool:
        return self.status is not Status.UNKNOWN


class Ordering(StrEnum):
    """Whether two readings can be placed in order on the origin's clock."""

    PROVEN = "proven"
    """The earlier request's window ends before the later request's window begins."""

    REVERSED = "reversed"
    """The LATER request provably describes an EARLIER origin state.

    Not a theoretical case. A fresh read at 12:00 describes 12:00; a read at 12:30 served from a
    cache one to two hours old describes 10:30-11:30. Every instant the second reading could be
    describing falls before the instant the first one describes, so the order of the two ORIGIN
    states is the reverse of the order of the two requests.

    The consequence is worse than a late timestamp. Attribution that trusts request order reports
    the transition backwards -- active then restricted, where the evidence says restricted then
    active. A wrong instant is a wrong number; a wrong direction is a wrong story.
    """

    OVERLAPPING = "overlapping"
    """The windows overlap, so which origin state came first is not established.

    Request timestamps are ordered and that is not enough: both readings may describe the same
    moment of origin time, seen through caches of different ages.
    """

    UNBOUNDED = "unbounded"
    """At least one reading is undated, so it cannot be placed relative to anything."""


class Change(BaseModel):
    """A status change, located as an interval rather than an instant.

    Given a reading proving the old status held at some instant in [a, b] and a later reading
    proving the new status held at some instant in [c, d], the change happened after some point
    in [a, b] and at or before some point in [c, d]. The provable statement is therefore

        change in (a, d]

    and its width is d - a. The floor on that width is your polling interval: even with two
    perfectly fresh readings you only know the change fell between them.
    """

    model_config = ConfigDict(frozen=True)

    marketplace: str
    listing_id: str
    from_status: Status
    to_status: Status
    after: datetime | None
    """Lower bound, exclusive. None means unbounded: no dated reading established the old value."""

    at_or_before: datetime
    """Upper bound, inclusive."""

    ordering: Ordering
    confirmed_at: datetime
    """When the request that revealed the change was made -- NOT when the change happened.

    Carried so the naive attribution can be compared against the interval rather than assumed
    equal to it.
    """

    @property
    def is_bounded(self) -> bool:
        return self.after is not None

    @property
    def width(self) -> timedelta | None:
        if self.after is None:
            return None
        return self.at_or_before - self.after

    @property
    def naive_lateness(self) -> timedelta:
        """How far past the latest possible change instant the naive timestamp sits.

        Naive attribution records the change at `confirmed_at`. The change provably happened at
        or before `at_or_before`, which equals `confirmed_at` minus the confirming reading's
        minimum age. So this is never negative: the naive timestamp is LATE or exactly right,
        never early.

        That matters more than its size. A bias in one direction does not cancel across many
        changes, so a fleet-wide average built on naive timestamps is wrong rather than noisy.
        """
        return self.confirmed_at - self.at_or_before

    def contains(self, instant: datetime) -> bool:
        if self.after is not None and instant <= self.after:
            return False
        return instant <= self.at_or_before


class StateSpan(BaseModel):
    """A period spent in one status, with a duration that is an interval.

    Both ends are intervals, so the duration is too. The shortest possible run pairs the latest
    possible entry with the earliest possible exit; the longest pairs the earliest entry with the
    latest exit.
    """

    model_config = ConfigDict(frozen=True)

    marketplace: str
    listing_id: str
    status: Status
    entered: Change | None
    exited: Change | None

    @property
    def is_open(self) -> bool:
        return self.exited is None

    @property
    def min_duration(self) -> timedelta | None:
        """Shortest run consistent with the evidence: latest entry to earliest exit.

        Clamped at zero. The bound can go negative when the two changes' intervals overlap, which
        means the evidence does not even establish that the state persisted -- so the honest
        floor is zero, not a negative duration.
        """
        if self.entered is None or self.exited is None:
            return None
        if self.exited.after is None:
            return None
        span = self.exited.after - self.entered.at_or_before
        return max(span, timedelta(0))

    @property
    def max_duration(self) -> timedelta | None:
        """Longest run consistent with the evidence: earliest entry to latest exit.

        None when the entry is unbounded, which makes the run arbitrarily long as far as the
        evidence goes.
        """
        if self.entered is None or self.exited is None:
            return None
        if self.entered.after is None:
            return None
        return self.exited.at_or_before - self.entered.after

    @property
    def naive_duration(self) -> timedelta | None:
        """What a point-timestamp calculation would report: confirmation to confirmation.

        Kept for comparison. It is a single number where the evidence supports a range, and it
        inherits the lateness of both endpoints -- which partly cancels for a duration even
        though it does not cancel for an instant.
        """
        if self.entered is None or self.exited is None:
            return None
        return self.exited.confirmed_at - self.entered.confirmed_at

    def describe_duration(self) -> str:
        if self.exited is None:
            return "open"
        low, high = self.min_duration, self.max_duration
        if low is None or high is None:
            return "unbounded"
        return f"{_hours(low)}-{_hours(high)}h"


def _hours(span: timedelta) -> str:
    return f"{span.total_seconds() / 3600:.1f}"


class ObservationCount(BaseModel):
    """Requests are not observations.

    Two requests answered from one cache entry tell you what the origin said once. Reporting a
    status as "confirmed by 3 checks" when all three shared a generation overstates the evidence
    threefold, and it overstates it in the direction of confidence.
    """

    model_config = ConfigDict(frozen=True)

    requests: int = Field(ge=0)
    distinct_generations: int = Field(ge=0)
    undated_readings: int = Field(ge=0)
    """Readings carrying no generation marker. Each MIGHT be independent and none is proven so."""

    @property
    def echoes(self) -> int:
        """Requests that told us nothing new because they repeated a generation already seen."""
        return self.requests - self.distinct_generations - self.undated_readings

    @property
    def independence_ratio(self) -> float | None:
        """Distinct generations per request. None when there were no requests.

        Not clamped to look flattering: a value of 0.33 means two of every three checks were
        echoes, and that is the number worth seeing.
        """
        if self.requests == 0:
            return None
        return self.distinct_generations / self.requests


class LabelConflict(BaseModel):
    """One raw label mapping to different canonical statuses in different marketplaces.

    A static defect in the config, not a runtime event. It matters because every fleet-wide count
    silently assumes the mapping is a function of the label alone. Where it is not, "how many are
    restricted" has no single answer and no error is raised.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    mappings: dict[str, Status]

    @property
    def statuses(self) -> set[Status]:
        return set(self.mappings.values())


def seconds(span: timedelta) -> float:
    return span.total_seconds()


def clamp_positive(value: float) -> float:
    return value if value > 0.0 and math.isfinite(value) else 0.0
