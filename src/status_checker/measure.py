"""Scoring the two attribution strategies against known change instants.

The question is not "how close is the reported timestamp" but "does the reported claim CONTAIN the
truth". A point estimate two hours late is simply wrong. An interval two hours wide that contains
the true instant is correct and says so.

So the headline metric is containment, and width is reported beside it -- because an interval wide
enough to contain everything is trivially correct and useless, and only the pair of numbers
distinguishes a good bound from a vacuous one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from statistics import fmean

from pydantic import BaseModel, ConfigDict

from .models import Change, Ordering, Status


class TruthChange(BaseModel):
    """A change instant known independently of any reading. Synthetic corpus only."""

    model_config = ConfigDict(frozen=True)

    marketplace: str
    listing_id: str
    at: datetime
    from_status: Status
    to_status: Status


class AttributionScore(BaseModel):
    """How well one strategy located the changes it reported."""

    model_config = ConfigDict(frozen=True)

    strategy: str
    true_changes: int
    reported: int
    matched: int
    """Reported changes paired with a true change on the same listing, in sequence."""

    containing: int
    """Matched changes whose claim contains the true instant. The headline number."""

    direction_correct: int
    """Matched changes whose from/to pair matches the truth.

    Separate from containment because they fail independently: a change can be located in the
    right window with the wrong direction (a cache inversion) or the right direction in the wrong
    window (staleness).
    """

    bounded: int
    unbounded: int
    """Claims with no lower bound. Trivially containing, and reported separately so they cannot
    inflate the headline: an unbounded interval contains every instant and proves nothing."""

    late_count: int
    early_count: int
    """Claims falling entirely after / before the true instant.

    `early_count` should be zero for naive attribution, and a test asserts it. A one-sided error is
    a bias, and a bias does not average out over a fleet the way noise does.
    """

    reversed_orderings: int
    overlapping_orderings: int

    lateness_hours: list[float]
    widths_hours: list[float]

    @property
    def containment_rate(self) -> float | None:
        """Fraction of matched changes whose claim contains the truth.

        None when nothing matched: a strategy that reported no changes has not earned a 0% and has
        not earned a 100%, and either number would be read as a measurement.
        """
        if self.matched == 0:
            return None
        return self.containing / self.matched

    @property
    def bounded_containment_rate(self) -> float | None:
        """Containment counting only claims that actually bound the instant from both sides.

        The honest version of the headline. An unbounded claim contains the truth by construction,
        so including it measures nothing.
        """
        if self.bounded == 0:
            return None
        return (self.containing - self.unbounded) / self.bounded

    @property
    def direction_rate(self) -> float | None:
        if self.matched == 0:
            return None
        return self.direction_correct / self.matched

    @property
    def mean_width_hours(self) -> float | None:
        return fmean(self.widths_hours) if self.widths_hours else None

    @property
    def mean_lateness_hours(self) -> float | None:
        return fmean(self.lateness_hours) if self.lateness_hours else None

    @property
    def max_lateness_hours(self) -> float | None:
        return max(self.lateness_hours) if self.lateness_hours else None

    @property
    def spurious(self) -> int:
        """Reported changes with no true counterpart."""
        return max(0, self.reported - self.matched)

    @property
    def missed(self) -> int:
        return max(0, self.true_changes - self.matched)


def _key(item: TruthChange | Change) -> tuple[str, str]:
    return (item.marketplace, item.listing_id)


def score(
    reported: Sequence[Change], truth: Sequence[TruthChange], *, strategy: str
) -> AttributionScore:
    """Pair reported changes with true ones per listing, in order, and score the pairs.

    Ordered pairing rather than nearest-match: both sequences are the transitions of one listing, so
    the nth reported change corresponds to the nth real one. Nearest-match would let a strategy that
    invented an extra transition claim credit by pairing each report with whichever truth it happened
    to sit closest to, which rewards over-reporting.

    Where the counts differ the surplus is `spurious` and the shortfall is `missed`, both reported
    rather than absorbed into the rate.
    """
    truth_by_listing: dict[tuple[str, str], list[TruthChange]] = {}
    for item in sorted(truth, key=lambda entry: entry.at):
        truth_by_listing.setdefault(_key(item), []).append(item)

    reported_by_listing: dict[tuple[str, str], list[Change]] = {}
    for item in reported:
        reported_by_listing.setdefault(_key(item), []).append(item)

    matched = containing = direction_correct = 0
    bounded = unbounded = late = early = 0
    reversed_count = overlapping = 0
    lateness: list[float] = []
    widths: list[float] = []

    for listing, actual in truth_by_listing.items():
        claims = reported_by_listing.get(listing, [])
        for claim, real in zip(claims, actual, strict=False):
            matched += 1
            if claim.is_bounded:
                bounded += 1
                width = claim.width
                if width is not None:
                    widths.append(width.total_seconds() / 3600)
            else:
                unbounded += 1
            if claim.contains(real.at):
                containing += 1
            elif claim.at_or_before < real.at:
                early += 1
            else:
                late += 1
                lateness.append((claim.at_or_before - real.at).total_seconds() / 3600)
            if claim.from_status == real.from_status and claim.to_status == real.to_status:
                direction_correct += 1
            if claim.ordering is Ordering.REVERSED:
                reversed_count += 1
            elif claim.ordering is Ordering.OVERLAPPING:
                overlapping += 1

    return AttributionScore(
        strategy=strategy,
        true_changes=len(truth),
        reported=len(reported),
        matched=matched,
        containing=containing,
        direction_correct=direction_correct,
        bounded=bounded,
        unbounded=unbounded,
        late_count=late,
        early_count=early,
        reversed_orderings=reversed_count,
        overlapping_orderings=overlapping,
        lateness_hours=lateness,
        widths_hours=widths,
    )


class WidthBudget(BaseModel):
    """How much of an attribution interval is unavoidable and how much is staleness.

    Even with perfectly fresh readings a change can only be placed between two polls, so the poll
    interval is a floor no arithmetic can beat. Width above that floor is bought with cache
    staleness, and unlike the floor it can be reduced without polling harder -- by asking the origin
    not to serve stale, or by reading the `Age` header and polling again when it is high.
    """

    model_config = ConfigDict(frozen=True)

    poll_interval_hours: float
    mean_width_hours: float | None

    @property
    def excess_hours(self) -> float | None:
        """Width beyond the polling floor. Negative values are clamped to zero.

        A negative excess would mean the interval is narrower than the poll interval, which happens
        when a change is caught by two readings closer together than the nominal cadence -- not an
        error, just a tighter bound than the budget assumed.
        """
        if self.mean_width_hours is None:
            return None
        return max(0.0, self.mean_width_hours - self.poll_interval_hours)

    @property
    def staleness_share(self) -> float | None:
        """Fraction of the mean interval width attributable to staleness rather than cadence."""
        if self.mean_width_hours is None or self.mean_width_hours == 0:
            return None
        excess = self.excess_hours
        return None if excess is None else excess / self.mean_width_hours


def width_budget(score_result: AttributionScore, poll_interval: timedelta) -> WidthBudget:
    return WidthBudget(
        poll_interval_hours=poll_interval.total_seconds() / 3600,
        mean_width_hours=score_result.mean_width_hours,
    )


def compare(
    interval_changes: Sequence[Change],
    naive_change_list: Sequence[Change],
    truth: Sequence[TruthChange],
) -> dict[str, AttributionScore]:
    return {
        "interval": score(interval_changes, truth, strategy="interval"),
        "naive": score(naive_change_list, truth, strategy="naive"),
    }
