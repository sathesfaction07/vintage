"""Attribution, echoes, durations, measurement, rendering, history, and corpus properties."""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from conftest import at, reading
from status_checker.attribute import (
    changes,
    collapse_echoes,
    inversions,
    naive_changes,
    observation_count,
    spans,
    widest_interval,
)
from status_checker.history import History
from status_checker.measure import TruthChange, compare, score, width_budget
from status_checker.models import ObservationCount, Ordering, Provenance, Status
from status_checker.render import (
    changes_csv,
    changes_json,
    hours,
    instant,
    pct,
    render_changes,
    render_comparison,
    render_conflicts,
    render_observations,
    render_readings,
    render_spans,
    span_text,
)


def truth_at(hours_offset: float, from_status: Status, to_status: Status) -> TruthChange:
    return TruthChange(
        marketplace="de", listing_id="L1", at=at(hours_offset),
        from_status=from_status, to_status=to_status,
    )


class TestAttributionIsAnIntervalNotAnInstant:
    def test_two_fresh_readings_bound_the_change_to_the_poll_interval(self):
        found = changes([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ])
        assert len(found) == 1
        assert found[0].after == at(0) and found[0].at_or_before == at(4)
        assert found[0].width == timedelta(hours=4)
        assert found[0].ordering is Ordering.PROVEN

    def test_the_interval_contains_the_true_instant_and_naive_does_not(self):
        """Ground truth at 05:30, polled at 04:00 fresh and 08:00 from a 2-3h cache."""
        readings = [
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.ACTIVE, generation="b"),
            reading(hours=8, status=Status.RESTRICTED, age_min=7200, age_max=10800, generation="c"),
        ]
        real = at(5.5)
        interval = changes(readings)[0]
        naive = naive_changes(readings)[0]
        assert interval.contains(real)
        assert not naive.contains(real)
        assert naive.confirmed_at == at(8)

    def test_naive_lateness_is_never_negative(self):
        """The theorem this project rests on: `at_or_before` is `confirmed_at` minus the confirming
        reading's minimum age, so the naive timestamp is late or exact, never early."""
        for age_min in (0, 60, 3600, 86400):
            found = changes([
                reading(hours=0, status=Status.ACTIVE, generation="a"),
                reading(hours=48, status=Status.RESTRICTED, age_min=age_min,
                        age_max=age_min + 600, generation="b"),
            ])
            assert found[0].naive_lateness >= timedelta(0)
            assert found[0].naive_lateness.total_seconds() == age_min

    def test_an_undated_previous_reading_leaves_the_change_unlocatable(self):
        found = changes([
            reading(hours=0, status=Status.ACTIVE, age_max=None, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ])
        assert found[0].after is None
        assert not found[0].is_bounded
        assert found[0].width is None
        # Detectable but not locatable: it contains every instant, which proves nothing.
        assert found[0].contains(at(-9999))

    def test_no_change_means_no_report(self):
        assert changes([reading(hours=0, generation="a"), reading(hours=4, generation="b")]) == []

    def test_a_single_reading_cannot_establish_a_change(self):
        assert changes([reading(hours=0, generation="a")]) == []
        assert changes([]) == []


class TestTheInversion:
    """The finding that is qualitative rather than quantitative."""

    def _stream(self):
        # Truth: active -> restricted at 08:00. A fresh read at 08:10 sees the new value; a read at
        # 08:40 served from a 3-4h cache sees the OLD value, because it describes 04:40-05:40.
        return [
            reading(hours=4, status=Status.ACTIVE, generation="a"),
            reading(hours=8 + 1 / 6, status=Status.RESTRICTED, generation="b"),
            reading(hours=8 + 2 / 3, status=Status.ACTIVE, age_min=10800, age_max=14400,
                    generation="c"),
        ]

    def test_naive_invents_a_phantom_flap(self):
        found = naive_changes(self._stream())
        assert len(found) == 2
        assert (found[0].from_status, found[0].to_status) == (Status.ACTIVE, Status.RESTRICTED)
        assert (found[1].from_status, found[1].to_status) == (Status.RESTRICTED, Status.ACTIVE)

    def test_the_interval_strategy_reports_one_change_in_the_right_direction(self):
        found = changes(self._stream())
        assert len(found) == 1
        assert (found[0].from_status, found[0].to_status) == (Status.ACTIVE, Status.RESTRICTED)
        assert found[0].contains(at(8))

    def test_the_inversion_is_reported_as_an_infrastructure_fact(self):
        found = inversions(self._stream())
        assert len(found) == 1
        earlier, later = found[0]
        assert earlier.observed_at < later.observed_at
        assert later.window.precedes(earlier.window)

    def test_inversions_are_scoped_to_one_listing(self):
        """The bug this test exists to prevent: comparing readings of DIFFERENT listings polled at
        the same instant manufactures inversions out of nothing. An earlier version reported 39 on
        the shipped corpus where the true count is 1."""
        cross = [
            reading(hours=0, listing_id="A", generation="a"),
            reading(hours=0.5, listing_id="B", age_min=10800, age_max=14400, generation="b"),
        ]
        assert inversions(cross) == []

    def test_an_always_fresh_listing_cannot_be_inverted(self):
        fresh = [reading(hours=index * 4, generation=f"g{index}") for index in range(6)]
        assert inversions(fresh) == []


class TestEchoesAreNotCorroboration:
    def test_four_requests_one_generation_is_one_observation(self):
        readings = [reading(hours=index, age_min=100, age_max=200, generation="same")
                    for index in range(4)]
        count = observation_count(readings)
        assert count.requests == 4
        assert count.distinct_generations == 1
        assert count.echoes == 3
        assert count.independence_ratio == 0.25
        assert len(collapse_echoes(readings)) == 1

    def test_the_tightest_window_survives_a_collapse(self):
        loose = reading(hours=0, age_min=0, age_max=7200, generation="same")
        tight = reading(hours=1, age_min=0, age_max=60, generation="same")
        kept = collapse_echoes([loose, tight])
        assert len(kept) == 1 and kept[0].age.max_seconds == 60

    def test_unidentified_readings_are_never_collapsed_together(self):
        """Absence of an identifier is not evidence of shared identity."""
        anonymous = [reading(hours=index, generation=None) for index in range(3)]
        assert len(collapse_echoes(anonymous)) == 3
        count = observation_count(anonymous)
        assert count.distinct_generations == 0
        assert count.undated_readings == 3

    def test_independence_ratio_is_none_with_no_requests(self):
        empty = ObservationCount(requests=0, distinct_generations=0, undated_readings=0)
        assert empty.independence_ratio is None

    def test_an_echo_run_cannot_pad_the_evidence_for_a_change(self):
        readings = [
            *[reading(hours=index, status=Status.ACTIVE, age_min=60, age_max=120,
                      generation="old") for index in range(5)],
            reading(hours=8, status=Status.RESTRICTED, age_min=60, age_max=120, generation="new"),
        ]
        assert len(changes(readings)) == 1


class TestDurationsAreRanges:
    def test_both_endpoints_are_intervals_so_the_duration_is_too(self):
        readings = [
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
            reading(hours=12, status=Status.ACTIVE, generation="c"),
        ]
        built = spans(readings)
        restricted = next(item for item in built if item.status is Status.RESTRICTED)
        # entered in (0, 4], exited in (4, 12] -> duration in (0, 12)
        assert restricted.min_duration == timedelta(0)
        assert restricted.max_duration == timedelta(hours=12)
        assert restricted.naive_duration == timedelta(hours=8)
        assert restricted.min_duration <= restricted.naive_duration <= restricted.max_duration

    def test_min_duration_is_clamped_at_zero(self):
        """A negative bound means the evidence does not establish the state persisted, so the
        honest floor is zero rather than a negative duration."""
        readings = [
            reading(hours=0, status=Status.ACTIVE, age_min=0, age_max=36000, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, age_min=0, age_max=36000, generation="b"),
            reading(hours=5, status=Status.ACTIVE, age_min=0, age_max=36000, generation="c"),
        ]
        restricted = next(i for i in spans(readings) if i.status is Status.RESTRICTED)
        assert restricted.min_duration == timedelta(0)

    def test_the_first_span_began_before_monitoring_started(self):
        built = spans([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ])
        assert built[0].entered is None
        assert built[0].min_duration is None
        assert built[0].max_duration is None

    def test_the_last_span_is_open(self):
        built = spans([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ])
        assert built[-1].is_open
        assert built[-1].describe_duration() == "open"

    def test_an_unbounded_entry_makes_the_run_unbounded(self):
        readings = [
            reading(hours=0, status=Status.ACTIVE, age_max=None, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
            reading(hours=8, status=Status.ACTIVE, generation="c"),
        ]
        restricted = next(i for i in spans(readings) if i.status is Status.RESTRICTED)
        assert restricted.max_duration is None
        assert restricted.describe_duration() == "unbounded"

    def test_no_readings_means_no_spans(self):
        assert spans([]) == []


class TestUnknownLabelsDoNotManufactureTransitions:
    def _stream(self):
        return [
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.UNKNOWN, raw_label="Eingeschränkt (neu)", generation="b"),
            reading(hours=8, status=Status.ACTIVE, generation="c"),
        ]

    def test_skipping_unknown_reports_no_change(self):
        assert changes(self._stream()) == []

    def test_including_unknown_would_invent_two(self):
        """Which is why the default excludes them. A transition into UNKNOWN is a fact about our
        config, and in the output it would be indistinguishable from a real status movement."""
        assert len(changes(self._stream(), skip_unknown=False)) == 2


class TestMeasurement:
    def test_containment_is_the_headline(self):
        found = changes([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ])
        result = score(found, [truth_at(2, Status.ACTIVE, Status.RESTRICTED)], strategy="x")
        assert result.containing == 1
        assert result.containment_rate == 1.0
        assert result.direction_rate == 1.0

    def test_unbounded_claims_cannot_inflate_the_headline(self):
        """An unbounded interval contains every instant, so counting it as a success measures
        nothing. `bounded_containment_rate` removes them."""
        found = changes([
            reading(hours=0, status=Status.ACTIVE, age_max=None, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ])
        result = score(found, [truth_at(2, Status.ACTIVE, Status.RESTRICTED)], strategy="x")
        assert result.containment_rate == 1.0
        assert result.unbounded == 1
        assert result.bounded == 0
        assert result.bounded_containment_rate is None

    def test_rates_are_none_when_nothing_matched(self):
        result = score([], [truth_at(2, Status.ACTIVE, Status.RESTRICTED)], strategy="x")
        assert result.containment_rate is None
        assert result.direction_rate is None
        assert result.mean_width_hours is None
        assert result.mean_lateness_hours is None
        assert result.max_lateness_hours is None
        assert result.missed == 1

    def test_spurious_and_missed_are_reported_not_absorbed(self):
        found = naive_changes([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
            reading(hours=8, status=Status.ACTIVE, generation="c"),
            reading(hours=12, status=Status.RESTRICTED, generation="d"),
        ])
        result = score(found, [truth_at(2, Status.ACTIVE, Status.RESTRICTED)], strategy="x")
        assert result.reported == 3
        assert result.matched == 1
        assert result.spurious == 2

    def test_width_budget_separates_cadence_from_staleness(self):
        found = changes([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, age_min=3600, age_max=7200, generation="b"),
        ])
        result = score(found, [truth_at(2, Status.ACTIVE, Status.RESTRICTED)], strategy="x")
        budget = width_budget(result, timedelta(hours=4))
        assert budget.poll_interval_hours == 4.0
        assert budget.excess_hours is not None and budget.excess_hours >= 0.0

    def test_excess_is_clamped_when_the_bound_beats_the_cadence(self):
        found = changes([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=0.5, status=Status.RESTRICTED, generation="b"),
        ])
        result = score(found, [truth_at(0.25, Status.ACTIVE, Status.RESTRICTED)], strategy="x")
        budget = width_budget(result, timedelta(hours=4))
        assert budget.excess_hours == 0.0

    def test_staleness_share_is_none_with_no_width(self):
        result = score([], [], strategy="x")
        assert width_budget(result, timedelta(hours=4)).staleness_share is None


