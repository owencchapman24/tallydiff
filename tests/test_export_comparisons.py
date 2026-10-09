"""Complete secondary exception exports preserve structured evidence and legacy bytes."""

import csv
import json
from dataclasses import replace
from decimal import Decimal, localcontext
from io import StringIO

import pytest

from tallydiff import (
    ComparisonFieldMapping,
    FieldComparison,
    FieldComparisonStatus,
    FindingCategory,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    export_exceptions_csv,
    reconcile,
)

DEPARTMENT = ComparisonFieldMapping("Department", "Cost Center")
CURRENCY = ComparisonFieldMapping("Currency", "Currency Code")
DATE = ComparisonFieldMapping("Posting Date", "Document Date")
BASE_COLUMNS = [
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
]
DEPARTMENT_COLUMNS = BASE_COLUMNS + [
    "Secondary differences",
    "Comparison 1 File A values — Department",
    "Comparison 1 File B values — Cost Center",
    "Comparison 1 status",
]


def _record(source, row, key="INV", amount="1.00", value="Sales", extra=None):
    column = "Department" if source is Source.A else "Cost Center"
    return SourceRecord(source, row, (key,), Decimal(amount), {column: value, **(extra or {})})


def _read(data, columns=DEPARTMENT_COLUMNS):
    reader = csv.DictReader(StringIO(data.decode("utf-8"), newline=""))
    assert reader.fieldnames == columns
    rows = list(reader)
    assert all(None not in row and None not in row.values() for row in rows)
    return rows


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    "amount_b,category,delta",
    [
        ("1.00", "Exact match", "0.00"),
        ("1.01", "Within tolerance", "-0.01"),
        ("2.00", "Amount mismatch", "-1.00"),
    ],
)
@pytest.mark.parametrize("mismatch", [False, True])
def test_export_membership_preserves_primary_category_and_true_delta(
    mode, amount_b, category, delta, mismatch
):
    a = (_record(Source.A, 2),)
    b = (_record(Source.B, 7, amount=amount_b, value="Marketing" if mismatch else "Sales"),)
    result = reconcile(
        a, b, mode=mode, amount_tolerance=Decimal("0.01"), comparison_fields=(DEPARTMENT,)
    )
    data = export_exceptions_csv(result)
    rows = _read(data)
    assert data == export_exceptions_csv(result)
    assert data.endswith(b"\r\n") and not data.startswith(b"\xef\xbb\xbf")
    requires_review = mismatch or category == "Amount mismatch"
    assert len(rows) == len(result.exceptions) == int(requires_review)
    if requires_review:
        assert rows[0] == {
            "Category": category,
            "Matching key": "INV",
            "File A amount": "1.00",
            "File B amount": amount_b,
            "Delta (A - B)": delta,
            "File A records": "2",
            "File B records": "7",
            "Secondary differences": "Comparison 1: Department ↔ Cost Center" if mismatch else "",
            "Comparison 1 File A values — Department": '["Sales"]',
            "Comparison 1 File B values — Cost Center": '["Marketing"]'
            if mismatch
            else '["Sales"]',
            "Comparison 1 status": "mismatch" if mismatch else "match",
        }
    assert result.tolerated_findings == (
        (result.findings[0],) if category == "Within tolerance" and not mismatch else ()
    )
    assert result.tolerated_delta_total == Decimal(
        "-0.01" if category == "Within tolerance" and not mismatch else "0"
    )


