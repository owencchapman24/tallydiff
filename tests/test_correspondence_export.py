"""Stored correspondence evidence preserves parent accounting and CSV compatibility."""

import csv
import json
from decimal import Decimal, localcontext
from io import StringIO

import pytest

import tallydiff.engine as engine
import tallydiff.normalization as normalization
import tallydiff.subset_matching as subset_matching
from tallydiff import (
    ComparisonFieldMapping,
    CorrespondenceReason,
    CorrespondenceStatus,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    export_exceptions_csv,
    reconcile,
)
from tallydiff.presentation import CORRESPONDENCE_STATUS_LABELS, finding_rows

MODE = ReconciliationMode.BOUNDED_ONE_TO_MANY
S = CorrespondenceStatus
R = CorrespondenceReason
BASE_COLUMNS = [
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
]
CORRESPONDENCE_COLUMNS = ["Correspondence status", "Correspondence evidence"]
EVIDENCE_FIELDS = (
    "schema_version",
    "policy",
    "matching_key",
    "status",
    "reason",
    "anchor",
    "accepted_solution",
    "unassigned_rows_a",
    "unassigned_rows_b",
    "ambiguity_witnesses",
    "planned_combinations",
    "examined_combinations",
    "reserved_combinations",
    "search_complete",
)
POLICY_SNAPSHOT = {
    "policy_id": "exact_unique_v1",
    "max_candidate_rows": 12,
    "minimum_accepted_subset_rows": 2,
    "max_planned_combinations_per_run": 1_000_000,
    "max_ambiguity_witnesses": 2,
    "exact_amounts_only": True,
    "physical_row_uniqueness": True,
    "singleton_rivals_count": True,
}
COMPARISONS = (
    ComparisonFieldMapping("department", "cost_center"),
    ComparisonFieldMapping("currency", "currency_code"),
)


def _record(source, ordinal, amount, key="INV", value="Sales"):
    return SourceRecord(
        source,
        ordinal,
        (key,),
        Decimal(amount),
        {
            "department": value,
            "cost_center": value,
            "currency": "USD",
            "currency_code": "USD",
            "private_note": "SECRET_RAW_VALUE",
        },
    )


def _ledger(specifications):
    return tuple(
        tuple(
            _record(source, ordinal, amount, key)
            for ordinal, (key, amount) in enumerate(
                (
                    (key, amount)
                    for key, a, b in specifications
                    for amount in (a if source is Source.A else b)
                ),
                start=2,
            )
        )
        for source in Source
    )


def _group(a, b, source=Source.A, key="INV"):
    return _ledger([(key, a, b) if source is Source.A else (key, b, a)])


def _read(data, columns=None):
    reader = csv.DictReader(StringIO(data.decode("utf-8"), newline=""))
    assert reader.fieldnames == (
        BASE_COLUMNS + CORRESPONDENCE_COLUMNS if columns is None else columns
    )
    rows = list(reader)
    assert all(None not in row and None not in row.values() for row in rows)
    return rows


def _reference(row):
    return {
        "source": row.source.value,
        "source_row": row.source_row,
        "amount": format(row.amount, "f"),
        "original_key": list(row.key),
    }


def _solution(solution):
    return {
        "amount": format(solution.amount, "f"),
        "rows_a": [_reference(row) for row in solution.rows_a],
        "rows_b": [_reference(row) for row in solution.rows_b],
    }