class TestRendering:
    def test_none_is_never_zero(self):
        assert pct(None) == "n/a"
        assert hours(None) == "n/a"
        assert span_text(None) == "n/a"
        assert pct(0.0) == "0.0%"
        assert hours(0.0) == "0.00h"

    def test_an_unbounded_lower_bound_prints_as_minus_infinity(self):
        assert instant(None) == "-inf"
        assert instant(at(0)).startswith("2026-03-01")

    def test_every_ordering_is_listed_even_at_zero(self):
        text = render_changes(changes([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ]))
        for value in Ordering:
            assert value.value in text

    def test_json_uses_null_and_csv_uses_an_empty_cell(self):
        import json

        found = changes([
            reading(hours=0, status=Status.ACTIVE, age_max=None, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ])
        result = score(found, [], strategy="interval")
        payload = json.loads(changes_json(found, {"interval": result},
                                          width_budget(result, timedelta(hours=4))))
        assert payload["changes"][0]["after"] is None
        assert payload["changes"][0]["width_hours"] is None
        assert payload["strategies"]["interval"]["containment_rate"] is None

        rows = changes_csv(found).strip().split("\n")
        assert rows[1].split(",")[4] == ""

    def test_readings_report_counts_the_three_categories(self):
        text = render_readings(
            [reading(hours=0, age_min=3600, age_max=7200),
             reading(hours=1, age_max=None),
             reading(hours=2, status=Status.UNKNOWN, raw_label="???")],
            stale_after=timedelta(minutes=15),
        )
        assert "1 reading(s) PROVABLY staler" in text
        assert "1 reading(s) UNDATED" in text
        assert "1 reading(s) with a label the mapping does not know" in text

    def test_no_conflicts_says_so_explicitly(self):
        assert "no label conflicts" in render_conflicts([])

    def test_spans_and_observations_render(self):
        readings = [
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ]
        assert "RANGE" in render_spans(spans(readings))
        assert "echo" in render_observations({("de", "L1"): observation_count(readings)})

    def test_comparison_explains_why_naive_containment_is_uninteresting(self):
        result = score([], [], strategy="naive")
        text = render_comparison({"naive": result}, width_budget(result, timedelta(hours=4)))
        assert "exact equality" in text
        assert "bias" in text


class TestHistorySchema:
    def test_round_trip_preserves_unbounded_as_null_not_zero(self, tmp_path):
        store = History(tmp_path / "h.db")
        store.initialise()
        store.record_readings([reading(hours=0, age_min=5, age_max=None, generation="a")])
        loaded = store.readings_for("de", "L1")
        assert len(loaded) == 1
        assert loaded[0].age.max_seconds is None
        assert loaded[0].age.min_seconds == 5.0
        assert loaded[0].provenance is Provenance.UNDATED

    def test_changes_keep_both_bounds_and_the_poll_time_separately(self, tmp_path):
        store = History(tmp_path / "h.db")
        store.initialise()
        found = changes([
            reading(hours=0, status=Status.ACTIVE, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, age_min=3600, age_max=7200, generation="b"),
        ])
        store.record_changes(found)
        loaded = store.changes_for("de", "L1")[0]
        assert loaded.after == found[0].after
        assert loaded.at_or_before == found[0].at_or_before
        # confirmed_at is when we LOOKED, and it is not the same as either bound.
        assert loaded.confirmed_at == at(4)
        assert loaded.confirmed_at > loaded.at_or_before

    def test_an_unbounded_change_stores_null(self, tmp_path):
        store = History(tmp_path / "h.db")
        store.initialise()
        store.record_changes(changes([
            reading(hours=0, status=Status.ACTIVE, age_max=None, generation="a"),
            reading(hours=4, status=Status.RESTRICTED, generation="b"),
        ]))
        assert store.changes_for("de", "L1")[0].after is None
        assert store.counts()["unbounded_changes"] == 1

    def test_reinserting_does_not_rewrite_what_was_observed(self, tmp_path):
        store = History(tmp_path / "h.db")
        store.initialise()
        item = reading(hours=0, generation="a")
        assert store.record_readings([item]) == 1
        assert store.record_readings([item]) == 0
        assert store.counts()["readings"] == 1

    def test_listings_are_enumerable(self, tmp_path):
        store = History(tmp_path / "h.db")
        store.initialise()
        store.record_readings([
            reading(hours=0, listing_id="A", generation="a"),
            reading(hours=0, listing_id="B", generation="b"),
        ])
        assert store.listings() == [("de", "A"), ("de", "B")]


class TestPropertiesOverTheShippedCorpus:
    def test_the_corpus_is_the_size_the_docs_claim(self, corpus, truth):
        assert len(corpus) == 451
        assert len(truth) == 17

    def test_every_provenance_occurs(self, corpus):
        """Otherwise the assertions about them prove nothing."""
        assert {item.provenance for item in corpus} == set(Provenance)

    def test_every_ordering_occurs_across_the_corpus(self, grouped):
        seen = set()
        for group in grouped.values():
            seen.update(item.ordering for item in changes(group))
            by_request = sorted(group, key=lambda item: item.observed_at)
            from status_checker.attribute import classify_ordering

            for earlier, later in pairwise(by_request):
                seen.add(classify_ordering(earlier, later))
        assert seen == set(Ordering), sorted(set(Ordering) - seen)

    def test_interval_attribution_contains_every_true_instant(self, grouped, truth):
        found = []
        for group in grouped.values():
            found.extend(changes(group))
        result = score(found, truth, strategy="interval")
        assert result.matched == 17
        assert result.containing == 17
        assert result.containment_rate == 1.0
        assert result.bounded_containment_rate == 1.0
        assert result.spurious == 0
        assert result.missed == 0

    def test_naive_attribution_is_always_late_and_never_early(self, grouped, truth):
        """The bias, which is the point. A one-sided error does not cancel across a fleet."""
        found = []
        for group in grouped.values():
            found.extend(naive_changes(group))
        result = score(found, truth, strategy="naive")
        assert result.early_count == 0
        assert result.late_count == 17
        assert result.mean_lateness_hours is not None
        assert result.mean_lateness_hours > 2.0
        assert result.max_lateness_hours is not None
        assert result.max_lateness_hours > 6.0

    def test_naive_invents_exactly_two_phantom_transitions(self, grouped, truth):
        found = []
        for group in grouped.values():
            found.extend(naive_changes(group))
        assert score(found, truth, strategy="naive").spurious == 2

        # And they come from the inverted listing alone.
        inverted = grouped[("es", "inverted")]
        assert len(naive_changes(inverted)) == 3
        assert len(changes(inverted)) == 1

    def test_the_corpus_contains_exactly_one_inversion(self, corpus):
        found = inversions(corpus)
        assert len(found) == 1
        assert found[0][0].listing_id == "inverted"

    def test_the_echo_storm_wastes_most_of_its_requests(self, grouped):
        count = observation_count(grouped[("us", "echoed")])
        assert count.requests == 216
        assert count.distinct_generations == 12
        assert count.echoes == 204
        assert count.independence_ratio is not None
        assert count.independence_ratio < 0.06

    def test_undated_listings_produce_unlocatable_changes(self, grouped):
        for key in (("de", "undated"), ("au", "undated")):
            found = changes(grouped[key])
            assert len(found) == 1
            assert not found[0].is_bounded

    def test_the_widest_interval_is_reported(self, grouped):
        found = []
        for group in grouped.values():
            found.extend(changes(group))
        widest = widest_interval(found)
        assert widest is not None and widest > timedelta(hours=4)

    def test_the_polling_interval_is_a_floor_on_width(self, grouped, truth):
        """Even perfect freshness cannot beat the cadence, so most of the width is unavoidable and
        the report says which part is not."""
        found = []
        for group in grouped.values():
            found.extend(changes(group))
        result = score(found, truth, strategy="interval")
        budget = width_budget(result, timedelta(hours=4))
        assert budget.mean_width_hours is not None
        assert budget.mean_width_hours >= 4.0
        assert budget.staleness_share is not None
        assert 0.0 < budget.staleness_share < 0.5

    def test_the_reworded_listing_does_not_flap(self, grouped):
        """The platform changed its wording halfway through. That is one unmapped label, not two
        status transitions."""
        found = changes(grouped[("de", "reworded")])
        assert len(found) == 1
        assert Status.UNKNOWN not in {found[0].from_status, found[0].to_status}

    def test_normalisation_is_required_for_the_widechar_listing(self, grouped):
        group = grouped[("jp", "widechars")]
        assert all(item.status is not Status.UNKNOWN for item in group)
        assert len(changes(group)) == 1

    def test_grouping_never_mixes_marketplaces(self, grouped):
        for (marketplace, listing), group in grouped.items():
            assert {item.marketplace for item in group} == {marketplace}
            assert {item.listing_id for item in group} == {listing}

    def test_interval_and_naive_agree_on_the_always_fresh_listings(self, grouped):
        """The naive strategy is not a straw man: where nothing is cached it is exactly right."""
        for key in (("us", "clean"), ("de", "clean"), ("jp", "clean")):
            interval = changes(grouped[key])
            naive = naive_changes(grouped[key])
            assert len(interval) == len(naive) == 2
            for left, right in zip(interval, naive, strict=True):
                assert (left.from_status, left.to_status) == (right.from_status, right.to_status)
                assert left.at_or_before == right.at_or_before

    def test_compare_returns_both_strategies(self, grouped, truth):
        interval_found, naive_found = [], []
        for group in grouped.values():
            interval_found.extend(changes(group))
            naive_found.extend(naive_changes(group))
        result = compare(interval_found, naive_found, truth)
        assert set(result) == {"interval", "naive"}
        assert result["interval"].containment_rate == 1.0
        assert result["naive"].containment_rate == 0.0
