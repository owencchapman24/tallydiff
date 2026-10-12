"""Attack the verifier with independently planned correspondence evidence."""

import copy
import csv
import json
import sys
from decimal import Decimal
from io import StringIO

import pytest

from scripts import benchmark
from scripts.benchmark import measure
from scripts.subset_benchmark import (
    ingest_subset_pair,
    subset_mapping,
    verify_subset_result,
)
from scripts.subset_oracle import generate_subset_pair
from tallydiff import (
    CorrespondenceReason,
    CorrespondenceStatus,
    FieldComparisonStatus,
    ReconciliationMode,
    export_exceptions_csv,
    reconcile,
)


@pytest.fixture
def observed():
    pair = generate_subset_pair(100)
    a, b = ingest_subset_pair(pair)
    result = reconcile(
        a,
        b,
        mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
        amount_tolerance=Decimal("0.01"),
        comparison_fields=subset_mapping().comparison_fields,
    )
    return a, b, result, export_exceptions_csv(result), pair.expected(Decimal("0.01"))


def forged(value, **changes):
    """Bypass model validation deliberately to test the verifier's own coverage."""
    result = copy.copy(value)
    for name, replacement in changes.items():
        object.__setattr__(result, name, replacement)
    return result


def altered_finding(result, index, finding):
    findings = list(result.findings)
    findings[index] = finding
    return forged(result, findings=tuple(findings))


class View:
    def __init__(self, original, **changes):
        self.original, self.changes = original, changes

    def __getattr__(self, name):
        return self.changes[name] if name in self.changes else getattr(self.original, name)


@pytest.mark.parametrize("mode", tuple(ReconciliationMode))
def test_measure_separates_phases_and_verifies_disabled_baseline(mode):
    measured = measure(100, 42, Decimal("0.01"), mode=mode, workload="subset")
    expected = generate_subset_pair(100).expected(Decimal("0.01"), mode=mode.value).summary()
    assert {key: measured.summary[key] for key in expected} == expected
    assert measured.total == pytest.approx(
        measured.generation
        + measured.ingestion
        + measured.reconciliation
        + measured.export
        + measured.verification
    )
    if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        assert measured.summary["same_input_unique_baseline"]["summary"]["analyzed"] == 0
        assert measured.summary["same_input_unique_baseline"]["summary"]["reserved"] == 0


def test_accept_untampered(observed):
    verify_subset_result(*observed)


@pytest.mark.parametrize(
    "tamper",
    (
        "replace_parent",
        "selected_order",
        "selected_swap",
        "omit_residual",
        "duplicate_residual",
        "replace_witness",
        "reverse_witnesses",
        "ambiguous_unique",
        "reason",
        "planned",
        "examined",
        "reserved",
        "incomplete_complete",
        "remove_inferred_exception",
        "move_tolerated",
        "secondary_match",
        "policy",
        "finding_order",
        "group_total",
        "raw_input",
        "mapping_order",
        "missing_analysis",
    ),
)
def test_verifier_rejects_structured_corruption(observed, tamper):
    a, b, result, report, truth = observed
    altered = result
    index = (
        7
        if tamper in ("omit_residual", "duplicate_residual", "selected_swap")
        else 11
        if tamper
        in ("replace_witness", "reverse_witnesses", "ambiguous_unique", "incomplete_complete")
        else 5
    )
    finding = result.findings[index]
    analysis = finding.correspondence_analysis
    if tamper == "replace_parent":
        finding = forged(finding, rows_a=(copy.copy(finding.rows_a[0]),))
    elif tamper in ("selected_order", "selected_swap"):
        solution = analysis.accepted_solution
        rows = (
            solution.rows_b[::-1]
            if tamper == "selected_order"
            else (solution.rows_b[0], analysis.unassigned_rows_b[0])
        )
        analysis = forged(analysis, accepted_solution=forged(solution, rows_b=rows))
    elif tamper == "omit_residual":
        analysis = forged(analysis, unassigned_rows_b=())
    elif tamper == "duplicate_residual":
        analysis = forged(analysis, unassigned_rows_b=analysis.unassigned_rows_b * 2)
    elif tamper == "replace_witness":
        witness = analysis.ambiguity_witnesses[0]
        changed = forged(witness, rows_b=(copy.copy(witness.rows_b[0]), *witness.rows_b[1:]))
        analysis = forged(
            analysis, ambiguity_witnesses=(changed, *analysis.ambiguity_witnesses[1:])
        )
    elif tamper == "reverse_witnesses":
        analysis = forged(analysis, ambiguity_witnesses=analysis.ambiguity_witnesses[::-1])
    elif tamper == "ambiguous_unique":
        analysis = forged(
            analysis,
            status=CorrespondenceStatus.UNIQUE_EXACT,
            accepted_solution=analysis.ambiguity_witnesses[0],
            ambiguity_witnesses=(),
        )
    elif tamper == "reason":
        analysis = forged(analysis, reason=CorrespondenceReason.RUN_BUDGET_EXHAUSTED)
    elif tamper in ("planned", "examined", "reserved"):
        attribute = f"{tamper}_combinations"
        analysis = forged(analysis, **{attribute: getattr(analysis, attribute) - 1})
    elif tamper == "incomplete_complete":
        analysis = forged(analysis, search_complete=True)
    elif tamper == "remove_inferred_exception":
        altered = View(
            result, exceptions=tuple(item for item in result.exceptions if item is not finding)
        )
    elif tamper == "move_tolerated":
        altered = View(result, tolerated_findings=(*result.tolerated_findings, finding))
    elif tamper == "secondary_match":
        comparison = forged(finding.field_comparisons[0], status=FieldComparisonStatus.MATCH)
        finding = forged(finding, field_comparisons=(comparison, *finding.field_comparisons[1:]))
    elif tamper == "policy":
        analysis = forged(analysis, policy=forged(analysis.policy, singleton_rivals_count=False))
    elif tamper == "finding_order":
        altered = forged(result, findings=result.findings[::-1])
    elif tamper == "group_total":
        finding = forged(finding, amount_a=finding.amount_a + Decimal("1"))
    elif tamper == "raw_input":
        row = a[0]
        a = (forged(row, raw_fields={**row.raw_fields, "Department": "fabricated"}), *a[1:])
    elif tamper == "mapping_order":
        altered = forged(result, comparison_fields=result.comparison_fields[::-1])
    elif tamper == "missing_analysis":
        analysis = None
    if altered is result:
        altered = altered_finding(result, index, forged(finding, correspondence_analysis=analysis))
    with pytest.raises(AssertionError):
        verify_subset_result(a, b, altered, report, truth)


