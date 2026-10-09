"""Attack the comparison runner's checks with independently constructed truth."""

import csv
from dataclasses import replace
from decimal import Decimal
from io import StringIO

import pytest

from scripts import benchmark
from scripts.benchmark import comparison_mapping, measure, verify_comparison_result
from scripts.synthetic_data import COMPARISON_MAPPINGS, generate_pair
from tallydiff import (
    FieldComparisonStatus,
    ReconciliationMode,
    Source,
    export_exceptions_csv,
    ingest_csv,
    reconcile,
)


@pytest.fixture(params=tuple(ReconciliationMode))
def observed_comparisons(request):
    pair = generate_pair(100, seed=42, workload="comparison")
    mapping = comparison_mapping()
    a = ingest_csv(
        pair.csv_a,
        source=Source.A,
        key_columns=mapping.keys_a,
        amount_column=mapping.amount_a,
        comparison_columns=mapping.comparisons_a,
    )
    b = ingest_csv(
        pair.csv_b,
        source=Source.B,
        key_columns=mapping.keys_b,
        amount_column=mapping.amount_b,
        comparison_columns=mapping.comparisons_b,
    )
    result = reconcile(
        a,
        b,
        amount_tolerance=Decimal("0.01"),
        mode=request.param,
        comparison_fields=mapping.comparison_fields,
    )
    return (
        a,
        b,
        result,
        export_exceptions_csv(result),
        pair.expected(Decimal("0.01"), mode=request.param.value),
    )


@pytest.mark.parametrize("mode", tuple(ReconciliationMode))
def test_comparison_measure_runs_every_phase_and_independent_checks(mode):
    measured = measure(100, 42, Decimal("0.01"), mode=mode, workload="comparison")
    truth = generate_pair(100, seed=42, workload="comparison").expected(
        Decimal("0.01"), mode=mode.value
    )
    assert (measured.rows_a, measured.rows_b) == (truth.record_count_a, truth.record_count_b)
    assert measured.exceptions == len(truth.exception_keys)
    assert measured.summary == truth.summary()
    assert truth.comparison_fields == COMPARISON_MAPPINGS
    phases = (
        measured.generation,
        measured.ingestion,
        measured.reconciliation,
        measured.export,
        measured.verification,
    )
    assert all(phase > 0 for phase in phases)
    assert sum(phases) == pytest.approx(measured.total)


def test_comparison_verifier_accepts_untampered_truth(observed_comparisons):
    verify_comparison_result(*observed_comparisons)


def _replace_finding(result, index, finding):
    findings = list(result.findings)
    findings[index] = finding
    return replace(result, findings=tuple(findings))


@pytest.mark.parametrize("tamper", ("status", "values", "amount", "row_order", "finding_order"))
def test_verifier_rejects_material_structured_result_corruption(observed_comparisons, tamper):
    a, b, result, report, truth = observed_comparisons
    if tamper in ("status", "values"):
        index = next(
            index
            for index, finding in enumerate(result.findings)
            if all(
                field.status is FieldComparisonStatus.MATCH for field in finding.field_comparisons
            )
        )
        finding = result.findings[index]
        comparison = finding.field_comparisons[0]
        if tamper == "status":
            comparison = replace(comparison, status=FieldComparisonStatus.NOT_COMPARABLE)
        else:
            comparison = replace(comparison, values_a=("fabricated",), values_b=("fabricated",))
        finding = replace(
            finding,
            field_comparisons=(comparison, *finding.field_comparisons[1:]),
        )
        altered = _replace_finding(result, index, finding)
    elif tamper == "amount":
        finding = result.findings[0]
        altered = _replace_finding(
            result, 0, replace(finding, amount_a=finding.amount_a + Decimal("1"))
        )
    elif tamper == "row_order":
        index = next(
            index for index, finding in enumerate(result.findings) if len(finding.rows_a) > 1
        )
        finding = result.findings[index]
        altered = _replace_finding(result, index, replace(finding, rows_a=finding.rows_a[::-1]))
    else:
        altered = replace(result, findings=result.findings[::-1])
    with pytest.raises(AssertionError):
        verify_comparison_result(a, b, altered, report, truth)


def test_verifier_rejects_reordered_mapping_with_coherently_reordered_findings(
    observed_comparisons,
):
    a, b, result, report, truth = observed_comparisons
    altered = replace(
        result,
        comparison_fields=result.comparison_fields[::-1],
        findings=tuple(
            replace(finding, field_comparisons=finding.field_comparisons[::-1])
            for finding in result.findings
        ),
    )
    with pytest.raises(AssertionError, match="mapping count/order"):
        verify_comparison_result(a, b, altered, report, truth)


