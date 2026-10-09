"""Exact secondary controls preserve primary reconciliation and drive review."""

import csv
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from random import Random

import pytest

import tallydiff.engine as engine
from tallydiff import (
    ComparisonFieldMapping,
    FieldComparisonStatus,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    NormalizationCollisionError,
    ReconciliationMode,
    Source,
    SourceRecord,
    ingest_csv,
    ingest_xlsx,
    reconcile,
)

DEPARTMENT = ComparisonFieldMapping("Department", "Cost Center")
CURRENCY = ComparisonFieldMapping("Currency", "Currency Code")
FIELDS = (DEPARTMENT,)


def _record(source, row, value="Sales", *, amount="100", key=("INV",), extra=None):
    column = "Department" if source is Source.A else "Cost Center"
    return SourceRecord(source, row, key, Decimal(amount), {column: value, **(extra or {})})


def _side(source, values):
    return tuple(_record(source, row, value) for row, value in enumerate(values, start=2))


def _primary_only(result):
    return replace(
        result,
        findings=tuple(replace(finding, field_comparisons=()) for finding in result.findings),
        comparison_fields=(),
    )


class Once:
    def __init__(self, records):
        self.records = records
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        assert self.iterations == 1, "source iterable must only be consumed once"
        yield from self.records


@pytest.mark.parametrize(
    ("amount_b", "category"),
    [
        ("100", FindingCategory.EXACT_MATCH),
        ("100.01", FindingCategory.WITHIN_TOLERANCE),
        ("101", FindingCategory.AMOUNT_MISMATCH),
    ],
)
@pytest.mark.parametrize(
    ("value_b", "status"),
    [("Sales", FieldComparisonStatus.MATCH), ("Marketing", FieldComparisonStatus.MISMATCH)],
)
def test_unique_comparison_is_independent_of_amount_category_and_secondary_review(
    amount_b, category, value_b, status
):
    a = (_record(Source.A, 2),)
    b = (_record(Source.B, 3, value_b, amount=amount_b),)
    result = reconcile(a, b, amount_tolerance=Decimal("0.01"), comparison_fields=FIELDS)
    finding = result.findings[0]

    assert finding.category is category
    assert finding.field_comparisons[0].status is status
    assert finding.delta == Decimal("100") - Decimal(amount_b)
    requires_review = (
        category is FindingCategory.AMOUNT_MISMATCH or status is FieldComparisonStatus.MISMATCH
    )
    accepted_tolerance = category is FindingCategory.WITHIN_TOLERANCE and not requires_review
    assert finding.is_exception is requires_review
    assert result.exceptions == ((finding,) if requires_review else ())
    assert result.tolerated_findings == ((finding,) if accepted_tolerance else ())
    assert result.tolerated_delta_total == Decimal("-0.01" if accepted_tolerance else "0")
    assert result.comparison_fields is FIELDS
    assert finding.rows_a[0] is a[0] and finding.rows_b[0] is b[0]
    assert _primary_only(result) == reconcile(a, b, amount_tolerance=Decimal("0.01"))
    assert result.control_difference == result.finding_delta_sum


@pytest.mark.parametrize(
    ("value_a", "value_b", "status"),
    [
        ("", "", FieldComparisonStatus.MATCH),
        ("", "Sales", FieldComparisonStatus.MISMATCH),
        ("Sales", "sales", FieldComparisonStatus.MISMATCH),
        (" Sales ", "Sales", FieldComparisonStatus.MISMATCH),
        ("Straße", "STRASSE", FieldComparisonStatus.MISMATCH),
        ("é", "e\u0301", FieldComparisonStatus.MISMATCH),
        ("first\r\nsecond", "first\nsecond", FieldComparisonStatus.MISMATCH),
        ("100", "100.00", FieldComparisonStatus.MISMATCH),
        ("2026-01-02", "2026-01-02T00:00:00", FieldComparisonStatus.MISMATCH),
        ("=1+1", "=1+1", FieldComparisonStatus.MATCH),
        ("#N/A", "#N/A", FieldComparisonStatus.MATCH),
    ],
)
def test_unique_uses_exact_evidence_without_normalization_or_type_inference(
    value_a, value_b, status
):
    result = reconcile(
        _side(Source.A, [value_a]), _side(Source.B, [value_b]), comparison_fields=FIELDS
    )
    comparison = result.findings[0].field_comparisons[0]

    assert comparison.values_a == (value_a,)
    assert comparison.values_b == (value_b,)
    assert comparison.status is status