@pytest.mark.parametrize(
    "tamper",
    (
        "row_reference",
        "missing_json_field",
        "reorder_json_keys",
        "witness",
        "reason",
        "anchor",
        "amount",
        "search_counter",
        "secondary",
        "csv_order",
        "status",
    ),
)
def test_verifier_rejects_exact_csv_and_json_corruption(observed, tamper):
    a, b, result, report, truth = observed
    rows = list(csv.reader(StringIO(report.decode(), newline="")))
    header = rows[0]
    evidence_column = header.index("Correspondence evidence")
    index = next(index for index, row in enumerate(rows[1:], 1) if row[evidence_column])
    evidence = json.loads(rows[index][evidence_column])
    if tamper == "row_reference":
        evidence["accepted_solution"]["rows_b"][0]["source_row"] += 1
    elif tamper == "missing_json_field":
        del evidence["schema_version"]
    elif tamper == "reorder_json_keys":
        evidence = dict(reversed(tuple(evidence.items())))
    elif tamper == "witness":
        index = next(
            index
            for index, row in enumerate(rows[1:], 1)
            if row[header.index("Correspondence status")] == "ambiguous"
        )
        evidence = json.loads(rows[index][evidence_column])
        evidence["ambiguity_witnesses"].reverse()
    elif tamper == "reason":
        evidence["reason"] = "run_budget_exhausted"
    elif tamper == "anchor":
        evidence["anchor"]["original_key"] = ["fabricated"]
    elif tamper == "amount":
        evidence["accepted_solution"]["amount"] = "0.0000"
    elif tamper == "search_counter":
        evidence["examined_combinations"] = 0
    elif tamper == "secondary":
        rows[index][header.index("Comparison 1 status")] = "match"
    elif tamper == "csv_order":
        rows[1:] = rows[:0:-1]
    elif tamper == "status":
        rows[index][header.index("Correspondence status")] = "ambiguous"
    if tamper not in ("secondary", "csv_order", "status"):
        rows[index][evidence_column] = json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":")
        )
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerows(rows)
    with pytest.raises(AssertionError, match="CSV"):
        verify_subset_result(a, b, result, output.getvalue().encode(), truth)


def test_repeat_export_guard(observed, monkeypatch):
    monkeypatch.setattr("scripts.subset_benchmark.export_exceptions_csv", lambda _: b"changed")
    with pytest.raises(AssertionError, match="repeat CSV"):
        verify_subset_result(*observed)


def test_cli_discloses_schema_policy_phases_counts_and_same_input_baseline(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "--workload",
            "subset",
            "--groups",
            "100",
            "--seed",
            "42",
            "--modes",
            "bounded_one_to_many",
            "--tolerances",
            "0.01",
        ],
    )
    benchmark.main()
    output = capsys.readouterr().out
    assert "workload=subset" in output and "Python " in output
    assert '"policy_id": "exact_unique_v1"' in output
    assert '"max_candidate_rows": 12' in output
    assert '"max_planned_combinations_per_run": 1000000' in output
    assert "gen_s ingest_s reconcile_s export_s verify_s total_s peak_MiB" in output
    assert '"planned": 4374' in output and '"examined": 4350' in output
    assert "same_input_unique_baseline" in output
