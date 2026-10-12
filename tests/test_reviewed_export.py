"""Reviewed CSV appends workflow annotations without changing stored evidence."""

import csv
import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from importlib.metadata import version
from io import StringIO

import pytest

import tallydiff
import tallydiff.engine as engine
import tallydiff.export as export_module
import tallydiff.ingest as ingestion
import tallydiff.normalization as normalization
import tallydiff.subset_matching as subset_matching
import tallydiff.xlsx as xlsx
from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    CorrespondenceReason,
    CorrespondenceStatus,
    FieldComparison,
    FieldComparisonStatus,
    FindingCategory,
    FindingReview,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    ReviewBinding,
    ReviewDisposition,
    ReviewSession,
    ReviewSessionError,
    Source,
    SourceRecord,
    bind_review_result,
    export_exceptions_csv,
    export_review_session,
    export_reviewed_exceptions_csv,
    load_review_session,
    reconcile,
)

BASE_COLUMNS = [
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
]
REVIEW_COLUMNS = ["Review disposition", "Review note"]
COMPARISONS = (
    ComparisonFieldMapping("department", "cost_center"),
    ComparisonFieldMapping("currency", "currency_code"),
)
MODE = ReconciliationMode.BOUNDED_ONE_TO_MANY
LABELS = (
    (ReviewDisposition.UNREVIEWED, "Unreviewed"),
    (ReviewDisposition.NEEDS_FOLLOW_UP, "Needs follow-up"),
    (ReviewDisposition.REVIEW_COMPLETE, "Review complete"),
)

# Captured from the untouched Stage 3 exporter at 7e2342e, before factoring its
# column/row construction. These fixed SHA-256 digests are not derived at test time.
ORIGINAL_BYTE_DIGESTS = {
    "plain_unique": "32dc95f31d3a471d4a41a780dfdd1b4d6dba860c408b971d1dea538391bf9311",
    "comparisons": "374cc6dc4f66744611e600235757b994d9d002db53d9f06865f6a32194a06959",
    "correspondence": "c40f115d91449c71922cc586e79bebb8186811ec7ac5003754c5e7563b48a76c",
    "mixed": "d64cd712073e05f40f8782c375e7c7a5f9b7f28d256419f7e96f32e147cc0560",
    "zero": "6b9b294a0065ece2e2ff95161bfc861fdf7740015ab5c1345ae4558999120117",
    "zero_comparisons": "cb56bfcb34e4b09462b96118d81bca627e614892ff99b2037381e42c3e3c7d1b",
}


def _ledger(specifications, *, value_a="Café", value_b="财务"):
    ledgers = []
    for source in Source:
        records = []
        for key, amounts_a, amounts_b in specifications:
            key = (key,) if isinstance(key, str) else key
            for amount in amounts_a if source is Source.A else amounts_b:
                value = value_a if source is Source.A else value_b
                raw = {
                    **{f"id{index}": component for index, component in enumerate(key, 1)},
                    "amount": amount,
                    "department": value,
                    "cost_center": value,
                    "currency": "USD",
                    "currency_code": "USD",
                    "private_note": "Original raw evidence\r\ne\u0301 🧾",
                }
                records.append(SourceRecord(source, len(records) + 2, key, Decimal(amount), raw))
        ledgers.append(tuple(records))
    return tuple(ledgers)


def _result(specifications=(("K", ("3",), ("2",)),), **kwargs):
    return reconcile(*_ledger(specifications), **kwargs)


def _bind(result=None, **kwargs):
    result = _result() if result is None else result
    arity = len(result.findings[0].key) if result.findings else 1
    options = dict(
        data_a=b"complete input A",
        data_b=b"complete input B",
        mapping=ColumnMapping(
            tuple((f"id{index}", f"id{index}") for index in range(1, arity + 1)),
            "amount",
            "amount",
            comparison_fields=result.comparison_fields,
        ),
        format_a="csv",
        format_b="csv",
    )
    options.update(kwargs)
    return bind_review_result(result, **options)


def _read(data):
    return list(csv.reader(StringIO(data.decode("utf-8"), newline=""), strict=True))