@pytest.mark.parametrize(
    ("values_a", "values_b", "category", "summary_a", "summary_b"),
    [
        (["Sales"], [], FindingCategory.A_ONLY, ("Sales",), ()),
        ([], ["Sales"], FindingCategory.B_ONLY, (), ("Sales",)),
        (
            ["Sales", "Marketing"],
            ["Sales"],
            FindingCategory.DUPLICATE_AMBIGUOUS,
            ("Marketing", "Sales"),
            ("Sales",),
        ),
        (
            ["Sales", "Sales"],
            ["Sales", "Sales"],
            FindingCategory.DUPLICATE_AMBIGUOUS,
            ("Sales",),
            ("Sales",),
        ),
        (
            ["Sales", "Marketing"],
            [],
            FindingCategory.DUPLICATE_AMBIGUOUS,
            ("Marketing", "Sales"),
            (),
        ),
        ([], ["Sales", ""], FindingCategory.DUPLICATE_AMBIGUOUS, (), ("", "Sales")),
    ],
)
def test_unique_unavailable_comparisons_retain_distinct_observed_values(
    values_a, values_b, category, summary_a, summary_b
):
    result = reconcile(
        _side(Source.A, values_a), _side(Source.B, values_b), comparison_fields=FIELDS
    )
    finding = result.findings[0]
    comparison = finding.field_comparisons[0]

    assert finding.category is category
    assert comparison.status is FieldComparisonStatus.NOT_COMPARABLE
    assert comparison.values_a == summary_a
    assert comparison.values_b == summary_b
    assert finding.has_secondary_mismatch is False
    assert finding.is_exception is True


@pytest.mark.parametrize(
    ("values_a", "values_b", "summary_a", "summary_b", "status"),
    [
        (["Sales"], ["Sales"], ("Sales",), ("Sales",), FieldComparisonStatus.MATCH),
        (["Sales"], ["Sales", "Sales"], ("Sales",), ("Sales",), FieldComparisonStatus.MATCH),
        (["Sales", "Sales"], ["Sales"], ("Sales",), ("Sales",), FieldComparisonStatus.MATCH),
        (
            ["Sales", "Marketing"],
            ["Marketing", "Sales"],
            ("Marketing", "Sales"),
            ("Marketing", "Sales"),
            FieldComparisonStatus.MATCH,
        ),
        (
            ["Sales", "Sales", "Marketing"],
            ["Marketing", "Sales", "Marketing", "Sales"],
            ("Marketing", "Sales"),
            ("Marketing", "Sales"),
            FieldComparisonStatus.MATCH,
        ),
        (
            ["Sales", "Sales"],
            ["Sales", "Marketing"],
            ("Sales",),
            ("Marketing", "Sales"),
            FieldComparisonStatus.MISMATCH,
        ),
        (
            ["", "Sales"],
            ["Sales", "", ""],
            ("", "Sales"),
            ("", "Sales"),
            FieldComparisonStatus.MATCH,
        ),
        (["Sales"], ["Sales", ""], ("Sales",), ("", "Sales"), FieldComparisonStatus.MISMATCH),
        (["", ""], [""], ("",), ("",), FieldComparisonStatus.MATCH),
        (
            ["Sales", "sales", "é", "e\u0301"],
            ["é", "Sales", "e\u0301", "sales"],
            ("Sales", "e\u0301", "sales", "é"),
            ("Sales", "e\u0301", "sales", "é"),
            FieldComparisonStatus.MATCH,
        ),
    ],
)
def test_grouped_compares_distinct_sets_without_counts_or_pairing(
    values_a, values_b, summary_a, summary_b, status
):
    a, b = _side(Source.A, values_a), _side(Source.B, values_b)
    result = reconcile(a, b, mode=ReconciliationMode.GROUPED_BY_KEY, comparison_fields=FIELDS)
    comparison = result.findings[0].field_comparisons[0]

    assert comparison.values_a == summary_a
    assert comparison.values_b == summary_b
    assert comparison.status is status
    assert result == reconcile(
        reversed(a), reversed(b), mode=ReconciliationMode.GROUPED_BY_KEY, comparison_fields=FIELDS
    )
    assert _primary_only(result) == reconcile(a, b, mode=ReconciliationMode.GROUPED_BY_KEY)