@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize(
    "a,b,status,reason,counters,complete",
    [
        (("300.00",), ("100.00", "200.00"), S.UNIQUE_EXACT, None, (3, 3, 3), True),
        (("300.00",), ("100.00", "200.00", "7.00"), S.UNIQUE_EXACT, None, (7, 7, 7), True),
        (("300",), ("300", "100", "200"), S.AMBIGUOUS, None, (7, 6, 7), False),
        (("300",), ("100", "200", "0"), S.AMBIGUOUS, None, (7, 7, 7), True),
        (("300",), ("100", "199.99"), S.NO_EXACT_SUBSET, None, (3, 3, 3), True),
        (("300",), ("300", "1"), S.SINGLETON_ONLY, None, (3, 3, 3), True),
        (("13",), ("1",) * 13, S.BOUND_EXCEEDED, R.CANDIDATE_ROW_LIMIT, (0, 0, 0), False),
        (("300", "2"), ("100", "202"), S.NOT_ELIGIBLE, R.BOTH_SIDES_MULTIPLE, (0, 0, 0), False),
        (("100", "200"), (), S.NOT_ELIGIBLE, R.MISSING_OPPOSITE_SIDE, (0, 0, 0), False),
    ],
)
def test_export_all_statuses_preserves_stored_solutions_residuals_witnesses_and_counters(
    source, a, b, status, reason, counters, complete
):
    rows = _group(a, b, source)
    result = reconcile(*rows, mode=MODE)
    finding = result.findings[0]
    analysis = finding.correspondence_analysis
    data = export_exceptions_csv(result)
    report = _read(data)[0]
    evidence = json.loads(report["Correspondence evidence"])
    assert tuple(evidence) == EVIDENCE_FIELDS
    assert evidence["schema_version"] == 1
    assert tuple(evidence["policy"]) == tuple(POLICY_SNAPSHOT)
    assert evidence["policy"] == POLICY_SNAPSHOT
    assert report["Correspondence status"] == evidence["status"] == status.value
    assert evidence["reason"] == (reason.value if reason is not None else None)
    assert evidence["matching_key"] == ["INV"]
    parent_anchor = (
        finding.rows_a[0]
        if len(finding.rows_a) == 1 and len(finding.rows_b) > 1
        else finding.rows_b[0]
        if len(finding.rows_b) == 1 and len(finding.rows_a) > 1
        else None
    )
    assert evidence["anchor"] == (_reference(parent_anchor) if parent_anchor is not None else None)
    assert evidence["accepted_solution"] == (
        _solution(analysis.accepted_solution) if analysis.accepted_solution is not None else None
    )
    assert evidence["unassigned_rows_a"] == [_reference(row) for row in analysis.unassigned_rows_a]
    assert evidence["unassigned_rows_b"] == [_reference(row) for row in analysis.unassigned_rows_b]
    assert evidence["ambiguity_witnesses"] == [
        _solution(witness) for witness in analysis.ambiguity_witnesses
    ]
    assert tuple(evidence[name] for name in EVIDENCE_FIELDS[-4:-1]) == counters
    assert all(type(evidence[name]) is int for name in EVIDENCE_FIELDS[-4:-1])
    assert evidence["search_complete"] is complete
    assert report["Correspondence evidence"] == json.dumps(
        evidence, ensure_ascii=False, separators=(",", ":")
    )
    assert "private_note" not in data.decode() and "SECRET_RAW_VALUE" not in data.decode()
    assert data == export_exceptions_csv(result)
    assert not data.startswith(b"\xef\xbb\xbf") and data.endswith(b"\r\n")
    assert (
        finding_rows(result.findings)[0]["Correspondence status"]
        == CORRESPONDENCE_STATUS_LABELS[status]
    )
    if status is S.UNIQUE_EXACT:
        assert evidence["accepted_solution"] is not None and evidence["ambiguity_witnesses"] == []
    else:
        assert evidence["accepted_solution"] is None
        assert evidence["unassigned_rows_a"] == [_reference(row) for row in finding.rows_a]
        assert evidence["unassigned_rows_b"] == [_reference(row) for row in finding.rows_b]
    if source is Source.B and status in (S.SINGLETON_ONLY, S.AMBIGUOUS) and b[0] == "300":
        assert analysis.ambiguity_witnesses[0].anchor.source is Source.A
        assert evidence["anchor"]["source"] == "B"


def test_unique_residual_evidence_has_exact_documented_schema_snapshot():
    result = reconcile(*_group(("300.00",), ("100.00", "200.00", "7.00")), mode=MODE)
    expected = {
        "schema_version": 1,
        "policy": POLICY_SNAPSHOT,
        "matching_key": ["INV"],
        "status": "unique_exact",
        "reason": None,
        "anchor": {"source": "A", "source_row": 2, "amount": "300.00", "original_key": ["INV"]},
        "accepted_solution": {
            "amount": "300.00",
            "rows_a": [
                {"source": "A", "source_row": 2, "amount": "300.00", "original_key": ["INV"]}
            ],
            "rows_b": [
                {"source": "B", "source_row": 2, "amount": "100.00", "original_key": ["INV"]},
                {"source": "B", "source_row": 3, "amount": "200.00", "original_key": ["INV"]},
            ],
        },
        "unassigned_rows_a": [],
        "unassigned_rows_b": [
            {"source": "B", "source_row": 4, "amount": "7.00", "original_key": ["INV"]}
        ],
        "ambiguity_witnesses": [],
        "planned_combinations": 7,
        "examined_combinations": 7,
        "reserved_combinations": 7,
        "search_complete": True,
    }
    cell = _read(export_exceptions_csv(result))[0]["Correspondence evidence"]
    assert cell == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))