def _assert_report(binding, session):
    """Check every original cell and the full population/order on every fixture."""

    original = _read(export_exceptions_csv(binding.result))
    data = export_reviewed_exceptions_csv(binding, session)
    reviewed = _read(data)
    assert reviewed[0] == original[0] + REVIEW_COLUMNS
    assert [row[:-2] for row in reviewed] == original
    assert len(reviewed) == len(binding.result.exceptions) + 1 == len(session.reviews) + 1
    assert all(len(row) == len(reviewed[0]) for row in reviewed)
    assert tuple(finding.key for finding in binding.result.exceptions) == binding.exception_keys
    assert tuple(review.matching_key for review in session.reviews) == binding.exception_keys
    assert data == export_reviewed_exceptions_csv(binding, session)
    assert not data.startswith(b"\xef\xbb\xbf") and data.endswith(b"\r\n")
    for row, review in zip(reviewed[1:], session.reviews, strict=True):
        assert row[-1] == ("" if review.note == "" else "Note: " + review.note)
    return reviewed


def _annotated(binding, disposition=ReviewDisposition.REVIEW_COMPLETE, note="Checked 🧾"):
    return ReviewSession(
        binding,
        tuple(FindingReview(key, disposition, note) for key in binding.exception_keys),
    )


def _baseline_result(name):
    if name == "plain_unique":
        return _result(
            [
                ("K", ("3.00",), ("2.00",)),
                ("A_ONLY", ("0.00",), ()),
                ("CLEAN", ("2",), ("2",)),
                ("TOLERATED", ("2",), ("1.99",)),
            ],
            amount_tolerance=Decimal("0.01"),
        )
    if name == "comparisons":
        return _result(
            [
                ("EXACT", ("3",), ("3",)),
                ("TOLERATED", ("3",), ("2.99",)),
                ("DUPLICATE", ("1", "2"), ("3",)),
                ("MISSING", ("1",), ()),
            ],
            comparison_fields=COMPARISONS,
            amount_tolerance=Decimal("0.01"),
        )
    if name == "correspondence":
        return _result([("K", ("300.00",), ("100.00", "200.00", "7.00"))], mode=MODE)
    if name == "mixed":
        return _result(
            [
                ("A_INFERRED", ("300",), ("100", "200")),
                ("B_MISMATCH", ("10",), ("12",)),
                ("C_ONLY", ("3",), ()),
                ("D_CLEAN", ("2",), ("2",)),
                ("TOLERATED", ("10",), ("10.01",)),
            ],
            mode=MODE,
            amount_tolerance=Decimal("0.01"),
        )
    if name == "zero":
        return _result(
            [("CLEAN", ("3.00",), ("3.00",)), ("TOLERATED", ("3",), ("2.99",))],
            amount_tolerance=Decimal("0.01"),
        )
    if name == "zero_comparisons":
        return _result((), comparison_fields=COMPARISONS)
    raise AssertionError(f"Unknown baseline fixture: {name}")


@pytest.mark.parametrize("name,digest", ORIGINAL_BYTE_DIGESTS.items())
def test_original_export_bytes_remain_frozen_after_private_factoring(name, digest):
    result = _baseline_result(name)
    original = export_exceptions_csv(result)
    assert hashlib.sha256(original).hexdigest() == digest
    binding = _bind(result)
    _assert_report(binding, _annotated(binding))
    assert export_exceptions_csv(result) == original


def test_basic_schema_default_reviews_and_crlf_bytes():
    binding = _bind(_baseline_result("plain_unique"))
    session = ReviewSession.unreviewed(binding)
    rows = _assert_report(binding, session)
    assert rows[0] == BASE_COLUMNS + REVIEW_COLUMNS
    assert [row[1] for row in rows[1:]] == ["A_ONLY", "K"]
    assert [row[-2:] for row in rows[1:]] == [["Unreviewed", ""], ["Unreviewed", ""]]
    data = export_reviewed_exceptions_csv(binding, session)
    assert data.count(b"\r\n") == 3
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert data.startswith((",".join(BASE_COLUMNS + REVIEW_COLUMNS) + "\r\n").encode())


@pytest.mark.parametrize("disposition,label", LABELS)
@pytest.mark.parametrize("name", ["plain_unique", "comparisons", "correspondence", "mixed"])
def test_dispositions_keep_every_exception_and_original_categories(name, disposition, label):
    binding = _bind(_baseline_result(name))
    session = _annotated(binding, disposition, "Follow up, then verify")
    rows = _assert_report(binding, session)
    assert all(row[-2:] == [label, "Note: Follow up, then verify"] for row in rows[1:])
    assert len(rows) - 1 == session.total == len(binding.result.exceptions)
    assert all(finding.is_exception for finding in binding.result.exceptions)
    if name == "mixed":
        assert rows[1][0] == "Exact match"
        assert binding.result.exceptions[0].has_inferred_correspondence
        assert [row[1] for row in rows[1:]] == ["A_INFERRED", "B_MISMATCH", "C_ONLY"]
        assert [finding.key for finding in binding.result.tolerated_findings] == [("TOLERATED",)]