@pytest.mark.parametrize("source", list(Source))
def test_grouped_one_sided_summaries_are_not_comparable(source):
    records = _side(source, ["Sales", "", "Sales", "Marketing"])
    result = reconcile(
        records if source is Source.A else (),
        records if source is Source.B else (),
        mode=ReconciliationMode.GROUPED_BY_KEY,
        comparison_fields=FIELDS,
    )
    finding = result.findings[0]
    comparison = finding.field_comparisons[0]

    assert finding.category is (
        FindingCategory.A_ONLY if source is Source.A else FindingCategory.B_ONLY
    )
    assert comparison.status is FieldComparisonStatus.NOT_COMPARABLE
    assert comparison.values_a == (("", "Marketing", "Sales") if source is Source.A else ())
    assert comparison.values_b == (("", "Marketing", "Sales") if source is Source.B else ())


def test_grouped_credits_zero_lines_and_offsets_all_participate():
    a = tuple(
        _record(Source.A, row, value, amount=amount)
        for row, value, amount in [
            (2, "Main", "100"),
            (3, "Credit", "-100"),
            (4, "Zero", "0"),
            (5, "", "0"),
        ]
    )
    b = (_record(Source.B, 8, "Main", amount="0"),)
    result = reconcile(a, b, mode=ReconciliationMode.GROUPED_BY_KEY, comparison_fields=FIELDS)
    finding = result.findings[0]
    comparison = finding.field_comparisons[0]

    assert finding.category is FindingCategory.EXACT_MATCH
    assert comparison.values_a == ("", "Credit", "Main", "Zero")
    assert comparison.values_b == ("Main",)
    assert comparison.status is FieldComparisonStatus.MISMATCH
    assert finding.is_exception is True
    assert (
        result.total_a
        == result.total_b
        == result.control_difference
        == result.finding_delta_sum
        == Decimal("0")
    )
    assert Counter(id(row) for row in (*finding.rows_a, *finding.rows_b)) == Counter(
        map(id, (*a, *b))
    )


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_every_finding_retains_configured_mapping_order_and_independent_statuses(mode):
    fields = (CURRENCY, DEPARTMENT)
    a = (
        _record(Source.A, 2, extra={"Currency": "USD"}),
        _record(Source.A, 3, key=("ONLY",), extra={"Currency": "USD"}),
    )
    b = (_record(Source.B, 5, extra={"Currency Code": "EUR"}),)
    result = reconcile(a, b, mode=mode, comparison_fields=fields)

    assert result.comparison_fields is fields
    assert all(
        tuple(comparison.mapping for comparison in finding.field_comparisons) == fields
        for finding in result.findings
    )
    assert [comparison.status for comparison in result.findings[0].field_comparisons] == [
        FieldComparisonStatus.MISMATCH,
        FieldComparisonStatus.MATCH,
    ]
    assert all(
        comparison.status is FieldComparisonStatus.NOT_COMPARABLE
        for comparison in result.findings[1].field_comparisons
    )


def test_grouped_cross_field_associations_are_deliberately_not_compared():
    a = (
        _record(Source.A, 2, "Sales", extra={"Currency": "USD"}),
        _record(Source.A, 3, "Marketing", extra={"Currency": "EUR"}),
    )
    b = (
        _record(Source.B, 7, "Sales", extra={"Currency Code": "EUR"}),
        _record(Source.B, 8, "Marketing", extra={"Currency Code": "USD"}),
    )
    result = reconcile(
        a, b, mode=ReconciliationMode.GROUPED_BY_KEY, comparison_fields=(DEPARTMENT, CURRENCY)
    )
    comparisons = result.findings[0].field_comparisons

    assert comparisons[0].values_a == comparisons[0].values_b == ("Marketing", "Sales")
    assert comparisons[1].values_a == comparisons[1].values_b == ("EUR", "USD")
    assert all(comparison.status is FieldComparisonStatus.MATCH for comparison in comparisons)