@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize(
    "mode,values",
    [
        (ReconciliationMode.UNIQUE, [""]),
        (ReconciliationMode.UNIQUE, ["Sales"]),
        (ReconciliationMode.GROUPED_BY_KEY, ["Sales", "Sales", "Marketing", ""]),
    ],
)
def test_one_sided_export_retains_distinct_summaries_and_empty_array(source, mode, values):
    records = tuple(
        _record(source, row, value=value) for row, value in zip([9, 2, 7, 4], values, strict=False)
    )
    result = reconcile(
        records if source is Source.A else (),
        records if source is Source.B else (),
        mode=mode,
        comparison_fields=(DEPARTMENT,),
    )
    row = _read(export_exceptions_csv(result))[0]
    assert row["Category"] == ("File A only" if source is Source.A else "File B only")
    assert row["Comparison 1 status"] == "not_comparable"
    assert row["Secondary differences"] == ""
    assert json.loads(row["Comparison 1 File A values — Department"]) == (
        sorted(set(values)) if source is Source.A else []
    )
    assert json.loads(row["Comparison 1 File B values — Cost Center"]) == (
        sorted(set(values)) if source is Source.B else []
    )
    expected_records = "; ".join(
        str(r.source_row) for r in sorted(records, key=lambda r: r.source_row)
    )
    assert row[f"File {source.value} records"] == expected_records
    assert row["File B records" if source is Source.A else "File A records"] == ""


@pytest.mark.parametrize(
    "values_a,values_b",
    [
        (["Sales", "Sales"], ["Sales"]),
        (["Sales"], ["Sales", "Marketing"]),
        (["Sales", "", "Marketing"], ["Marketing", "Sales"]),
        (["Sales", "Sales"], []),
        ([], ["Sales", ""]),
    ],
)
def test_unique_duplicate_export_keeps_all_source_rows_and_not_comparable_summaries(
    values_a, values_b
):
    a = tuple(_record(Source.A, row, value=value) for row, value in enumerate(values_a, start=2))
    b = tuple(_record(Source.B, row, value=value) for row, value in enumerate(values_b, start=8))
    result = reconcile(reversed(a), reversed(b), comparison_fields=(DEPARTMENT,))
    rows = _read(export_exceptions_csv(result))
    assert len(rows) == 1
    row = rows[0]
    assert row["Category"] == "Duplicate / ambiguous"
    assert row["Secondary differences"] == ""
    assert row["Comparison 1 status"] == "not_comparable"
    assert json.loads(row["Comparison 1 File A values — Department"]) == sorted(set(values_a))
    assert json.loads(row["Comparison 1 File B values — Cost Center"]) == sorted(set(values_b))
    assert row["File A records"] == "; ".join(str(r.source_row) for r in a)
    assert row["File B records"] == "; ".join(str(r.source_row) for r in b)


@pytest.mark.parametrize(
    "values_b,status", [(["Sales"], "match"), (["Sales", "Marketing"], "mismatch")]
)
def test_grouped_export_uses_stored_distinct_sets_independent_of_occurrence_counts(
    values_b, status
):
    a = (_record(Source.A, 8), _record(Source.A, 2))
    b = tuple(
        _record(Source.B, row, value=value, amount="0.50")
        for row, value in enumerate(values_b, start=3)
    )
    result = reconcile(
        a, b, mode=ReconciliationMode.GROUPED_BY_KEY, comparison_fields=(DEPARTMENT,)
    )
    row = _read(export_exceptions_csv(result))[0]
    assert row["Category"] == "Amount mismatch"
    assert row["Comparison 1 status"] == status
    assert json.loads(row["Comparison 1 File A values — Department"]) == ["Sales"]
    assert json.loads(row["Comparison 1 File B values — Cost Center"]) == sorted(set(values_b))
    assert row["File A records"] == "2; 8"