def test_individual_review_updates_preserve_full_binding_order():
    binding = _bind(_result([(name, ("3",), ("2",)) for name in ("C", "A", "B")]))
    session = ReviewSession.unreviewed(binding)
    session = session.with_review(
        FindingReview(("B",), ReviewDisposition.NEEDS_FOLLOW_UP, "Need the source record")
    ).with_review(FindingReview(("C",), ReviewDisposition.REVIEW_COMPLETE, "Checked"))
    rows = _assert_report(binding, session)
    assert [row[1] for row in rows[1:]] == ["A", "B", "C"]
    assert [row[-2:] for row in rows[1:]] == [
        ["Unreviewed", ""],
        ["Needs follow-up", "Note: Need the source record"],
        ["Review complete", "Note: Checked"],
    ]


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("comparisons", [(), COMPARISONS], ids=["base", "comparisons"])
@pytest.mark.parametrize("empty", [False, True], ids=["clean-and-tolerated", "empty-inputs"])
def test_zero_exceptions_are_header_only_with_original_dynamic_schema(mode, comparisons, empty):
    specifications = () if empty else [("clean", ("3",), ("3",)), ("tolerated", ("3",), ("2.99",))]
    result = reconcile(
        *_ledger(specifications, value_a="Sales", value_b="Sales"),
        mode=mode,
        comparison_fields=comparisons,
        amount_tolerance=Decimal("0.01"),
    )
    binding = _bind(result)
    session = ReviewSession.unreviewed(binding)
    rows = _assert_report(binding, session)
    assert result.exceptions == binding.exception_keys == session.reviews == ()
    assert len(rows) == 1
    assert "Correspondence evidence" not in rows[0]
    assert ("Secondary differences" in rows[0]) == bool(comparisons)
    assert export_reviewed_exceptions_csv(binding, session).count(b"\r\n") == 1


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_secondary_only_exact_and_tolerated_mismatches_keep_match_and_unicode_evidence(mode):
    result = _result(
        [("exact", ("3",), ("3",)), ("within", ("3",), ("2.99",))],
        mode=mode,
        comparison_fields=COMPARISONS,
        amount_tolerance=Decimal("0.01"),
    )
    binding = _bind(result)
    rows = _assert_report(binding, _annotated(binding))
    assert (
        rows[0]
        == BASE_COLUMNS
        + [
            "Secondary differences",
            "Comparison 1 File A values — department",
            "Comparison 1 File B values — cost_center",
            "Comparison 1 status",
            "Comparison 2 File A values — currency",
            "Comparison 2 File B values — currency_code",
            "Comparison 2 status",
        ]
        + REVIEW_COLUMNS
    )
    assert [row[0] for row in rows[1:]] == ["Exact match", "Within tolerance"]
    assert result.tolerated_findings == ()
    for row in rows[1:]:
        assert row[7] == "Comparison 1: department ↔ cost_center"
        assert row[8:14] == ['["Café"]', '["财务"]', "mismatch", '["USD"]', '["USD"]', "match"]
        assert json.loads(row[8]) == ["Café"] and json.loads(row[9]) == ["财务"]
    assert "Café".encode() in export_reviewed_exceptions_csv(binding, _annotated(binding))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_multiple_rows_use_stored_comparison_statuses_and_distinct_sets(mode):
    result = _result([("K", ("1", "2"), ("4",))], mode=mode, comparison_fields=COMPARISONS)
    binding = _bind(result)
    rows = _assert_report(binding, _annotated(binding))
    status = "mismatch" if mode is ReconciliationMode.GROUPED_BY_KEY else "not_comparable"
    assert rows[1][10] == status
    assert rows[1][13] == (
        "match" if mode is ReconciliationMode.GROUPED_BY_KEY else "not_comparable"
    )
    assert rows[1][8:10] == ['["Café"]', '["财务"]']