@pytest.mark.parametrize(
    ("fields", "error_type", "message"),
    [
        (None, TypeError, "tuple of ComparisonFieldMapping"),
        ([], TypeError, "tuple of ComparisonFieldMapping"),
        ([DEPARTMENT], TypeError, "tuple of ComparisonFieldMapping"),
        ("Department", TypeError, "tuple of ComparisonFieldMapping"),
        ((None,), TypeError, "tuple of ComparisonFieldMapping"),
        ((DEPARTMENT, object()), TypeError, "tuple of ComparisonFieldMapping"),
        ((ComparisonFieldMapping(None, "Cost Center"),), ValueError, "nonblank columns"),
        ((ComparisonFieldMapping("Department", None),), ValueError, "nonblank columns"),
        ((ComparisonFieldMapping("", "Cost Center"),), ValueError, "nonblank columns"),
        ((ComparisonFieldMapping("Department", " \t"),), ValueError, "nonblank columns"),
        (
            (DEPARTMENT, ComparisonFieldMapping("Department", "Currency Code")),
            ValueError,
            "File A comparison column",
        ),
        (
            (DEPARTMENT, ComparisonFieldMapping("Currency", "Cost Center")),
            ValueError,
            "File B comparison column",
        ),
    ],
)
def test_engine_rejects_invalid_configuration_before_consuming_records(fields, error_type, message):
    a, b = Once(()), Once(())
    with pytest.raises(error_type, match=message):
        reconcile(a, b, comparison_fields=fields)
    assert a.iterations == b.iterations == 0


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize("shape", ["two_sided", "one_sided", "duplicate"])
def test_missing_evidence_in_any_row_blocks_before_classification_with_context(
    mode, source, shape, monkeypatch
):
    good = _record(source, 2, key=("INV",) if shape == "duplicate" else ("OTHER",))
    missing = replace(_record(source, 9), raw_fields={})
    other = (
        () if shape == "one_sided" else (_record(Source.B if source is Source.A else Source.A, 4),)
    )
    a, b = ((good, missing), other) if source is Source.A else (other, (good, missing))

    def forbidden(*args, **kwargs):
        pytest.fail("missing evidence must block before classification or result construction")

    monkeypatch.setattr(engine, "_classify", forbidden)
    monkeypatch.setattr(engine, "ReconciliationResult", forbidden)
    with pytest.raises(ValueError) as error:
        reconcile(a, b, mode=mode, comparison_fields=FIELDS)
    message = str(error.value)
    assert f"File {source.value}" in message
    assert "source row 9" in message
    assert ("Department" if source is Source.A else "Cost Center") in message
    assert "missing" in message


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_normalized_keys_compare_original_identifier_evidence_exactly(mode):
    identifier = ComparisonFieldMapping("Vendor", "Supplier")
    a = ingest_csv(
        "Vendor,amount\nACME-01,100\n",
        source=Source.A,
        key_columns=("Vendor",),
        amount_column="amount",
        comparison_columns=("Vendor",),
    )
    b = ingest_csv(
        "Supplier,amount\nACME01,100\n",
        source=Source.B,
        key_columns=("Supplier",),
        amount_column="amount",
        comparison_columns=("Supplier",),
    )
    normalization = KeyNormalizationConfig((KeyNormalizationRules(remove_punctuation=True),))
    result = reconcile(
        a, b, mode=mode, key_normalization=normalization, comparison_fields=(identifier,)
    )
    finding = result.findings[0]

    assert finding.key == ("ACME01",)
    assert finding.category is FindingCategory.EXACT_MATCH
    assert finding.field_comparisons[0].values_a == ("ACME-01",)
    assert finding.field_comparisons[0].values_b == ("ACME01",)
    assert finding.field_comparisons[0].status is FieldComparisonStatus.MISMATCH
    assert finding.rows_a[0] is a[0] and finding.rows_b[0] is b[0]
    assert a[0].key == ("ACME-01",) and b[0].key == ("ACME01",)
    assert a[0].raw_fields["Vendor"] == "ACME-01"
    assert b[0].raw_fields["Supplier"] == "ACME01"
    assert result.key_normalization is normalization


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("colliding_sources", [(Source.A,), (Source.B,), (Source.A, Source.B)])
def test_normalization_collisions_still_block_before_secondary_comparison(
    mode, colliding_sources, monkeypatch
):
    a = tuple(
        _record(Source.A, row, key=(key,))
        for row, key in enumerate(
            ("ACME-01", "ACME01") if Source.A in colliding_sources else ("ACME01",), start=2
        )
    )
    b = tuple(
        _record(Source.B, row, key=(key,))
        for row, key in enumerate(
            ("ACME-01", "ACME01") if Source.B in colliding_sources else ("ACME01",), start=2
        )
    )

    def forbidden(*args, **kwargs):
        pytest.fail("normalization collisions must block before comparison or result construction")

    monkeypatch.setattr(engine, "_compare_fields", forbidden)
    monkeypatch.setattr(engine, "ReconciliationResult", forbidden)
    with pytest.raises(NormalizationCollisionError) as error:
        reconcile(
            a,
            b,
            mode=mode,
            comparison_fields=FIELDS,
            key_normalization=KeyNormalizationConfig(
                (KeyNormalizationRules(remove_punctuation=True),)
            ),
        )
    assert bool(error.value.collisions_a) is (Source.A in colliding_sources)
    assert bool(error.value.collisions_b) is (Source.B in colliding_sources)
    retained = [
        record
        for collision in (*error.value.collisions_a, *error.value.collisions_b)
        for original in collision.originals
        for record in original.records
    ]
    expected = [record for record in (*a, *b) if record.source in colliding_sources]
    assert Counter(map(id, retained)) == Counter(map(id, expected))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_one_pass_inputs_shuffling_identity_evidence_and_accounting_remain_intact(mode):
    a = (
        _record(Source.A, 5, "Sales", amount="20"),
        _record(Source.A, 2, "Marketing", amount="-10"),
        _record(Source.A, 7, "", amount="0", key=("ONLY_A",)),
    )
    b = (
        _record(Source.B, 8, "Sales", amount="10"),
        _record(Source.B, 3, "Sales", amount="0"),
        _record(Source.B, 9, "Other", amount="2", key=("ONLY_B",)),
    )
    before = [(record.key, record.amount, dict(record.raw_fields)) for record in (*a, *b)]
    inputs_a, inputs_b = Once(a), Once(b)
    result = reconcile(inputs_a, inputs_b, mode=mode, comparison_fields=FIELDS)
    shuffled_a, shuffled_b = list(a), list(b)
    Random(42).shuffle(shuffled_a)
    Random(43).shuffle(shuffled_b)

    assert inputs_a.iterations == inputs_b.iterations == 1
    assert result == reconcile(shuffled_a, shuffled_b, mode=mode, comparison_fields=FIELDS)
    assert result == reconcile(reversed(a), reversed(b), mode=mode, comparison_fields=FIELDS)
    assert before == [(record.key, record.amount, dict(record.raw_fields)) for record in (*a, *b)]
    retained = [
        record for finding in result.findings for record in (*finding.rows_a, *finding.rows_b)
    ]
    assert Counter(map(id, retained)) == Counter(map(id, (*a, *b)))
    assert result.total_a == Decimal("10") and result.total_b == Decimal("12")
    assert result.control_difference == result.finding_delta_sum == Decimal("-2")
    assert _primary_only(result) == reconcile(a, b, mode=mode)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_empty_reconciliation_retains_complete_ordered_comparison_configuration(mode):
    fields = (CURRENCY, DEPARTMENT)
    result = reconcile(iter(()), iter(()), mode=mode, comparison_fields=fields)

    assert result.comparison_fields is fields
    assert result.findings == ()
    assert result.total_a == result.total_b == result.control_difference == Decimal("0")