@pytest.mark.parametrize(
    "values",
    [
        ("",),
        ("comma,cell",),
        ('quote"cell',),
        ("Café", "e\u0301", "é", "🧾"),
        (
            "line\nfeed",
            "carriage\rreturn",
            "both\r\nlines",
            "tab\tvalue",
            "nul\0value",
            "control\x01",
        ),
        ("=1+1", "+00123", "-00123", "@SUM(1,2)", "#N/A"),
        (";", " / ", "[a,b]", '["x"]'),
        ("x" * 10_000,),
    ],
)
def test_json_cells_round_trip_exact_evidence_without_truncation_or_rewriting(values):
    values = tuple(sorted(set(values)))
    a = tuple(_record(Source.A, row, value=value) for row, value in enumerate(values, start=2))
    b = (_record(Source.B, 9, value="unlike"),)
    result = reconcile(
        a, b, mode=ReconciliationMode.GROUPED_BY_KEY, comparison_fields=(DEPARTMENT,)
    )
    data = export_exceptions_csv(result)
    row = _read(data)[0]
    assert tuple(json.loads(row["Comparison 1 File A values — Department"])) == values
    assert json.loads(row["Comparison 1 File B values — Cost Center"]) == ["unlike"]
    assert row["Comparison 1 status"] == "mismatch"
    assert data == export_exceptions_csv(result)
    for value in values:
        if any(ord(char) > 127 for char in value):
            assert value.encode("utf-8") in data
    assert tuple(record.raw_fields["Department"] for record in a) == values


def test_dynamic_headers_and_mismatch_ordinals_follow_configuration_order():
    fields = (CURRENCY, DATE, DEPARTMENT)
    a = (_record(Source.A, 2, extra={"Currency": "USD", "Posting Date": "2026-01-01"}),)
    b = (
        _record(
            Source.B,
            3,
            value="Marketing",
            extra={"Currency Code": "EUR", "Document Date": "2026-01-01"},
        ),
    )
    result = reconcile(a, b, comparison_fields=fields)
    columns = BASE_COLUMNS + [
        "Secondary differences",
        "Comparison 1 File A values — Currency",
        "Comparison 1 File B values — Currency Code",
        "Comparison 1 status",
        "Comparison 2 File A values — Posting Date",
        "Comparison 2 File B values — Document Date",
        "Comparison 2 status",
        "Comparison 3 File A values — Department",
        "Comparison 3 File B values — Cost Center",
        "Comparison 3 status",
    ]
    row = _read(export_exceptions_csv(result), columns)[0]
    assert (
        row["Secondary differences"]
        == "Comparison 1: Currency ↔ Currency Code; Comparison 3: Department ↔ Cost Center"
    )
    assert [row[f"Comparison {i} status"] for i in range(1, 4)] == ["mismatch", "match", "mismatch"]
    assert [json.loads(row[column]) for column in columns if " values — " in column] == [
        ["USD"],
        ["EUR"],
        ["2026-01-01"],
        ["2026-01-01"],
        ["Sales"],
        ["Marketing"],
    ]


def test_unusual_column_names_are_exact_with_static_header_and_summary_prefixes():
    mapping = ComparisonFieldMapping('=SUM(1,2)\r\n"dept"', " @Cost, center\t ")
    a = (SourceRecord(Source.A, 2, ("=1+1",), Decimal("1"), {mapping.file_a: "=A1"}),)
    b = (SourceRecord(Source.B, 3, ("=1+1",), Decimal("1"), {mapping.file_b: "@B1"}),)
    result = reconcile(a, b, comparison_fields=(mapping,))
    columns = BASE_COLUMNS + [
        "Secondary differences",
        f"Comparison 1 File A values — {mapping.file_a}",
        f"Comparison 1 File B values — {mapping.file_b}",
        "Comparison 1 status",
    ]
    row = _read(export_exceptions_csv(result), columns)[0]
    assert row["Matching key"] == "=1+1"
    assert row["Secondary differences"] == f"Comparison 1: {mapping.file_a} ↔ {mapping.file_b}"
    assert json.loads(row[columns[8]]) == ["=A1"]
    assert json.loads(row[columns[9]]) == ["@B1"]


