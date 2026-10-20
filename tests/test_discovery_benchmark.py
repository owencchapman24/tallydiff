"""Tiny completed-workflow benchmark invariants, without CI performance thresholds."""

import csv
import json
import tracemalloc
from dataclasses import replace
from decimal import Decimal
from io import StringIO

import pytest

from scripts import discovery_benchmark as benchmark
from tallydiff.discovery import (
    PossibleKeyDifference,
    PossibleKeyDifferenceResult,
    compare_amount_columns,
    compare_key_mapping,
    discover_csv,
    find_possible_key_differences,
    suggest_amount_mappings,
    suggest_key_mappings,
)
from tallydiff.models import Source

PHASES = {
    "discover_csv_a",
    "discover_csv_b",
    "discover_csv_pair",
    "suggest_key_mappings",
    "suggest_amount_mappings",
    "compare_key_mapping",
    "compare_amount_columns",
    "find_possible_key_differences",
}
PAIRS = (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref"))


@pytest.mark.parametrize("scenario", benchmark.WORKFLOW_SCENARIOS)
@pytest.mark.parametrize("row_count,column_count", [(0, 10), (1, 12), (8, 12)])
def test_workflow_generation_is_deterministic_and_reuses_representative_mixed_columns(
    scenario, row_count, column_count
):
    pair = benchmark.generate_workflow_csv_pair(row_count, column_count, scenario=scenario)
    assert pair == benchmark.generate_workflow_csv_pair(row_count, column_count, scenario=scenario)
    for text, headers in zip(pair, (benchmark.HEADERS_A, benchmark.HEADERS_B), strict=True):
        rows = list(csv.reader(StringIO(text)))
        assert tuple(rows[0]) == headers[:column_count]
        assert len(rows) == row_count + 1
        assert all(len(row) == column_count for row in rows)
        if row_count:
            assert rows[1][2:] == list(benchmark._synthetic_row(0)[2:column_count])
    a, b = discover_csv(pair[0], source=Source.A), discover_csv(pair[1], source=Source.B)
    facts = compare_key_mapping(a, b, key_pairs=PAIRS)
    assert facts.eligible_row_count_a == facts.eligible_row_count_b == row_count
    assert facts.duplicate_excess_count_a == facts.duplicate_excess_count_b == 0
    assert facts.overlap_distinct_count == 0
    differences = find_possible_key_differences(a, b, key_pairs=PAIRS)
    assert differences.evaluated and differences.signature_entry_count == row_count * 4
    if scenario == "one_component_differences":
        assert differences.total_candidate_count == row_count
        assert not differences.ambiguity_suppressed
    else:
        assert differences.total_candidate_count == (1 if row_count == 1 else 0)
        assert differences.ambiguity_suppressed is (row_count > 1)


@pytest.mark.parametrize("scenario", benchmark.WORKFLOW_SCENARIOS)
def test_tiny_workflow_reports_separate_phases_complete_observations_and_python_memory(scenario):
    observed = benchmark.benchmark_workflow_case(8, column_count=10, scenario=scenario)
    assert observed["scenario"] == scenario
    assert observed["selected_key_pairs"] == PAIRS
    assert observed["selected_amount_pair"] == ("Invoice Amount", "Gross Amount")
    assert set(observed["elapsed_seconds"]) == PHASES
    assert set(observed["observations"]) == PHASES
    memory = observed["python_memory"]
    assert set(memory["peak_mib"]) == set(memory["incremental_peak_mib"]) == PHASES
    assert all(value >= 0 for value in observed["elapsed_seconds"].values())
    assert all(value >= 0 for value in memory["peak_mib"].values())
    assert all(value >= 0 for value in memory["incremental_peak_mib"].values())
    assert memory["retained_snapshot_bytes"] >= 0
    assert memory["retained_snapshot_mib"] == memory["retained_snapshot_bytes"] / (1024 * 1024)
    assert memory["peak_mib"]["discover_csv_pair"] == max(
        memory["peak_mib"]["discover_csv_a"], memory["peak_mib"]["discover_csv_b"]
    )
    seconds = observed["elapsed_seconds"]
    assert seconds["discover_csv_pair"] == seconds["discover_csv_a"] + seconds["discover_csv_b"]
    for facts in observed["observations"].values():
        assert len(facts["checksum"]) == 64
        assert set(facts["checksum"]) <= set("0123456789abcdef")
    generated = benchmark.generate_workflow_csv_pair(8, 10, scenario=scenario)
    a, b = discover_csv(generated[0], source=Source.A), discover_csv(generated[1], source=Source.B)
    keys, amounts = suggest_key_mappings(a, b), suggest_amount_mappings(a, b)
    assert observed["observations"]["suggest_key_mappings"]["evaluated_single_count"] == 100
    assert observed["observations"]["suggest_key_mappings"][
        "checksum"
    ] == benchmark._content_checksum(keys)
    assert observed["observations"]["suggest_amount_mappings"]["evaluated_count"] == 100
    assert observed["observations"]["suggest_amount_mappings"][
        "checksum"
    ] == benchmark._content_checksum(amounts)
    selected_amount = compare_amount_columns(
        a, b, column_a="Invoice Amount", column_b="Gross Amount"
    )
    amount_facts = observed["observations"]["compare_amount_columns"]
    assert amount_facts["parseable_count_a"] == amount_facts["parseable_count_b"] == 8
    assert amount_facts["total_a"] == str(selected_amount.total_a)
    assert amount_facts["control_difference"] == str(selected_amount.control_difference)
    detector = observed["observations"]["find_possible_key_differences"]
    assert detector["evaluated"] and detector["skip_reason"] is None
    assert detector["total_candidate_count"] == (
        8 if scenario == "one_component_differences" else 0
    )
    assert detector["ambiguity_suppressed"] is (scenario == "ambiguous_signatures")
    assert not detector["display_truncated"]
    json.dumps(observed, ensure_ascii=False, allow_nan=False)


def test_content_checksums_include_exact_candidate_values_and_decimal_text():
    candidate = PossibleKeyDifference(("V/Ω", 'LEFT-"1\n'), ("V/Ω", "RIGHT-1"), 1, "left", "right")
    changed = replace(candidate, key_b=("V/Ω", "RIGHT-2"))
    assert benchmark._content_checksum(candidate) != benchmark._content_checksum(changed)
    assert benchmark._content_checksum({"total": Decimal("1.20")}) != benchmark._content_checksum(
        {"total": Decimal("1.2")}
    )
    assert benchmark._content_checksum({"a": 1, "b": 2}) == benchmark._content_checksum(
        {"b": 2, "a": 1}
    )


def test_skipped_and_evaluated_zero_candidate_observations_are_distinct():
    skipped = PossibleKeyDifferenceResult(
        PAIRS, (), 0, False, False, "signature_entry_limit", False, 100002
    )
    empty = PossibleKeyDifferenceResult(PAIRS, (), 0, False, True, None, False, 0)
    skipped_facts = benchmark._workflow_observation("find_possible_key_differences", skipped)
    empty_facts = benchmark._workflow_observation("find_possible_key_differences", empty)
    assert (
        not skipped_facts["evaluated"] and skipped_facts["skip_reason"] == "signature_entry_limit"
    )
    assert empty_facts["evaluated"] and empty_facts["skip_reason"] is None
    assert skipped_facts["checksum"] != empty_facts["checksum"]


def test_workflow_fails_visibly_when_timing_and_allocation_passes_disagree(monkeypatch):
    def fake_pass(csv_pair, *, measure_memory):
        return {"observations": {"checksum": "changed" if measure_memory else "original"}}

    monkeypatch.setattr(benchmark, "_measure_workflow_pass", fake_pass)
    with pytest.raises(AssertionError, match="different workflow content"):
        benchmark.benchmark_workflow_case(4)


def test_workflow_stops_its_memory_tracing_after_an_operation_failure(monkeypatch):
    inputs = benchmark.generate_workflow_csv_pair(2)
    original = benchmark.discover_csv

    def fail_b(data, *, source):
        if source is Source.B:
            raise RuntimeError("synthetic discovery failure")
        return original(data, source=source)

    monkeypatch.setattr(benchmark, "discover_csv", fail_b)
    with pytest.raises(RuntimeError, match="synthetic discovery failure"):
        benchmark._measure_workflow_pass(inputs, measure_memory=True)
    assert not tracemalloc.is_tracing()


def test_workflow_does_not_stop_a_callers_existing_allocation_trace():
    inputs = benchmark.generate_workflow_csv_pair(2)
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match="requires inactive tracemalloc"):
            benchmark._measure_workflow_pass(inputs, measure_memory=True)
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_completed_workflow_extension_preserves_existing_benchmark_phase_fields(monkeypatch):
    calls = []

    def fake_workflow(row_count, column_count, *, scenario):
        calls.append((row_count, column_count, scenario))
        return {"scenario": scenario}

    monkeypatch.setattr(benchmark, "benchmark_workflow_case", fake_workflow)
    observed = benchmark.benchmark_case(4, column_count=10, composite_limit=2)
    legacy_phases = {"discover_csv_pair", "single_key_pairs", "amount_pairs", "composite_key_pairs"}
    assert set(observed["elapsed_seconds"]) == legacy_phases
    assert set(observed["python_memory"]["peak_mib"]) == legacy_phases
    assert observed["candidate_counts"] == {
        "single_key_pairs": 100,
        "amount_pairs": 100,
        "composite_key_pairs": 2,
    }
    assert calls == [(4, 10, scenario) for scenario in benchmark.WORKFLOW_SCENARIOS]
    assert observed["v08_workflows"] == [
        {"scenario": scenario} for scenario in benchmark.WORKFLOW_SCENARIOS
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"row_count": -1},
        {"row_count": True},
        {"row_count": 2, "column_count": 9},
        {"row_count": 2, "scenario": "unknown"},
    ],
)
def test_invalid_workflow_generation_arguments_fail_visibly(kwargs):
    with pytest.raises((TypeError, ValueError)):
        benchmark.generate_workflow_csv_pair(**kwargs)
