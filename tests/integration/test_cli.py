"""CLI behaviour and exit codes.

Run against the shipped corpus via `SC_*` env vars, so these exercise the same loading path a real
invocation does. Nothing is mocked, because nothing talks to a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from status_checker.cli import cli

DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {
        "SC_READINGS": str(DATA / "readings.jsonl"),
        "SC_LABELS": str(DATA / "labels.json"),
        "SC_TRUTH": str(DATA / "truth.jsonl"),
        "SC_HISTORY": str(tmp_path / "history.db"),
    }


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestDoctor:
    def test_reports_config_and_teaches_the_vocabulary(self, runner, env):
        result = runner.invoke(cli, ["doctor"], env=env)
        assert result.exit_code == 0
        assert "no credentials, no network, no model" in result.output
        for word in ("origin", "cached", "undated", "proven", "reversed", "overlapping"):
            assert word in result.output
        assert "observation about the PAST" in result.output

    def test_fails_when_inputs_are_missing(self, runner, tmp_path):
        result = runner.invoke(
            cli, ["doctor"], env={"SC_READINGS": str(tmp_path / "nope.jsonl")}
        )
        assert result.exit_code == 1
        assert "FAIL: missing input(s)" in result.output


class TestCheck:
    def test_fails_on_the_label_conflict_and_the_malformed_row(self, runner, env):
        """Both make a reported status arbitrary, so both gate."""
        result = runner.invoke(cli, ["check"], env=env)
        assert result.exit_code == 1
        assert "1 label conflict(s)" in result.output
        assert "unreadable row(s)" in result.output
        assert "makes a reported status arbitrary" in result.output

    def test_reports_staleness_without_failing_on_it(self, runner, env):
        result = runner.invoke(cli, ["check"], env=env)
        assert "PROVABLY staler" in result.output
        assert "UNDATED" in result.output
        assert "not\n  provably stale and not provably fresh either" in result.output

    def test_reports_the_unmapped_label_as_config_drift(self, runner, env):
        result = runner.invoke(cli, ["check"], env=env)
        assert "Eingeschränkt (neu)" in result.output
        assert "line to add to the mapping" in result.output
        assert "would let the mapping rot undetected" in result.output

    def test_names_the_marketplaces_that_cannot_express_a_status(self, runner, env):
        result = runner.invoke(cli, ["check"], env=env)
        assert "cannot express every status" in result.output
        assert "not a finding" in result.output

    def test_every_provenance_is_counted(self, runner, env):
        result = runner.invoke(cli, ["check"], env=env)
        for word in ("origin", "cached", "undated"):
            assert word in result.output

    def test_passes_on_a_clean_mapping(self, runner, env, tmp_path):
        """So the gate is not unconditional -- a check that always fires carries no information."""
        labels = json.loads((DATA / "labels.json").read_text(encoding="utf-8"))
        del labels["ca"]  # the table holding the conflicting entry
        clean_labels = tmp_path / "labels.json"
        clean_labels.write_text(json.dumps(labels), encoding="utf-8")

        rows = (DATA / "readings.jsonl").read_text(encoding="utf-8").splitlines()
        clean_readings = tmp_path / "readings.jsonl"
        clean_readings.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")

        result = runner.invoke(
            cli, ["check"],
            env={**env, "SC_LABELS": str(clean_labels), "SC_READINGS": str(clean_readings)},
        )
        assert result.exit_code == 0
        assert "no label conflicts" in result.output


class TestChanges:
    def test_interval_is_the_default_and_reports_bounds(self, runner, env):
        result = runner.invoke(cli, ["changes"], env=env)
        assert "strategy: interval (origin window)" in result.output
        assert "at or before" in result.output
        assert "-inf" in result.output  # the undated listings

    def test_naive_mode_is_available_for_comparison(self, runner, env):
        result = runner.invoke(cli, ["changes", "--naive"], env=env)
        assert "strategy: naive (request time)" in result.output

    def test_the_inversion_is_reported(self, runner, env):
        result = runner.invoke(cli, ["changes"], env=env)
        assert "1 request pair(s) INVERTED" in result.output
        assert "es/inverted" in result.output
        assert "infrastructure fact" in result.output

    def test_unbounded_changes_are_called_out(self, runner, env):
        result = runner.invoke(cli, ["changes"], env=env)
        assert "detectable and not" in result.output
        assert "locatable" in result.output

    def test_csv_is_written(self, runner, env, tmp_path):
        out = tmp_path / "out"
        result = runner.invoke(cli, ["changes", "-o", str(out)], env=env)
        rows = (out / "changes.csv").read_text(encoding="utf-8").strip().split("\n")
        assert rows[0].startswith("marketplace,listing_id,from_status,to_status,after")
        assert len(rows) == 18  # header + 17 changes
        # An unbounded lower bound is an EMPTY cell, never a date and never a zero.
        assert any(row.split(",")[4] == "" for row in rows[1:])
        assert result.exit_code == 1  # the malformed row


class TestDurations:
    def test_durations_are_ranges_with_the_naive_number_beside_them(self, runner, env):
        result = runner.invoke(cli, ["durations"], env=env)
        assert result.exit_code == 0
        assert "shortest" in result.output and "longest" in result.output
        assert "A duration is a RANGE" in result.output
        assert "unfalsifiable" in result.output

    def test_the_echo_storm_is_visible(self, runner, env):
        result = runner.invoke(cli, ["durations"], env=env)
        assert "216" in result.output
        assert "which is the trap" in result.output

    def test_open_and_pre_monitoring_spans_are_labelled(self, runner, env):
        result = runner.invoke(cli, ["durations"], env=env)
        assert "still in this state" in result.output
        assert "began before monitoring started" in result.output


class TestAudit:
    def test_scores_both_strategies_and_passes(self, runner, env):
        """Exit 0 despite the naive baseline being bad, because the baseline being bad is the
        finding rather than a regression."""
        result = runner.invoke(cli, ["audit"], env=env)
        assert result.exit_code == 0
        assert "interval" in result.output and "naive" in result.output
        assert "invented 2 transition(s) that never happened" in result.output
        assert "pages someone" in result.output

    def test_explains_why_naive_containment_is_not_the_interesting_number(self, runner, env):
        result = runner.invoke(cli, ["audit"], env=env)
        assert "exact equality" in result.output
        assert "does not cancel across a fleet" in result.output

    def test_the_width_budget_separates_cadence_from_staleness(self, runner, env):
        result = runner.invoke(cli, ["audit"], env=env)
        assert "polling floor" in result.output
        assert "no arithmetic beats this" in result.output
        assert "staleness share" in result.output

    def test_json_carries_nulls_not_zeros(self, runner, env, tmp_path):
        out = tmp_path / "out"
        runner.invoke(cli, ["audit", "-o", str(out)], env=env)
        payload = json.loads((out / "audit.json").read_text(encoding="utf-8"))

        assert payload["strategies"]["interval"]["containment_rate"] == 1.0
        assert payload["strategies"]["interval"]["spurious"] == 0
        assert payload["strategies"]["naive"]["spurious"] == 2
        assert payload["strategies"]["naive"]["early_count"] == 0
        assert payload["strategies"]["naive"]["late_count"] == 17
        # A naive claim is a point, so its width is a MEASURED zero -- distinct from a null,
        # which is what the interval strategy reports for lateness it never incurred.
        assert payload["strategies"]["naive"]["mean_width_hours"] == 0.0
        assert payload["strategies"]["interval"]["mean_lateness_hours"] is None

        unbounded = [item for item in payload["changes"] if item["after"] is None]
        assert len(unbounded) == 2
        assert all(item["width_hours"] is None for item in unbounded)

    def test_fails_when_the_truth_file_is_unreadable(self, runner, env, tmp_path):
        bad = tmp_path / "truth.jsonl"
        bad.write_text('{"marketplace": "us"}\n', encoding="utf-8")
        result = runner.invoke(cli, ["audit"], env={**env, "SC_TRUTH": str(bad)})
        assert result.exit_code == 1
        assert "unreadable row(s) in the known-changes file" in result.output
        # And it must NOT blame the strategy for a broken fixture.
        assert "interval attribution reported" not in result.output


class TestRecord:
    def test_writes_readings_and_changes(self, runner, env):
        result = runner.invoke(cli, ["record"], env=env)
        assert result.exit_code == 0
        assert "inserted 451 reading(s), 17 change(s)" in result.output

    def test_null_means_unbounded_not_zero(self, runner, env):
        result = runner.invoke(cli, ["record"], env=env)
        assert "48 reading(s) with age_max NULL -- unbounded, not zero" in result.output
        assert "2 change(s) with after NULL -- detectable, not locatable" in result.output

    def test_explains_why_the_schema_has_two_columns(self, runner, env):
        result = runner.invoke(cli, ["record"], env=env)
        assert "not `changed_at`" in result.output
        assert "forces the poll time" in result.output

    def test_rerunning_inserts_nothing_new(self, runner, env):
        runner.invoke(cli, ["record"], env=env)
        again = runner.invoke(cli, ["record"], env=env)
        assert "inserted 0 reading(s), 0 change(s)" in again.output
        assert "stored   451 reading(s)" in again.output


class TestExplain:
    def test_shows_every_reading_with_the_window_it_describes(self, runner, env):
        result = runner.invoke(cli, ["explain", "es", "inverted"], env=env)
        assert result.exit_code == 0
        assert "describes [" in result.output
        assert "19 reading(s)" in result.output

    def test_shows_the_echo_ratio_where_it_matters(self, runner, env):
        result = runner.invoke(cli, ["explain", "us", "echoed"], env=env)
        assert "216 request(s) -> 12 distinct origin generation(s), 204 echo(es)" in result.output
        assert "not corroboration" in result.output

    def test_names_the_unmapped_label_and_why_it_is_excluded(self, runner, env):
        result = runner.invoke(cli, ["explain", "de", "reworded"], env=env)
        assert "Eingeschränkt (neu)" in result.output
        assert "manufacture two phantom status changes" in result.output

    def test_an_undated_listing_shows_an_unbounded_lower_bound(self, runner, env):
        result = runner.invoke(cli, ["explain", "de", "undated"], env=env)
        assert "-inf" in result.output
        assert "unbounded" in result.output

    def test_an_unknown_listing_exits_nonzero(self, runner, env):
        result = runner.invoke(cli, ["explain", "de", "nope"], env=env)
        assert result.exit_code == 1
        assert "no readings for de/nope" in result.output

    def test_an_unknown_marketplace_says_it_is_not_in_the_mapping(self, runner, env):
        result = runner.invoke(cli, ["explain", "zz", "anything"], env=env)
        assert result.exit_code == 1
        assert "not in the label mapping at all" in result.output