def test_structured_comparisons_export_without_raw_field_reconstruction():
    left = SourceRecord(Source.A, 2, ("K",), Decimal("1"))
    right = SourceRecord(Source.B, 7, ("K",), Decimal("1"))
    comparison = FieldComparison(
        COMPARISONS[0], ("e\u0301", "财务 🧾"), ("Stored B",), FieldComparisonStatus.MISMATCH
    )
    finding = ReconciliationFinding(
        FindingCategory.EXACT_MATCH,
        ("K",),
        (left,),
        (right,),
        left.amount,
        right.amount,
        field_comparisons=(comparison,),
    )
    result = ReconciliationResult(
        left.amount, right.amount, (finding,), comparison_fields=(COMPARISONS[0],)
    )
    binding = _bind(result)
    row = _assert_report(binding, _annotated(binding))[1]
    assert json.loads(row[8]) == ["e\u0301", "财务 🧾"]
    assert json.loads(row[9]) == ["Stored B"]
    assert left.raw_fields == right.raw_fields == {}


CORRESPONDENCE_CASES = [
    pytest.param(
        ("300.00",), ("100.00", "200.00"), CorrespondenceStatus.UNIQUE_EXACT, None, id="full-exact"
    ),
    pytest.param(
        ("300.00",),
        ("100.00", "200.00", "7.00"),
        CorrespondenceStatus.UNIQUE_EXACT,
        None,
        id="residual",
    ),
    pytest.param(
        ("300",), ("300", "100", "200"), CorrespondenceStatus.AMBIGUOUS, None, id="ambiguous"
    ),
    pytest.param(
        ("300",), ("300", "1"), CorrespondenceStatus.SINGLETON_ONLY, None, id="singleton-only"
    ),
    pytest.param(
        ("300",),
        ("100", "199.99"),
        CorrespondenceStatus.NO_EXACT_SUBSET,
        None,
        id="no-exact-subset",
    ),
    pytest.param(
        ("13",),
        ("1",) * 13,
        CorrespondenceStatus.BOUND_EXCEEDED,
        CorrespondenceReason.CANDIDATE_ROW_LIMIT,
        id="candidate-bound",
    ),
    pytest.param(
        ("300", "2"),
        ("100", "202"),
        CorrespondenceStatus.NOT_ELIGIBLE,
        CorrespondenceReason.BOTH_SIDES_MULTIPLE,
        id="both-multiple",
    ),
    pytest.param(
        ("100", "200"),
        (),
        CorrespondenceStatus.NOT_ELIGIBLE,
        CorrespondenceReason.MISSING_OPPOSITE_SIDE,
        id="missing-opposite",
    ),
]


@pytest.mark.parametrize("amounts_a,amounts_b,status,reason", CORRESPONDENCE_CASES)
@pytest.mark.parametrize("anchor_source", list(Source))
@pytest.mark.parametrize("comparisons", [(), COMPARISONS], ids=["base", "comparisons"])
def test_all_correspondence_evidence_and_column_order_remain_exact(
    amounts_a, amounts_b, status, reason, anchor_source, comparisons
):
    if anchor_source is Source.B:
        amounts_a, amounts_b = amounts_b, amounts_a
    result = _result([("K", amounts_a, amounts_b)], mode=MODE, comparison_fields=comparisons)
    binding = _bind(result)
    rows = _assert_report(binding, _annotated(binding))
    assert rows[0][-4:] == ["Correspondence status", "Correspondence evidence"] + REVIEW_COLUMNS
    row = rows[1]
    evidence = json.loads(row[-3])
    analysis = result.exceptions[0].correspondence_analysis
    assert row[-4] == evidence["status"] == status.value
    assert evidence["reason"] == (None if reason is None else reason.value)
    assert evidence["search_complete"] is analysis.search_complete
    assert evidence["planned_combinations"] == analysis.planned_combinations
    assert evidence["examined_combinations"] == analysis.examined_combinations
    assert evidence["reserved_combinations"] == analysis.reserved_combinations
    assert evidence["matching_key"] == ["K"]
    if status is CorrespondenceStatus.UNIQUE_EXACT:
        assert evidence["accepted_solution"] is not None
        assert result.exceptions[0].has_inferred_correspondence
        if len(amounts_a) + len(amounts_b) == 3:
            assert row[0] == "Exact match" and row[4] == "0.00"
        else:
            assert row[0] == "Amount mismatch"
            assert evidence["unassigned_rows_a"] or evidence["unassigned_rows_b"]
    else:
        assert evidence["accepted_solution"] is None