def test_run_budget_bound_exports_planned_but_unreserved_work_and_parent_anchor():
    specifications = [(f"K{index:03d}", ("0",), ("0",) * 12) for index in range(244)]
    specifications += [("Z_BOUND", ("300",), ("100", "200") * 6)]
    result = reconcile(*_ledger(specifications), mode=MODE)
    evidence = json.loads(_read(export_exceptions_csv(result))[-1]["Correspondence evidence"])
    assert evidence["status"] == "bound_exceeded"
    assert evidence["reason"] == "run_budget_exhausted"
    assert evidence["planned_combinations"] == 4095
    assert evidence["examined_combinations"] == evidence["reserved_combinations"] == 0
    assert evidence["search_complete"] is False
    assert evidence["anchor"] == _reference(result.findings[-1].rows_a[0])
    assert evidence["accepted_solution"] is None and evidence["ambiguity_witnesses"] == []


@pytest.mark.parametrize("comparisons", [(), COMPARISONS])
def test_mixed_inferred_and_ordinary_export_keeps_categories_and_secondary_column_order(
    comparisons,
):
    rows = _ledger(
        [
            ("A_EXACT", ("300",), ("100", "200")),
            ("B_RESIDUAL", ("300",), ("100", "200", "7")),
            ("C_MISMATCH", ("10",), ("12",)),
            ("D_TOLERATED", ("10",), ("10.01",)),
        ]
    )
    result = reconcile(
        *rows, mode=MODE, amount_tolerance=Decimal("0.01"), comparison_fields=comparisons
    )
    columns = BASE_COLUMNS.copy()
    if comparisons:
        columns += [
            "Secondary differences",
            "Comparison 1 File A values — department",
            "Comparison 1 File B values — cost_center",
            "Comparison 1 status",
            "Comparison 2 File A values — currency",
            "Comparison 2 File B values — currency_code",
            "Comparison 2 status",
        ]
    columns += CORRESPONDENCE_COLUMNS
    reports = _read(export_exceptions_csv(result), columns)
    assert [row["Matching key"] for row in reports] == ["A_EXACT", "B_RESIDUAL", "C_MISMATCH"]
    assert [row["Category"] for row in reports] == [
        "Exact match",
        "Amount mismatch",
        "Amount mismatch",
    ]
    assert reports[2]["Correspondence status"] == reports[2]["Correspondence evidence"] == ""
    if comparisons:
        for row in reports[:2]:
            assert row["Secondary differences"] == ""
            assert row["Comparison 1 status"] == row["Comparison 2 status"] == "not_comparable"
            assert row["Comparison 1 File A values — department"] == '["Sales"]'
            assert row["Comparison 1 File B values — cost_center"] == '["Sales"]'
            assert row["Comparison 2 File A values — currency"] == '["USD"]'
            assert row["Comparison 2 File B values — currency_code"] == '["USD"]'
        assert reports[2]["Comparison 1 status"] == reports[2]["Comparison 2 status"] == "match"


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_ordinary_exceptions_preserve_frozen_seven_column_bytes_in_all_modes(mode):
    rows = _ledger([("INV", ("300.00",), ("301.00",))])
    expected = (
        b"Category,Matching key,File A amount,File B amount,Delta (A - B),"
        b"File A records,File B records\r\n"
        b"Amount mismatch,INV,300.00,301.00,-1.00,2,2\r\n"
    )
    assert export_exceptions_csv(reconcile(*rows, mode=mode)) == expected
    assert (
        export_exceptions_csv(reconcile((), (), mode=mode)) == expected.split(b"\r\n")[0] + b"\r\n"
    )


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_ordinary_secondary_exception_preserves_frozen_csv_bytes_in_all_modes(mode):
    a = (_record(Source.A, 2, "300.00", value="Sales"),)
    b = (_record(Source.B, 2, "301.00", value="Finance"),)
    expected = (
        "Category,Matching key,File A amount,File B amount,Delta (A - B),"
        "File A records,File B records,"
        "Secondary differences,Comparison 1 File A values — department,"
        "Comparison 1 File B values — cost_center,Comparison 1 status\r\n"
        "Amount mismatch,INV,300.00,301.00,-1.00,2,2,Comparison 1: department ↔ cost_center,"
        '"[""Sales""]","[""Finance""]",mismatch\r\n'
    ).encode()
    assert (
        export_exceptions_csv(reconcile(a, b, mode=mode, comparison_fields=(COMPARISONS[0],)))
        == expected
    )


