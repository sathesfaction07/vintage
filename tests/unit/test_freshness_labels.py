"""Age arithmetic, windows, and label mapping."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import at, reading
from status_checker.freshness import (
    age_from_headers,
    apparent_age,
    freshness_lifetime,
    generation_of,
    parse_cache_control,
    stale_by,
)
from status_checker.labels import LabelMap, normalise
from status_checker.models import AgeBound, Ordering, Provenance, Status, Window


class TestAgeIsABoundNotANumber:
    def test_no_store_is_a_direct_response(self):
        provenance, age = age_from_headers(observed_at=at(), cache_control="no-store")
        assert provenance is Provenance.ORIGIN
        assert age.is_exact and age.min_seconds == 0.0

    def test_no_cache_is_also_direct_because_it_must_be_revalidated(self):
        provenance, age = age_from_headers(observed_at=at(), cache_control="no-cache")
        assert provenance is Provenance.ORIGIN
        assert age.is_exact

    def test_max_age_zero_cannot_be_satisfied_by_a_stored_copy(self):
        provenance, age = age_from_headers(observed_at=at(), cache_control="max-age=0")
        assert provenance is Provenance.ORIGIN
        assert age.max_seconds == 0.0

    def test_age_header_with_a_lifetime_bounds_from_both_sides(self):
        provenance, age = age_from_headers(
            observed_at=at(), age_header=3600, cache_control="max-age=7200"
        )
        assert provenance is Provenance.CACHED
        assert (age.min_seconds, age.max_seconds) == (3600.0, 7200.0)

    def test_age_header_without_a_lifetime_is_unbounded_above(self):
        _, age = age_from_headers(observed_at=at(), age_header=3600)
        assert age.min_seconds == 3600.0
        assert age.max_seconds is None
        assert not age.is_dated

    def test_a_missing_age_header_is_undated_not_fresh(self):
        """The single most important case. A conforming cache MUST send Age, so its absence is
        evidence of a direct response -- but evidence is not a bound, and the two errors do not
        cost the same."""
        provenance, age = age_from_headers(observed_at=at())
        assert provenance is Provenance.UNDATED
        assert provenance is not Provenance.ORIGIN
        assert age.max_seconds is None

    def test_a_lifetime_bounds_the_age_even_when_no_age_header_arrived(self):
        provenance, age = age_from_headers(observed_at=at(), cache_control="max-age=600")
        assert provenance is Provenance.UNDATED
        assert age.max_seconds == 600.0
        assert age.is_dated

    def test_s_maxage_wins_for_shared_caches(self):
        _, age = age_from_headers(
            observed_at=at(), age_header=10, cache_control="max-age=99999, s-maxage=60"
        )
        assert age.max_seconds == 60.0

    def test_a_cache_serving_past_its_own_lifetime_forfeits_the_upper_bound(self):
        """The lifetime is a bound only if the cache respects it. Here it plainly did not, so
        keeping it would produce a bound narrower than the evidence supports."""
        _, age = age_from_headers(
            observed_at=at(), age_header=9000, cache_control="max-age=7200"
        )
        assert age.min_seconds == 9000.0
        assert age.max_seconds is None

    def test_apparent_age_from_the_date_header_is_a_floor(self):
        _, age = age_from_headers(observed_at=at(1), date_header=at(0))
        assert age.min_seconds == 3600.0

    def test_the_larger_floor_wins(self):
        _, age = age_from_headers(observed_at=at(2), age_header=600, date_header=at(0))
        assert age.min_seconds == 7200.0

    def test_a_date_in_the_future_is_clock_skew_not_a_negative_age(self):
        assert apparent_age(at(5), at(0)) == 0.0
        _, age = age_from_headers(observed_at=at(0), date_header=at(5))
        assert age.min_seconds == 0.0

    def test_request_round_trip_adds_to_the_floor(self):
        _, age = age_from_headers(
            observed_at=at(0) + timedelta(seconds=30), age_header=100, request_sent_at=at(0)
        )
        assert age.min_seconds == 130.0

    def test_a_malformed_directive_costs_knowledge_not_the_reading(self):
        assert parse_cache_control("max-age=banana") == {"max-age": "banana"}
        assert freshness_lifetime(parse_cache_control("max-age=banana")) is None
        _, age = age_from_headers(observed_at=at(), cache_control="max-age=banana")
        assert age.max_seconds is None

    def test_expires_is_deliberately_ignored(self):
        """It is an instant on the origin's clock, so trusting it imports clock skew into a bound
        whose entire purpose is to be safe."""
        _, age = age_from_headers(observed_at=at(), cache_control="public")
        assert age.max_seconds is None

    def test_a_bound_cannot_be_inverted(self):
        with pytest.raises(ValueError, match="below min_seconds"):
            AgeBound(min_seconds=100.0, max_seconds=50.0)

    def test_describe_distinguishes_the_three_shapes(self):
        assert AgeBound.exact(0).describe() == "0s"
        assert AgeBound(min_seconds=1.0, max_seconds=2.0).describe() == "1-2s"
        assert "no upper bound" in AgeBound.unbounded(5).describe()


class TestStalenessIsProvableOrItIsNotClaimed:
    def test_provably_stale(self):
        assert stale_by(AgeBound(min_seconds=3600, max_seconds=7200), timedelta(minutes=30))

    def test_an_undated_reading_is_not_reported_stale(self):
        """It is not provably stale and not provably fresh. Reporting it as stale would put an
        unfalsifiable claim next to a demonstrable one."""
        assert not stale_by(AgeBound.unbounded(), timedelta(minutes=30))

    def test_fresh_is_not_stale(self):
        assert not stale_by(AgeBound.exact(0), timedelta(0))


class TestWindow:
    def test_a_fresh_reading_describes_an_instant(self):
        window = reading(hours=5).window
        assert window.is_instant and window.start == window.end == at(5)

    def test_a_cached_reading_describes_a_span_in_the_past(self):
        window = reading(hours=12, age_min=3600, age_max=7200).window
        assert window.start == at(10) and window.end == at(11)
        assert window.width == timedelta(hours=1)

    def test_an_undated_reading_is_unbounded_below(self):
        window = reading(hours=5, age_min=0, age_max=None).window
        assert window.start is None
        assert not window.is_bounded
        assert window.width is None
        # It still proves the value existed at or before the request.
        assert window.end == at(5)

    def test_precedes_needs_a_gap(self):
        assert reading(hours=0).window.precedes(reading(hours=1).window)
        assert not reading(hours=1).window.precedes(reading(hours=0).window)

    def test_nothing_precedes_an_unbounded_window(self):
        assert not reading(hours=0).window.precedes(reading(hours=9, age_max=None).window)

    def test_a_window_cannot_be_inverted(self):
        with pytest.raises(ValueError, match="after end"):
            Window(start=at(5), end=at(1))


class TestOrderingHasFourCasesAndAllOfThemHappen:
    def test_proven(self):
        from status_checker.attribute import classify_ordering

        assert classify_ordering(reading(hours=0), reading(hours=4)) is Ordering.PROVEN

    def test_reversed_is_the_headline(self):
        """A fresh read at 08:00 describes 08:00. A read at 08:30 from a 1-2h cache describes
        06:30-07:30. Every instant the later request could describe is BEFORE the instant the
        earlier one describes, so the origin order is the reverse of the request order."""
        from status_checker.attribute import classify_ordering

        fresh = reading(hours=8)
        stale = reading(hours=8.5, age_min=3600, age_max=7200)
        assert classify_ordering(fresh, stale) is Ordering.REVERSED
        assert stale.window.precedes(fresh.window)
        assert not fresh.window.precedes(stale.window)

    def test_overlapping_means_order_is_not_established(self):
        from status_checker.attribute import classify_ordering

        first = reading(hours=4, age_min=0, age_max=7200)
        second = reading(hours=5, age_min=0, age_max=7200)
        assert classify_ordering(first, second) is Ordering.OVERLAPPING

    def test_unbounded_cannot_be_placed_at_all(self):
        from status_checker.attribute import classify_ordering

        assert (
            classify_ordering(reading(hours=0), reading(hours=4, age_max=None))
            is Ordering.UNBOUNDED
        )
        assert (
            classify_ordering(reading(hours=0, age_max=None), reading(hours=4))
            is Ordering.UNBOUNDED
        )


class TestGenerationIdentity:
    def test_etag_is_preferred(self):
        assert generation_of(etag='W/"abc"', date_header=at()).startswith("etag:")

    def test_date_is_the_fallback(self):
        assert generation_of(date_header=at()).startswith("date:")

    def test_last_modified_is_weakest(self):
        assert generation_of(last_modified=at()).startswith("lastmod:")

    def test_nothing_identifying_yields_none(self):
        """And None must not be read as a shared identity: two unidentifiable readings are not
        thereby the same reading."""
        assert generation_of() is None
        assert generation_of(etag="") is None


class TestLabelNormalisation:
    def test_nfkc_and_casefold_are_both_required(self):
        """Neither substitutes for the other, which is why both are applied. The same conclusion
        CaseSearch reached for merchant names -- a repeat, cited as one."""
        assert normalise("ＡＣＴＩＶＥ") == normalise("active")  # noqa: RUF001 - fullwidth is the point
        assert normalise("STRASSE") == normalise("straße")  # sharp s needs casefold
        assert "straße".lower() == "straße"  # lower() alone does not

    def test_whitespace_is_collapsed(self):
        assert normalise("  Under   Review  ") == "under review"
        assert normalise("Under\u00a0Review") == "under review"  # non-breaking space
        assert normalise("\u3000Active\u00a0") == "active"

    def test_an_unmapped_label_becomes_unknown(self, labels: LabelMap):
        assert labels.resolve("de", "Eingeschränkt (neu)") is Status.UNKNOWN

    def test_an_unknown_marketplace_also_yields_unknown(self, labels: LabelMap):
        assert labels.resolve("zz", "Active") is Status.UNKNOWN
        assert not labels.knows_marketplace("zz")

    def test_localised_labels_resolve(self, labels: LabelMap):
        assert labels.resolve("de", "Eingeschränkt") is Status.RESTRICTED
        assert labels.resolve("jp", "制限あり") is Status.RESTRICTED
        assert labels.resolve("fr", "En cours d'examen") is Status.PENDING


class TestLabelConflictsAreStaticDefects:
    def test_the_shipped_mapping_contains_exactly_one_conflict(self, labels: LabelMap):
        conflicts = labels.conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].label == "under review"
        assert conflicts[0].statuses == {Status.PENDING, Status.RESTRICTED}

    def test_a_conflict_makes_a_fleet_count_undefined(self, labels: LabelMap):
        """Both tables are individually plausible. Any aggregate over them has no single answer,
        and it will still compute to a number that looks entirely reasonable."""
        conflict = labels.conflicts()[0]
        assert conflict.mappings["us"] is Status.PENDING
        assert conflict.mappings["ca"] is Status.RESTRICTED

    def test_no_conflict_when_tables_agree(self):
        agreeing = LabelMap({"a": {"Live": Status.ACTIVE}, "b": {"Live": Status.ACTIVE}})
        assert agreeing.conflicts() == []

    def test_conflicts_compare_on_the_normalised_key(self):
        sneaky = LabelMap(
            {"a": {"Under Review": Status.PENDING}, "b": {"under  review": Status.RESTRICTED}}
        )
        assert len(sneaky.conflicts()) == 1

    def test_a_table_that_cannot_express_a_status_is_reported(self, labels: LabelMap):
        """So a zero for that status is not read as a finding."""
        missing = labels.missing_statuses()
        assert Status.SUPPRESSED in missing["au"]
        assert Status.PENDING in missing["ca"]

    def test_mapping_a_label_to_unknown_is_flagged_as_pointless(self, tmp_path):
        import json

        path = tmp_path / "labels.json"
        path.write_text(json.dumps({"a": {"Weird": "unknown"}}), encoding="utf-8")
        _, problems = LabelMap.from_file(path)
        assert any("hides drift" in problem for problem in problems)

    def test_an_unrecognised_status_name_is_reported_not_raised(self, tmp_path):
        import json

        path = tmp_path / "labels.json"
        path.write_text(json.dumps({"a": {"X": "banana", "Y": "active"}}), encoding="utf-8")
        mapping, problems = LabelMap.from_file(path)
        assert len(problems) == 1
        # The rest of the table survives.
        assert mapping.resolve("a", "Y") is Status.ACTIVE