def test_run_budget_bound_is_exported_from_complete_stored_run():
    specifications = [(f"K{index:03d}", ("0",), ("0",) * 12) for index in range(244)]
    specifications.append(("Z_BOUND", ("300",), ("100", "200") * 6))
    binding = _bind(_result(specifications, mode=MODE))
    rows = _assert_report(binding, _annotated(binding))
    assert len(rows) == 246
    evidence = json.loads(rows[-1][-3])
    assert rows[-1][1] == "Z_BOUND"
    assert evidence["status"] == "bound_exceeded"
    assert evidence["reason"] == "run_budget_exhausted"
    assert evidence["planned_combinations"] == 4095
    assert evidence["examined_combinations"] == evidence["reserved_combinations"] == 0
    assert evidence["search_complete"] is False


@pytest.mark.parametrize("comparisons", [(), COMPARISONS], ids=["base", "comparisons"])
def test_mixed_ordinary_and_analyzed_rows_keep_blank_correspondence_cells(comparisons):
    binding = _bind(
        _result(
            [("A_INFERRED", ("3",), ("1", "2")), ("B_ORDINARY", ("3",), ("2",))],
            mode=MODE,
            comparison_fields=comparisons,
        )
    )
    rows = _assert_report(binding, _annotated(binding))
    assert rows[1][-4] == "unique_exact" and json.loads(rows[1][-3])["accepted_solution"]
    assert rows[2][-4:-2] == ["", ""]
    assert rows[0][-4:-2] == ["Correspondence status", "Correspondence evidence"]


NOTES = [
    pytest.param("", id="empty"),
    pytest.param("Checked against the invoice", id="prose"),
    pytest.param("  leading and trailing  ", id="spaces"),
    pytest.param(" \t\r\n ", id="whitespace-only"),
    pytest.param("\t", id="tab"),
    pytest.param("\n", id="lf"),
    pytest.param("\r", id="cr"),
    pytest.param("\r\n", id="crlf"),
    pytest.param("first\nsecond\rthird\r\nfourth", id="multiline"),
    pytest.param("Café 财务", id="unicode"),
    pytest.param("e\u0301", id="combining"),
    pytest.param("🧾😀", id="emoji"),
    pytest.param("comma,cell", id="comma"),
    pytest.param('double "quote"', id="quote"),
    pytest.param(r"back\slash", id="backslash"),
    pytest.param("=1+2", id="equals-formula"),
    pytest.param("+SUM(A1:A2)", id="plus-formula"),
    pytest.param("-1+2", id="minus-formula"),
    pytest.param("@SUM(A1:A2)", id="at-formula"),
    pytest.param("  =1+2", id="leading-spaces-formula"),
    pytest.param("\t@SUM(A1:A2)", id="leading-tab-formula"),
    pytest.param(" \t+SUM(A1:A2)", id="leading-space-tab-formula"),
    pytest.param("\n=1+2", id="leading-lf-formula"),
    pytest.param("\r-1+2", id="leading-cr-formula"),
    pytest.param('first\n=1+2,"another cell"', id="multiline-formula"),
    pytest.param(
        '"\r\nFile A only,OTHER,99,,,2,,Review complete,=1+2\r\n"', id="csv-row-lookalike"
    ),
    pytest.param('","=1+2"\r\n"@SUM(A1:A2)",next', id="csv-quote-breakout"),
    pytest.param("🧾" * 4096, id="maximum-utf8-note"),
]


@pytest.mark.parametrize("note", NOTES)
def test_every_note_is_one_exact_prefixed_csv_cell_and_json_stays_raw(note):
    binding = _bind()
    session = _annotated(binding, ReviewDisposition.NEEDS_FOLLOW_UP, note)
    json_before = export_review_session(session)
    rows = _assert_report(binding, session)
    assert len(rows) == 2 and len(rows[1]) == 9
    assert rows[1][-1] == ("" if note == "" else "Note: " + note)
    if note != "":
        assert rows[1][-1].startswith("Note: ") and rows[1][-1][0] == "N"
    assert session.reviews[0].note == note
    assert json.loads(json_before)["reviews"][0]["note"] == note
    assert export_review_session(session) == json_before


