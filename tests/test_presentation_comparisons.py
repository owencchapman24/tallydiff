"""Secondary review filters and summaries preserve complete results and evidence."""

from dataclasses import replace
from decimal import Decimal

import pytest

from tallydiff import (
    ComparisonFieldMapping,
    FieldComparison,
    FieldComparisonStatus,
    FindingCategory,
    Source,
    SourceRecord,
    export_exceptions_csv,
    reconcile,
)
from tallydiff.presentation import (
    EXCEPTION_CATEGORIES,
    REVIEW_CATEGORIES,
    finding_rows,
    review_exceptions,
)

DEPARTMENT = ComparisonFieldMapping("Department", "Cost Center")
CURRENCY = ComparisonFieldMapping("Currency", "Currency Code")
DATE = ComparisonFieldMapping("Posting Date", "Document Date")


def _record(source, row, key, amount="1.00", value="Sales"):
    column = "Department" if source is Source.A else "Cost Center"
    return SourceRecord(source, row, (key,), Decimal(amount), {column: value})


@pytest.fixture
def comparison_result():
    keys = [
        "accepted-exact",
        "accepted-tolerance",
        "exact-secondary",
        "tolerance-secondary",
        "amount-primary",
        "amount-secondary",
    ]
    a = [_record(Source.A, row, key) for row, key in enumerate(keys, start=2)]
    b = [
        _record(Source.B, row, key, amount, value)
        for row, (key, amount, value) in enumerate(
            zip(
                keys,
                ["1.00", "1.01", "1.00", "1.01", "2.00", "2.00"],
                ["Sales", "Sales", "Marketing", "Marketing", "Sales", "Marketing"],
                strict=True,
            ),
            start=2,
        )
    ]
    a.extend(
        [
            _record(Source.A, 10, "a-only"),
            _record(Source.A, 12, "duplicate"),
            _record(Source.A, 11, "duplicate"),
        ]
    )
    b.extend([_record(Source.B, 10, "b-only"), _record(Source.B, 11, "duplicate", "2.00")])
    return reconcile(a, b, amount_tolerance=Decimal("0.01"), comparison_fields=(DEPARTMENT,))


def test_finding_rows_keeps_exact_legacy_shape_when_no_comparisons_exist():
    a = (_record(Source.A, 2, "INV"),)
    result = reconcile(a, ())
    assert finding_rows(result.findings) == [
        {
            "Category": "File A only",
            "Matching key": "INV",
            "File A amount": "1.00",
            "File B amount": "—",
            "Delta (A - B)": "+1.00",
            "File A records": "2",
            "File B records": "",
        }
    ]
    assert finding_rows(()) == []


def test_finding_rows_names_only_mismatches_in_configuration_order(comparison_result):
    original = comparison_result.findings[0]
    comparisons = (
        FieldComparison(CURRENCY, ("USD",), ("EUR",), FieldComparisonStatus.MISMATCH),
        FieldComparison(DATE, ("2026",), ("2026",), FieldComparisonStatus.MATCH),
        FieldComparison(DEPARTMENT, ("Sales",), ("Marketing",), FieldComparisonStatus.MISMATCH),
    )
    finding = replace(original, field_comparisons=comparisons)
    row = finding_rows((finding,))[0]
    assert row["Secondary differences"] == "Currency ↔ Currency Code; Department ↔ Cost Center"
    assert "Posting Date" not in row["Secondary differences"]
    assert "Sales" not in row["Secondary differences"]
    assert finding.field_comparisons is comparisons
    assert finding.rows_a is original.rows_a and finding.rows_b is original.rows_b
    assert finding_rows((finding,)) == [row]


@pytest.mark.parametrize("status", list(FieldComparisonStatus))
def test_finding_rows_secondary_summary_lists_only_mismatch(status, comparison_result):
    comparison = FieldComparison(
        DEPARTMENT,
        ("Sales",),
        ("Sales",) if status is FieldComparisonStatus.MATCH else ("Marketing",),
        status,
    )
    finding = replace(comparison_result.findings[0], field_comparisons=(comparison,))
    assert finding_rows((finding,))[0]["Secondary differences"] == (
        "Department ↔ Cost Center" if status is FieldComparisonStatus.MISMATCH else ""
    )


def test_mixed_finding_table_uses_one_consistent_secondary_column(comparison_result):
    configured = next(f for f in comparison_result.findings if f.key == ("exact-secondary",))
    legacy = replace(configured, field_comparisons=())
    rows = finding_rows((legacy, configured))
    assert list(rows[0]) == list(rows[1])
    assert rows[0]["Secondary differences"] == ""
    assert rows[1]["Secondary differences"] == "Department ↔ Cost Center"


def test_review_constants_preserve_old_import_and_offer_all_primary_categories():
    assert EXCEPTION_CATEGORIES == (
        FindingCategory.AMOUNT_MISMATCH,
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
        FindingCategory.DUPLICATE_AMBIGUOUS,
    )
    assert REVIEW_CATEGORIES == tuple(FindingCategory)


@pytest.mark.parametrize(
    "category,keys",
    [
        (FindingCategory.EXACT_MATCH, ["exact-secondary"]),
        (FindingCategory.WITHIN_TOLERANCE, ["tolerance-secondary"]),
        (FindingCategory.AMOUNT_MISMATCH, ["amount-primary", "amount-secondary"]),
        (FindingCategory.A_ONLY, ["a-only"]),
        (FindingCategory.B_ONLY, ["b-only"]),
        (FindingCategory.DUPLICATE_AMBIGUOUS, ["duplicate"]),
    ],
)
def test_all_six_review_categories_are_valid_but_accepted_findings_stay_hidden(
    category, keys, comparison_result
):
    visible = review_exceptions(comparison_result.findings, categories=(category,))
    assert [f.key[0] for f in visible] == keys
    assert all(f.is_exception for f in visible)