def test_columns_depend_on_exported_exceptions_even_if_other_findings_have_analysis(monkeypatch):
    rows = _ledger([("A_INFERRED", ("300",), ("100", "200")), ("INV", ("300.00",), ("301.00",))])
    result = reconcile(*rows, mode=MODE)
    ordinary = result.findings[-1]
    monkeypatch.setattr(ReconciliationResult, "exceptions", property(lambda self: (ordinary,)))
    assert _read(export_exceptions_csv(result), BASE_COLUMNS) == [
        {
            "Category": "Amount mismatch",
            "Matching key": "INV",
            "File A amount": "300.00",
            "File B amount": "301.00",
            "Delta (A - B)": "-1.00",
            "File A records": "3",
            "File B records": "4",
        }
    ]


@pytest.mark.parametrize(
    "amount,candidates",
    [
        ("-300.000", ("-100.00", "-200.000")),
        ("-0.000", ("1.000", "-1.000")),
        ("100.000", ("150.000", "-50.000")),
    ],
)
def test_evidence_preserves_exact_negative_and_zero_amount_strings(amount, candidates):
    result = reconcile(*_group((amount,), candidates), mode=MODE)
    with localcontext() as context:
        context.prec = 1
        evidence = json.loads(_read(export_exceptions_csv(result))[0]["Correspondence evidence"])
    assert evidence["anchor"]["amount"] == evidence["accepted_solution"]["amount"] == amount
    assert [row["amount"] for row in evidence["accepted_solution"]["rows_b"]] == list(candidates)
    assert all(type(row["amount"]) is str for row in evidence["accepted_solution"]["rows_b"])


def test_unicode_csv_quoting_normalized_matching_key_and_original_keys_remain_auditable():
    key_a = ' CAFÉ,"Nord"\nLine '
    key_b = ' café,"nord"\nline '
    a = (_record(Source.A, 2, "300.00", key_a),)
    b = (_record(Source.B, 3, "200.00", key_b), _record(Source.B, 2, "100.00", key_b))
    config = KeyNormalizationConfig((KeyNormalizationRules(casefold=True),))
    result = reconcile(a, b, mode=MODE, key_normalization=config)
    data = export_exceptions_csv(result)
    row = _read(data)[0]
    evidence = json.loads(row["Correspondence evidence"])
    assert row["Matching key"] == key_b
    assert evidence["matching_key"] == [key_b]
    assert evidence["anchor"]["original_key"] == [key_a]
    assert [reference["original_key"] for reference in evidence["accepted_solution"]["rows_b"]] == [
        [key_b],
        [key_b],
    ]
    assert [reference["source_row"] for reference in evidence["accepted_solution"]["rows_b"]] == [
        2,
        3,
    ]
    assert "café" in data.decode("utf-8") and "\\u00e9" not in row["Correspondence evidence"]
    assert (
        export_exceptions_csv(reconcile(a, reversed(b), mode=MODE, key_normalization=config))
        == data
    )


def test_completed_result_exports_only_stored_evidence_after_processing_is_disabled(monkeypatch):
    rows = _ledger(
        [
            ("A_UNIQUE", ("300",), ("100", "200", "7")),
            ("B_AMBIGUOUS", ("300",), ("300", "100", "200")),
        ]
    )
    result = reconcile(*rows, mode=MODE, comparison_fields=COMPARISONS)
    expected = export_exceptions_csv(result)

    def forbidden(*args, **kwargs):
        pytest.fail("export must consume stored evidence without repeating processing")

    monkeypatch.setattr(engine, "reconcile", forbidden)
    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", forbidden)
    monkeypatch.setattr(subset_matching, "analyze_bounded_one_to_many", forbidden)
    monkeypatch.setattr(engine, "normalize_key", forbidden)
    monkeypatch.setattr(normalization, "normalize_key", forbidden)
    monkeypatch.setattr(engine, "_compare_fields", forbidden)
    assert export_exceptions_csv(result) == export_exceptions_csv(result) == expected