@pytest.mark.parametrize(
    "key", ["=1+2", "+SUM(A1:A2)", "-1+2", "@SUM(A1:A2)", "  =1+2", "\t@SUM(A1:A2)"]
)
def test_formula_looking_original_evidence_is_preserved_while_notes_are_prefixed(key):
    result = reconcile(
        *_ledger([(key, ("3",), ("1", "2", "7"))], value_a="=1+2", value_b="@SUM(A1:A2)"),
        mode=MODE,
        comparison_fields=COMPARISONS,
    )
    binding = _bind(result)
    rows = _assert_report(binding, _annotated(binding, note='=1+2,"cell"\r\nnext'))
    row = rows[1]
    assert row[1] == key
    assert json.loads(row[8]) == ["=1+2"]
    assert json.loads(row[9]) == ["@SUM(A1:A2)"]
    evidence = json.loads(row[-3])
    assert evidence["matching_key"] == evidence["anchor"]["original_key"] == [key]
    assert all(
        record["original_key"] == [key] for record in evidence["accepted_solution"]["rows_b"]
    )
    assert row[-1] == 'Note: =1+2,"cell"\r\nnext'


def _forbidden(*args, **kwargs):
    pytest.fail("export must not construct output or recompute evidence here")


def _forbid_output(monkeypatch):
    for name in ("StringIO", "_exception_columns", "_exception_row"):
        monkeypatch.setattr(export_module, name, _forbidden)


@pytest.mark.parametrize("argument", ["binding", "session"])
@pytest.mark.parametrize(
    "wrong", [None, "binding", {}, (), object()], ids=["none", "str", "dict", "tuple", "object"]
)
def test_wrong_python_types_fail_before_output(monkeypatch, argument, wrong):
    binding = _bind()
    session = ReviewSession.unreviewed(binding)
    options = dict(binding=binding, session=session)
    options[argument] = wrong
    _forbid_output(monkeypatch)
    with pytest.raises(TypeError, match=argument):
        export_reviewed_exceptions_csv(**options)


@pytest.mark.parametrize(
    "difference", ["input-bytes", "raw-evidence", "amount", "format", "fingerprint-only"]
)
def test_mismatched_fingerprints_fail_even_when_exception_keys_match(monkeypatch, difference):
    binding = _bind()
    if difference == "input-bytes":
        other = _bind(data_a=b"different input A")
    elif difference == "raw-evidence":
        result = reconcile(*_ledger([("K", ("3",), ("2",))], value_a="Different raw evidence"))
        # Dataclass equality alone cannot detect this evidence difference.
        assert result == binding.result
        other = _bind(result)
    elif difference == "amount":
        other = _bind(_result([("K", ("4",), ("2",))]))
    elif difference == "format":
        other = _bind(format_a="xlsx", worksheet_a="Ledger")
    else:
        other = replace(binding, result_fingerprint="0" * 64)
    session = ReviewSession.unreviewed(other)
    assert other.exception_keys == binding.exception_keys
    assert other.result_fingerprint != binding.result_fingerprint
    _forbid_output(monkeypatch)
    with pytest.raises(ReviewSessionError, match="fingerprint"):
        export_reviewed_exceptions_csv(binding, session)


@pytest.mark.parametrize(
    "population", ["different-key", "fewer", "more", "same-keys-different-order"]
)
def test_exception_population_is_checked_independently_of_fingerprint(monkeypatch, population):
    binding = _bind(_result([(name, ("3",), ("2",)) for name in ("A", "B")]))
    keys = {
        "different-key": ("A", "Z"),
        "fewer": ("A",),
        "more": ("A", "B", "C"),
        "same-keys-different-order": ("B", "A"),
    }[population]
    result = _result([(name, ("3",), ("2",)) for name in keys])
    # Public construction permits a supplied valid digest; use the same one to
    # isolate the separate population check rather than the fingerprint check.
    other = ReviewBinding(
        result, binding.result_fingerprint, tuple(f.key for f in result.exceptions)
    )
    if population == "same-keys-different-order":
        # ReviewBinding rejects unsorted results at construction. Bypass its
        # frozen state only to isolate the export's exact ordered-key check.
        object.__setattr__(other, "exception_keys", tuple(reversed(other.exception_keys)))
    session = ReviewSession.unreviewed(other)
    _forbid_output(monkeypatch)
    with pytest.raises(ReviewSessionError, match="exception keys"):
        export_reviewed_exceptions_csv(binding, session)