def test_direct_engine_cannot_infer_the_primary_amount_column_role():
    fields = (ComparisonFieldMapping("amount", "gross"),)
    a = (_record(Source.A, 2, extra={"amount": "original A"}),)
    b = (_record(Source.B, 2, extra={"gross": "original B"}),)
    result = reconcile(a, b, comparison_fields=fields)

    assert result.findings[0].category is FindingCategory.EXACT_MATCH
    assert result.findings[0].field_comparisons[0].status is FieldComparisonStatus.MISMATCH


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    "formats", [("csv", "csv"), ("csv", "xlsx"), ("xlsx", "csv"), ("xlsx", "xlsx")]
)
def test_selected_comparison_end_to_end_is_equivalent_across_formats(mode, formats, xlsx_bytes):
    inputs = []
    for source, kind, column in [
        (Source.A, formats[0], "Department"),
        (Source.B, formats[1], "Cost Center"),
    ]:
        rows = [
            ["id", "amount", column],
            ["INV", "10.00", " Sales "],
            ["INV", "20.00", ""],
            ["SINGLE", "1.00", "Café"],
        ]
        options = {
            "source": source,
            "key_columns": ("id",),
            "amount_column": "amount",
            "comparison_columns": (column,),
        }
        if kind == "xlsx":
            records = ingest_xlsx(xlsx_bytes({"Ledger": rows}), worksheet="Ledger", **options)
        else:
            text = StringIO(newline="")
            csv.writer(text, lineterminator="\r\n").writerows(rows)
            records = ingest_csv(text.getvalue(), **options)
        inputs.append(records)
    result = reconcile(*inputs, mode=mode, comparison_fields=FIELDS)

    assert [finding.category for finding in result.findings] == [
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.EXACT_MATCH,
        FindingCategory.EXACT_MATCH,
    ]
    assert (
        result.findings[0].field_comparisons[0].values_a
        == result.findings[0].field_comparisons[0].values_b
        == ("", " Sales ")
    )
    assert result.findings[0].field_comparisons[0].status is (
        FieldComparisonStatus.NOT_COMPARABLE
        if mode is ReconciliationMode.UNIQUE
        else FieldComparisonStatus.MATCH
    )
    assert result.findings[1].field_comparisons[0].status is FieldComparisonStatus.MATCH
    assert result.total_a == result.total_b == Decimal("31.00")
    assert _primary_only(result) == reconcile(*inputs, mode=mode)