def test_header_only_result_uses_configured_fields_without_inferring_from_findings():
    fields = (CURRENCY, DEPARTMENT)
    result = reconcile((), (), comparison_fields=fields)
    expected = BASE_COLUMNS + [
        "Secondary differences",
        "Comparison 1 File A values — Currency",
        "Comparison 1 File B values — Currency Code",
        "Comparison 1 status",
        "Comparison 2 File A values — Department",
        "Comparison 2 File B values — Cost Center",
        "Comparison 2 status",
    ]
    data = export_exceptions_csv(result)
    assert _read(data, expected) == []
    assert data.decode("utf-8") == ",".join(expected) + "\r\n"


def test_export_uses_structured_comparisons_without_requiring_or_recomputing_raw_fields():
    a = SourceRecord(Source.A, 2, ("INV",), Decimal("1"))
    b = SourceRecord(Source.B, 7, ("INV",), Decimal("1"))
    comparison = FieldComparison(
        DEPARTMENT, ("Stored A",), ("Stored B",), FieldComparisonStatus.MISMATCH
    )
    finding = ReconciliationFinding(
        FindingCategory.EXACT_MATCH,
        ("INV",),
        (a,),
        (b,),
        a.amount,
        b.amount,
        field_comparisons=(comparison,),
    )
    result = ReconciliationResult(a.amount, b.amount, (finding,), comparison_fields=(DEPARTMENT,))
    row = _read(export_exceptions_csv(result))[0]
    assert json.loads(row["Comparison 1 File A values — Department"]) == ["Stored A"]
    assert json.loads(row["Comparison 1 File B values — Cost Center"]) == ["Stored B"]
    assert finding.rows_a[0] is a and finding.rows_b[0] is b


def test_export_preserves_result_finding_order_and_repeat_call_bytes():
    a = tuple(_record(Source.A, row, key) for row, key in [(8, "C"), (3, "A"), (2, "B")])
    b = tuple(
        _record(Source.B, row, key, value="Marketing")
        for row, key in [(9, "B"), (4, "C"), (2, "A")]
    )
    result = reconcile(a, b, comparison_fields=(DEPARTMENT,))
    data = export_exceptions_csv(result)
    assert data == export_exceptions_csv(
        reconcile(reversed(a), reversed(b), comparison_fields=(DEPARTMENT,))
    )
    reversed_result = replace(result, findings=tuple(reversed(result.findings)))
    assert [row["Matching key"] for row in _read(export_exceptions_csv(reversed_result))] == [
        "C",
        "B",
        "A",
    ]


def test_configured_export_preserves_decimal_text_under_hostile_decimal_context():
    amount_a = "1000000000000000000000000.0000000000000000000000001"
    amount_b = "1000000000000000000000000.0000000000000000000000002"
    a, b = (
        (_record(Source.A, 2, amount=amount_a),),
        (_record(Source.B, 7, amount=amount_b, value="Marketing"),),
    )
    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        result = reconcile(a, b, amount_tolerance=Decimal("1E-25"), comparison_fields=(DEPARTMENT,))
        row = _read(export_exceptions_csv(result))[0]
        assert row["Category"] == "Within tolerance"
        assert row["File A amount"] == amount_a
        assert row["File B amount"] == amount_b
        assert row["Delta (A - B)"] == "-0.0000000000000000000000001"
        assert result.tolerated_findings == ()
        assert not any(context.flags.values())


def test_zero_comparisons_preserve_exact_seven_column_csv_bytes():
    a = (_record(Source.A, 7, '=1+2",x\r\nnext', "-0.1200"), _record(Source.A, 2, "INV", "100.00"))
    b = (_record(Source.B, 9, "INV", "101.00"),)
    expected = (
        b"Category,Matching key,File A amount,File B amount,Delta (A - B),"
        b"File A records,File B records\r\n"
        b'File A only,"=1+2"",x\r\nnext",-0.1200,,-0.1200,7,\r\n'
        b"Amount mismatch,INV,100.00,101.00,-1.00,2,9\r\n"
    )
    assert export_exceptions_csv(reconcile(a, b)) == expected
    assert export_exceptions_csv(reconcile(a, b, comparison_fields=())) == expected