@pytest.mark.parametrize("invalid", ["reversed", "missing", "extra", "duplicate", "wrong-key"])
def test_invalid_review_order_and_population_are_never_repaired(monkeypatch, invalid):
    binding = _bind(_result([(name, ("3",), ("2",)) for name in ("A", "B")]))
    session = ReviewSession.unreviewed(binding)
    first, second = session.reviews
    reviews = {
        "reversed": (second, first),
        "missing": (first,),
        "extra": (first, second, first),
        "duplicate": (first, first),
        "wrong-key": (first, FindingReview(("Z",), ReviewDisposition.REVIEW_COMPLETE)),
    }[invalid]
    # Bypass frozen-domain construction solely to exercise the export boundary's
    # required review-key check, which ordinary ReviewSession already enforces.
    object.__setattr__(session, "reviews", reviews)
    _forbid_output(monkeypatch)
    with pytest.raises(ReviewSessionError, match="exception keys in order"):
        export_reviewed_exceptions_csv(binding, session)
    assert session.reviews is reviews


def test_equivalent_independently_rebuilt_bindings_support_persisted_sessions():
    binding = _bind(_baseline_result("mixed"))
    rebuilt = _bind(_baseline_result("mixed"))
    assert rebuilt is not binding and rebuilt.result is not binding.result
    assert rebuilt.result.findings[0] is not binding.result.findings[0]
    assert rebuilt.result_fingerprint == binding.result_fingerprint
    assert rebuilt.exception_keys == binding.exception_keys
    session = _annotated(rebuilt)
    saved = export_review_session(session)
    _assert_report(binding, session)
    loaded = load_review_session(saved, binding=rebuilt)
    _assert_report(binding, loaded)
    assert export_reviewed_exceptions_csv(binding, loaded) == export_reviewed_exceptions_csv(
        rebuilt, session
    )


def test_export_uses_current_binding_result_even_with_equivalent_session_binding():
    binding = _bind()
    # Caller-supplied ReviewBinding digests are not recomputed by the exporter.
    # Hold the compatibility fields fixed to make the stored-result source clear.
    other_result = _result([("K", ("9",), ("2",))])
    other = ReviewBinding(other_result, binding.result_fingerprint, binding.exception_keys)
    session = _annotated(other)
    rows = _assert_report(binding, session)
    assert rows[1][2:5] == ["3", "2", "+1"]
    assert rows[1][2:5] != _read(export_exceptions_csv(other_result))[1][2:5]


def test_canonical_tuple_keys_remain_distinct_when_display_keys_collide():
    binding = _bind(_result([(("A / B", "C"), ("3",), ("2",)), (("A", "B / C"), ("4",), ("2",))]))
    assert binding.exception_keys == (("A", "B / C"), ("A / B", "C"))
    session = ReviewSession(
        binding,
        (
            FindingReview(("A", "B / C"), ReviewDisposition.NEEDS_FOLLOW_UP, "First canonical key"),
            FindingReview(
                ("A / B", "C"), ReviewDisposition.REVIEW_COMPLETE, "Second canonical key"
            ),
        ),
    )
    rows = _assert_report(binding, session)
    assert rows[1][1] == rows[2][1] == "A / B / C"
    assert rows[1][-2:] == ["Needs follow-up", "Note: First canonical key"]
    assert rows[2][-2:] == ["Review complete", "Note: Second canonical key"]


def _snapshot(value):
    """Include object identities and raw fields excluded from dataclass equality."""

    if is_dataclass(value):
        return (
            type(value),
            id(value),
            tuple((f.name, _snapshot(getattr(value, f.name))) for f in fields(value)),
        )
    if isinstance(value, Mapping):
        return (
            type(value),
            id(value),
            tuple((key, _snapshot(item)) for key, item in value.items()),
        )
    if isinstance(value, tuple):
        return (type(value), id(value), tuple(_snapshot(item) for item in value))
    return value