@pytest.mark.parametrize("use_exceptions", [False, True])
def test_default_review_excludes_accepted_findings_and_includes_every_exception_category(
    use_exceptions, comparison_result
):
    supplied = comparison_result.exceptions if use_exceptions else comparison_result.findings
    assert review_exceptions(supplied) == comparison_result.exceptions
    assert review_exceptions(tuple(reversed(supplied))) == comparison_result.exceptions
    assert {f.category for f in review_exceptions(supplied)} == set(FindingCategory)


@pytest.mark.parametrize(
    "category", [FindingCategory.EXACT_MATCH, FindingCategory.WITHIN_TOLERANCE]
)
def test_selecting_an_accepted_category_cannot_admit_accepted_findings(category, comparison_result):
    accepted = tuple(f for f in comparison_result.findings if not f.is_exception)
    assert review_exceptions(accepted, categories=(category,)) == ()


@pytest.mark.parametrize("secondary_only", [False, True])
def test_secondary_filter_requires_mismatch_not_not_comparable(secondary_only, comparison_result):
    visible = review_exceptions(
        comparison_result.findings, secondary_mismatches_only=secondary_only
    )
    assert visible == (
        comparison_result.secondary_mismatch_findings
        if secondary_only
        else comparison_result.exceptions
    )
    if secondary_only:
        assert [f.key[0] for f in visible] == [
            "amount-secondary",
            "exact-secondary",
            "tolerance-secondary",
        ]


@pytest.mark.parametrize("value", [None, 0, 1, "true", "", [], Decimal("0")])
def test_secondary_filter_requires_an_actual_bool(value, comparison_result):
    with pytest.raises(TypeError, match="secondary_mismatches_only must be a bool"):
        review_exceptions(comparison_result.findings, secondary_mismatches_only=value)


@pytest.mark.parametrize("minimum,keys", [("0", ["exact-secondary"]), ("1E-100", [])])
def test_zero_delta_secondary_exception_obeys_inclusive_numeric_filter(
    minimum, keys, comparison_result
):
    visible = review_exceptions(
        comparison_result.findings,
        categories=(FindingCategory.EXACT_MATCH,),
        minimum_abs_delta=Decimal(minimum),
    )
    assert [f.key[0] for f in visible] == keys


@pytest.mark.parametrize(
    "sort_order,keys",
    [
        ("matching_key", ["amount-secondary", "exact-secondary", "tolerance-secondary"]),
        ("absolute_delta_desc", ["amount-secondary", "tolerance-secondary", "exact-secondary"]),
        ("absolute_delta_asc", ["exact-secondary", "tolerance-secondary", "amount-secondary"]),
    ],
)
def test_secondary_review_preserves_existing_sort_orders(sort_order, keys, comparison_result):
    visible = review_exceptions(
        tuple(reversed(comparison_result.findings)),
        secondary_mismatches_only=True,
        sort_order=sort_order,
    )
    assert [f.key[0] for f in visible] == keys


def test_review_filters_compose_without_changing_complete_export_or_evidence(comparison_result):
    before = export_exceptions_csv(comparison_result)
    findings = comparison_result.findings
    evidence = [
        (f.key, tuple((r, dict(r.raw_fields)) for r in (*f.rows_a, *f.rows_b))) for f in findings
    ]
    visible = review_exceptions(
        findings,
        query=" SECONDARY ",
        categories=(FindingCategory.AMOUNT_MISMATCH, FindingCategory.WITHIN_TOLERANCE),
        minimum_abs_delta=Decimal("0.01"),
        sort_order="absolute_delta_desc",
        secondary_mismatches_only=True,
    )
    assert [f.key[0] for f in visible] == ["amount-secondary", "tolerance-secondary"]
    assert all(any(f is original for original in findings) for f in visible)
    assert comparison_result.findings is findings
    assert evidence == [
        (f.key, tuple((r, dict(r.raw_fields)) for r in (*f.rows_a, *f.rows_b))) for f in findings
    ]
    assert export_exceptions_csv(comparison_result) == before
    assert len(comparison_result.exceptions) == 7
    assert len(comparison_result.secondary_mismatch_findings) == 3
    assert comparison_result.tolerated_delta_total == Decimal("-0.01")


def test_secondary_review_handles_ten_thousand_mixed_groups():
    a = tuple(_record(Source.A, index + 2, f"INV-{index:05d}", "10") for index in range(10_000))
    b = tuple(
        _record(
            Source.B,
            index + 2,
            f"INV-{index:05d}",
            "11" if index % 3 == 2 else "10",
            "Marketing" if index % 3 == 1 else "Sales",
        )
        for index in range(10_000)
    )
    result = reconcile(a, b, comparison_fields=(DEPARTMENT,))
    visible = review_exceptions(
        tuple(reversed(result.findings)),
        query="INV-09",
        categories=(FindingCategory.EXACT_MATCH,),
        secondary_mismatches_only=True,
        sort_order="absolute_delta_desc",
    )
    assert [f.key for f in visible] == [
        (f"INV-{index:05d}",) for index in range(9000, 10_000) if index % 3 == 1
    ]
    assert all(f.rows_a[0] is a[f.rows_a[0].source_row - 2] for f in visible)
    assert len(result.secondary_mismatch_findings) == 3333