@pytest.mark.parametrize("tamper", ("raw_evidence", "key", "source_row", "source", "amount"))
def test_verifier_compares_ingested_evidence_with_construction_truth(observed_comparisons, tamper):
    a, b, result, report, truth = observed_comparisons
    row = a[0]
    if tamper == "raw_evidence":
        fields = dict(row.raw_fields)
        fields["Department"] = "fabricated"
        altered_row = replace(row, raw_fields=fields)
    elif tamper == "key":
        altered_row = replace(row, key=("fabricated", *row.key[1:]))
    elif tamper == "source_row":
        altered_row = replace(row, source_row=row.source_row + 10_000)
    elif tamper == "source":
        altered_row = replace(row, source=Source.B)
    else:
        altered_row = replace(row, amount=row.amount + Decimal("1"))
    # Replace the input and all finding references coherently. A verifier that
    # merely checks result evidence against already parsed inputs would accept this.
    altered = replace(
        result,
        findings=tuple(
            replace(
                finding,
                rows_a=tuple(
                    altered_row if original is row else original for original in finding.rows_a
                ),
            )
            for finding in result.findings
        ),
    )
    with pytest.raises(AssertionError, match="Ingested source evidence"):
        verify_comparison_result((altered_row, *a[1:]), b, altered, report, truth)


def test_verifier_rejects_replaced_source_object_even_when_all_values_match(observed_comparisons):
    a, b, result, report, truth = observed_comparisons
    index = next(index for index, finding in enumerate(result.findings) if finding.rows_a)
    finding = result.findings[index]
    altered = _replace_finding(
        result,
        index,
        replace(finding, rows_a=(replace(finding.rows_a[0]), *finding.rows_a[1:])),
    )
    with pytest.raises(AssertionError, match="source object was replaced"):
        verify_comparison_result(a, b, altered, report, truth)


def _write_report(rows):
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerows(rows)
    return output.getvalue().encode("utf-8")


@pytest.mark.parametrize(
    "tamper",
    (
        "missing_exception",
        "duplicate_exception",
        "exception_order",
        "column_order",
        "category",
        "delta",
        "secondary_labels",
        "json_evidence",
        "status",
        "source_references",
    ),
)
def test_verifier_rejects_material_export_corruption(observed_comparisons, tamper):
    a, b, result, report, truth = observed_comparisons
    rows = list(csv.reader(StringIO(report.decode("utf-8"), newline="")))
    if tamper == "missing_exception":
        rows.pop()
    elif tamper == "duplicate_exception":
        rows.append(rows[-1])
    elif tamper == "exception_order":
        rows[1], rows[2] = rows[2], rows[1]
    elif tamper == "column_order":
        for row in rows:
            row[8], row[9] = row[9], row[8]
    elif tamper == "category":
        rows[1][0] = "Amount mismatch"
    elif tamper == "delta":
        rows[1][4] = "+123.00"
    elif tamper == "secondary_labels":
        rows[1][7] = ""
    elif tamper == "json_evidence":
        rows[1][8] = '["fabricated"]'
    elif tamper == "status":
        rows[1][10] = "not_comparable"
    else:
        rows[1][5] = "999999"
    with pytest.raises(AssertionError, match="structured export"):
        verify_comparison_result(a, b, result, _write_report(rows), truth)


def test_verifier_requires_repeat_export_byte_identity(observed_comparisons, monkeypatch):
    monkeypatch.setattr(benchmark, "export_exceptions_csv", lambda result: b"changed")
    with pytest.raises(AssertionError, match="Repeated complete exports"):
        verify_comparison_result(*observed_comparisons)


def test_comparison_cli_discloses_configuration_phases_and_python_allocation_method(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark",
            "--groups",
            "100",
            "--tolerances",
            "0.01",
            "--workload",
            "comparison",
            "--memory",
        ],
    )
    benchmark.main()
    output = capsys.readouterr().out
    assert "workload=comparison" in output
    assert "Comparison config:" in output
    assert all(name in output for mapping in COMPARISON_MAPPINGS for name in mapping)
    assert "untraced single passes" in output
    assert "gen_s ingest_s reconcile_s export_s verify_s total_s peak_MiB" in output
    assert "Python allocations in a separate tracemalloc pass, not process RSS" in output
    assert "Verified 100 unique:" in output
    assert "Verified 100 grouped_by_key:" in output