@pytest.mark.parametrize("name", ORIGINAL_BYTE_DIGESTS)
def test_reviewed_export_preserves_all_objects_evidence_populations_and_artifact_bytes(name):
    binding = _bind(_baseline_result(name))
    session = _annotated(binding)
    result = binding.result
    before = (_snapshot(binding), _snapshot(session))
    original = export_exceptions_csv(result)
    saved = export_review_session(session)
    exceptions = result.exceptions
    tolerated = result.tolerated_findings
    findings = result.findings
    reviews = session.reviews
    controls = (result.total_a, result.total_b, result.control_difference)
    _assert_report(binding, session)
    assert (_snapshot(binding), _snapshot(session)) == before
    assert session.binding is binding and binding.result is result
    assert result.findings is findings and session.reviews is reviews
    assert all(
        after is initial for after, initial in zip(result.exceptions, exceptions, strict=True)
    )
    assert all(
        after is initial
        for after, initial in zip(result.tolerated_findings, tolerated, strict=True)
    )
    assert (result.total_a, result.total_b, result.control_difference) == controls
    assert export_exceptions_csv(result) == original
    assert export_review_session(session) == saved


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("comparisons", [(), COMPARISONS], ids=["base", "comparisons"])
def test_reviewed_export_never_recomputes_ingestion_normalization_comparisons_or_subsets(
    monkeypatch, mode, comparisons
):
    binding = _bind(
        _result(
            [("A", ("3",), ("1", "2")), ("B", ("3",), ("2",))],
            mode=mode,
            comparison_fields=comparisons,
        )
    )
    session = _annotated(binding)
    for module, names in (
        (tallydiff, ("reconcile", "ingest_csv", "ingest_xlsx", "bind_review_result")),
        (
            engine,
            (
                "reconcile",
                "normalize_key",
                "find_normalization_collisions",
                "_compare_fields",
                "analyze_bounded_one_to_many",
            ),
        ),
        (normalization, ("normalize_key", "find_normalization_collisions")),
        (subset_matching, ("analyze_bounded_one_to_many", "combinations")),
        (ingestion, ("ingest_csv",)),
        (xlsx, ("ingest_xlsx",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, _forbidden)
    # Both exporters must consume the same stored construction, without parsing
    # the old CSV to recover rows or joining annotations on displayed keys.
    original_export = export_module.export_exceptions_csv
    original = original_export(binding.result)
    monkeypatch.setattr(export_module, "export_exceptions_csv", _forbidden)
    rows = _assert_report(binding, session)
    assert [row[:-2] for row in rows] == _read(original)


LEGACY_PUBLIC_API = set(
    """
    EXACT_UNIQUE_ONE_TO_MANY_POLICY AmountParseError ColumnMapping ComparisonFieldMapping
    CorrespondenceAnalysis CorrespondenceReason CorrespondenceStatus FieldComparison
    FieldComparisonStatus FindingCategory FindingReview IngestionError KeyNormalizationConfig
    KeyNormalizationError KeyNormalizationRules MappingProfile NormalizationCollision
    NormalizationCollisionError OriginalKeyEvidence OneToManyPolicy ProfileError
    ReconciliationFinding ReconciliationIntegrityError ReconciliationMode ReconciliationResult
    ReviewBinding ReviewDisposition ReviewSession ReviewSessionError Source SourceRecord
    SubsetSolution
    bind_review_result export_exceptions_csv export_mapping_profile export_review_session
    ingest_csv ingest_xlsx inspect_csv_columns inspect_xlsx_columns inspect_xlsx_sheets
    load_mapping_profile load_review_session parse_amount reconcile
    """.split()
)


def test_public_exporter_signature_legacy_api_and_package_version():
    assert tallydiff.export_reviewed_exceptions_csv is export_module.export_reviewed_exceptions_csv
    signature = inspect.signature(export_reviewed_exceptions_csv)
    assert list(signature.parameters) == ["binding", "session"]
    assert signature.parameters["binding"].annotation is ReviewBinding
    assert signature.parameters["session"].annotation is ReviewSession
    assert all(
        p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in signature.parameters.values()
    )
    assert all(p.default is inspect.Parameter.empty for p in signature.parameters.values())
    assert signature.return_annotation is bytes
    original_signature = inspect.signature(export_exceptions_csv)
    assert list(original_signature.parameters) == ["result"]
    assert original_signature.parameters["result"].annotation is ReconciliationResult
    assert original_signature.return_annotation is bytes
    assert set(tallydiff.__all__) == LEGACY_PUBLIC_API | {"export_reviewed_exceptions_csv"}
    assert len(set(tallydiff.__all__)) == len(tallydiff.__all__)
    assert all(hasattr(tallydiff, name) for name in LEGACY_PUBLIC_API)
    assert tallydiff.export_review_session is export_review_session
    assert tallydiff.load_review_session is load_review_session
    assert version("tallydiff") == "0.6.0"