@pytest.mark.parametrize(
    ("csv_value", "xlsx_value", "matches"),
    [
        ("TRUE", True, True),
        ("true", True, False),
        ("100", 100, True),
        ("100.00", 100, False),
        ("2026-01-02", date(2026, 1, 2), True),
        ("1/2/2026", date(2026, 1, 2), False),
        ("2026-01-02", datetime(2026, 1, 2), False),
        ("", None, True),
    ],
)
def test_mixed_format_comparison_uses_ingested_strings_without_smart_equivalence(
    csv_value, xlsx_value, matches, xlsx_bytes
):
    a = ingest_csv(
        f"id,amount,Department\nINV,1,{csv_value}\n",
        source=Source.A,
        key_columns=("id",),
        amount_column="amount",
        comparison_columns=("Department",),
    )
    b = ingest_xlsx(
        xlsx_bytes({"Ledger": [["id", "amount", "Cost Center"], ["INV", 1, xlsx_value]]}),
        source=Source.B,
        worksheet="Ledger",
        key_columns=("id",),
        amount_column="amount",
        comparison_columns=("Cost Center",),
    )
    comparison = reconcile(a, b, comparison_fields=FIELDS).findings[0].field_comparisons[0]

    assert comparison.status is (
        FieldComparisonStatus.MATCH if matches else FieldComparisonStatus.MISMATCH
    )


@pytest.mark.parametrize("source", list(Source))
def test_engine_requires_evidence_for_every_configured_mapping(source):
    a = (_record(Source.A, 9, extra={} if source is Source.A else {"Currency": "USD"}),)
    b = (_record(Source.B, 9, extra={} if source is Source.B else {"Currency Code": "USD"}),)
    with pytest.raises(ValueError) as error:
        reconcile(a, b, comparison_fields=(DEPARTMENT, CURRENCY))
    assert f"File {source.value} source row 9" in str(error.value)
    assert ("Currency" if source is Source.A else "Currency Code") in str(error.value)


@pytest.mark.parametrize(
    ("second_amount_b", "category", "delta"),
    [
        ("70", FindingCategory.EXACT_MATCH, "0"),
        ("70.01", FindingCategory.WITHIN_TOLERANCE, "-0.01"),
        ("71", FindingCategory.AMOUNT_MISMATCH, "-1"),
    ],
)
@pytest.mark.parametrize("mismatch", [False, True])
def test_grouped_comparisons_preserve_amount_tolerance_and_secondary_exception_membership(
    second_amount_b, category, delta, mismatch
):
    a = (_record(Source.A, 2, amount="60"), _record(Source.A, 3, amount="40"))
    b = (
        _record(Source.B, 7, amount="30"),
        _record(Source.B, 8, "Marketing" if mismatch else "Sales", amount=second_amount_b),
    )
    result = reconcile(
        a,
        b,
        mode=ReconciliationMode.GROUPED_BY_KEY,
        amount_tolerance=Decimal("0.01"),
        comparison_fields=FIELDS,
    )
    finding = result.findings[0]

    assert finding.category is category
    assert finding.field_comparisons[0].status is (
        FieldComparisonStatus.MISMATCH if mismatch else FieldComparisonStatus.MATCH
    )
    assert finding.delta == result.control_difference == result.finding_delta_sum == Decimal(delta)
    requires_review = category is FindingCategory.AMOUNT_MISMATCH or mismatch
    accepted_tolerance = category is FindingCategory.WITHIN_TOLERANCE and not mismatch
    assert finding.is_exception is requires_review
    assert result.exceptions == ((finding,) if requires_review else ())
    assert result.tolerated_findings == ((finding,) if accepted_tolerance else ())
    assert result.tolerated_delta_total == Decimal("-0.01" if accepted_tolerance else "0")
    assert _primary_only(result) == reconcile(
        a, b, mode=ReconciliationMode.GROUPED_BY_KEY, amount_tolerance=Decimal("0.01")
    )
