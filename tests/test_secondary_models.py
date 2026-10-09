"""Immutable secondary result contracts; execution and review remain primary-only."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

import tallydiff
from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    FieldComparison,
    FieldComparisonStatus,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    reconcile,
)
from tallydiff.configuration import ComparisonFieldMapping as ConfigurationMapping
from tallydiff.models import FieldComparison as ModelComparison
from tallydiff.models import FieldComparisonStatus as ModelStatus

DEPARTMENT = ComparisonFieldMapping("Department", "Cost Center")
POSTING_DATE = ComparisonFieldMapping("Posting Date", "Document Date")
CURRENCY = ComparisonFieldMapping("Currency", "Currency Code")
MATCH = FieldComparison(DEPARTMENT, ("Sales",), ("Sales",), FieldComparisonStatus.MATCH)
MISMATCH = FieldComparison(DEPARTMENT, ("Sales",), ("Marketing",), FieldComparisonStatus.MISMATCH)


def _finding(
    comparisons=(), category=FindingCategory.EXACT_MATCH, amount_b=Decimal("100")
) -> ReconciliationFinding:
    rows_a = (SourceRecord(Source.A, 2, ("INV",), Decimal("100")),)
    rows_b = (SourceRecord(Source.B, 2, ("INV",), amount_b),)
    return ReconciliationFinding(
        category,
        ("INV",),
        rows_a,
        rows_b,
        Decimal("100"),
        amount_b,
        field_comparisons=comparisons,
    )


def test_secondary_types_are_exported_from_the_public_root() -> None:
    assert ComparisonFieldMapping is ConfigurationMapping
    assert FieldComparison is ModelComparison
    assert FieldComparisonStatus is ModelStatus
    assert {"ComparisonFieldMapping", "FieldComparison", "FieldComparisonStatus"} <= set(
        tallydiff.__all__
    )


def test_secondary_statuses_are_exact_and_primary_categories_are_unchanged() -> None:
    assert [(status.name, status.value) for status in FieldComparisonStatus] == [
        ("MATCH", "match"),
        ("MISMATCH", "mismatch"),
        ("NOT_COMPARABLE", "not_comparable"),
    ]
    assert str(FieldComparisonStatus.MATCH) == "match"
    assert [category.value for category in FindingCategory] == [
        "exact_match",
        "within_tolerance",
        "amount_mismatch",
        "a_only",
        "b_only",
        "duplicate_ambiguous",
    ]


@pytest.mark.parametrize(
    "mapping", [None, ("Department", "Cost Center"), "Department", ColumnMapping((), None, None)]
)
def test_field_comparison_requires_a_mapping_object(mapping) -> None:
    with pytest.raises(TypeError, match="mapping must be a ComparisonFieldMapping"):
        FieldComparison(mapping, (), (), FieldComparisonStatus.NOT_COMPARABLE)


@pytest.mark.parametrize(
    "mapping",
    [
        ComparisonFieldMapping(None, "Cost Center"),
        ComparisonFieldMapping("Department", None),
        ComparisonFieldMapping(None, None),
        ComparisonFieldMapping("", "Cost Center"),
        ComparisonFieldMapping("Department", ""),
        ComparisonFieldMapping(" \t", "Cost Center"),
        ComparisonFieldMapping("Department", "\u2003"),
    ],
)
def test_field_comparison_requires_complete_nonblank_columns(mapping) -> None:
    with pytest.raises(ValueError, match="nonblank columns for both files"):
        FieldComparison(mapping, (), (), FieldComparisonStatus.NOT_COMPARABLE)


@pytest.mark.parametrize("side", ["values_a", "values_b"])
@pytest.mark.parametrize(
    "values",
    [
        None,
        [],
        ["Sales"],
        "Sales",
        frozenset({"Sales"}),
        (None,),
        (1,),
        (True,),
        ("Sales", object()),
    ],
)
def test_field_comparison_requires_tuples_of_strings(side, values) -> None:
    with pytest.raises(TypeError, match=f"{side} must be a tuple of strings"):
        replace(MATCH, status=FieldComparisonStatus.NOT_COMPARABLE, **{side: values})


@pytest.mark.parametrize("side", ["values_a", "values_b"])
@pytest.mark.parametrize(
    "values", [("Sales", "Marketing"), ("a", "A"), ("Sales", "Sales"), ("", "")]
)
def test_field_comparison_rejects_unsorted_or_repeated_values(side, values) -> None:
    with pytest.raises(ValueError, match=f"{side} must be sorted and duplicate-free"):
        replace(MATCH, status=FieldComparisonStatus.NOT_COMPARABLE, **{side: values})


def test_field_comparison_retains_exact_sorted_distinct_values_without_normalizing() -> None:
    values = ("", " Sales ", "A", "a", "e\u0301", "é")
    comparison = FieldComparison(DEPARTMENT, values, values, FieldComparisonStatus.MATCH)

    assert comparison.mapping is DEPARTMENT
    assert comparison.values_a is comparison.values_b is values
    assert comparison == FieldComparison(DEPARTMENT, values, values, FieldComparisonStatus.MATCH)


@pytest.mark.parametrize(
    ("values_a", "values_b", "status"),
    [
        (("Sales",), ("Sales",), FieldComparisonStatus.MATCH),
        (("Marketing", "Sales"), ("Marketing", "Sales"), FieldComparisonStatus.MATCH),
        (("",), ("",), FieldComparisonStatus.MATCH),
        ((), (), FieldComparisonStatus.MATCH),
        (("Sales",), ("Marketing",), FieldComparisonStatus.MISMATCH),
        (("",), ("Sales",), FieldComparisonStatus.MISMATCH),
        (("",), (), FieldComparisonStatus.MISMATCH),
        ((), ("Sales",), FieldComparisonStatus.NOT_COMPARABLE),
        (("Sales",), (), FieldComparisonStatus.NOT_COMPARABLE),
        ((), (), FieldComparisonStatus.NOT_COMPARABLE),
        (("Sales",), ("Sales",), FieldComparisonStatus.NOT_COMPARABLE),
        (("Sales",), ("Marketing",), FieldComparisonStatus.NOT_COMPARABLE),
    ],
)
def test_field_comparison_accepts_consistent_status_and_blank_or_missing_sides(
    values_a, values_b, status
) -> None:
    comparison = FieldComparison(DEPARTMENT, values_a, values_b, status)

    assert comparison.values_a == values_a
    assert comparison.values_b == values_b
    assert comparison.status is status


@pytest.mark.parametrize(
    ("values_a", "values_b", "status", "message"),
    [
        (("Sales",), ("Marketing",), FieldComparisonStatus.MATCH, "MATCH requires equal"),
        (("",), (), FieldComparisonStatus.MATCH, "MATCH requires equal"),
        (("Sales",), ("Sales",), FieldComparisonStatus.MISMATCH, "MISMATCH requires unequal"),
        ((), (), FieldComparisonStatus.MISMATCH, "MISMATCH requires unequal"),
        (("",), ("",), FieldComparisonStatus.MISMATCH, "MISMATCH requires unequal"),
    ],
)
def test_field_comparison_rejects_status_inconsistent_with_values(
    values_a, values_b, status, message
) -> None:
    with pytest.raises(ValueError, match=message):
        FieldComparison(DEPARTMENT, values_a, values_b, status)


@pytest.mark.parametrize("status", ["match", "mismatch", "not_comparable", None, True, 1, Source.A])
def test_field_comparison_requires_an_actual_status_enum(status) -> None:
    with pytest.raises(TypeError, match="status must be a FieldComparisonStatus enum member"):
        replace(MATCH, status=status)


@pytest.mark.parametrize("name", ["mapping", "values_a", "values_b", "status"])
def test_field_comparison_is_frozen_and_slotted(name) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(MATCH, name, None)
    assert not hasattr(MATCH, "__dict__")
    with pytest.raises(TypeError):
        MATCH.values_a[0] = "changed"


def test_finding_preserves_all_existing_positional_arguments_and_empty_default() -> None:
    finding = _finding()
    positional = ReconciliationFinding(
        finding.category,
        finding.key,
        finding.rows_a,
        finding.rows_b,
        finding.amount_a,
        finding.amount_b,
    )

    assert finding == positional == replace(positional, field_comparisons=())
    assert positional.field_comparisons == ()
    assert positional.has_secondary_mismatch is False
    assert positional.secondary_mismatches == ()
    with pytest.raises(TypeError):
        ReconciliationFinding(
            finding.category,
            finding.key,
            finding.rows_a,
            finding.rows_b,
            finding.amount_a,
            finding.amount_b,
            (MATCH,),
        )


@pytest.mark.parametrize(
    "comparisons", [None, [], [MATCH], {}, "match", (None,), (DEPARTMENT,), (MATCH, object())]
)
def test_finding_rejects_wrong_comparison_container_or_member(comparisons) -> None:
    with pytest.raises(TypeError, match="field_comparisons must be a tuple of FieldComparison"):
        _finding(comparisons)


@pytest.mark.parametrize(
    ("comparisons", "has_mismatch"),
    [
        ((), False),
        ((MATCH,), False),
        ((MISMATCH,), True),
        (
            (FieldComparison(DEPARTMENT, ("A",), ("B",), FieldComparisonStatus.NOT_COMPARABLE),),
            False,
        ),
        ((MATCH, MISMATCH), True),
    ],
)
def test_finding_secondary_mismatch_property_is_independent_of_other_statuses(
    comparisons, has_mismatch
) -> None:
    assert _finding(comparisons).has_secondary_mismatch is has_mismatch


def test_finding_retains_comparison_order_and_ordered_mismatch_subset() -> None:
    date_match = FieldComparison(
        POSTING_DATE, ("2026-01-01",), ("2026-01-01",), FieldComparisonStatus.MATCH
    )
    currency_mismatch = FieldComparison(
        CURRENCY, ("USD",), ("EUR",), FieldComparisonStatus.MISMATCH
    )
    comparisons = (currency_mismatch, date_match, MISMATCH)
    finding = _finding(comparisons)

    assert finding.field_comparisons is comparisons
    assert finding.secondary_mismatches == (currency_mismatch, MISMATCH)
    assert finding.secondary_mismatches[0] is currency_mismatch
    assert finding.secondary_mismatches[1] is MISMATCH
    with pytest.raises(FrozenInstanceError):
        finding.field_comparisons = ()


@pytest.mark.parametrize(
    ("category", "is_exception"),
    [
        (FindingCategory.EXACT_MATCH, False),
        (FindingCategory.WITHIN_TOLERANCE, False),
        (FindingCategory.AMOUNT_MISMATCH, True),
        (FindingCategory.A_ONLY, True),
        (FindingCategory.B_ONLY, True),
        (FindingCategory.DUPLICATE_AMBIGUOUS, True),
    ],
)
def test_secondary_mismatch_does_not_yet_change_primary_exception_membership(
    category, is_exception
) -> None:
    finding = _finding((MISMATCH,), category=category)

    assert finding.has_secondary_mismatch is True
    assert finding.is_exception is is_exception
    assert finding.delta == Decimal("0")


def test_result_preserves_all_six_existing_positional_arguments() -> None:
    finding = _finding()
    normalization = KeyNormalizationConfig((KeyNormalizationRules(casefold=True),))
    result = ReconciliationResult(
        Decimal("100"),
        Decimal("100"),
        (finding,),
        Decimal("0.01"),
        ReconciliationMode.GROUPED_BY_KEY,
        normalization,
    )

    assert result == replace(result, comparison_fields=())
    assert result.comparison_fields == ()
    assert result.key_normalization is normalization
    assert result.mode is ReconciliationMode.GROUPED_BY_KEY
    with pytest.raises(TypeError):
        ReconciliationResult(
            Decimal("100"),
            Decimal("100"),
            (finding,),
            Decimal("0.01"),
            ReconciliationMode.GROUPED_BY_KEY,
            normalization,
            (),
        )


@pytest.mark.parametrize(
    "comparisons",
    [None, [], [DEPARTMENT], {}, "Department", (None,), (MATCH,), (DEPARTMENT, object())],
)
def test_result_rejects_wrong_comparison_container_or_member(comparisons) -> None:
    with pytest.raises(
        TypeError, match="comparison_fields must be a tuple of ComparisonFieldMapping"
    ):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), comparison_fields=comparisons)


@pytest.mark.parametrize(
    "mapping",
    [
        ComparisonFieldMapping(None, "Cost Center"),
        ComparisonFieldMapping("Department", None),
        ComparisonFieldMapping(None, None),
        ComparisonFieldMapping("", "Cost Center"),
        ComparisonFieldMapping("Department", ""),
        ComparisonFieldMapping(" \t", "Cost Center"),
        ComparisonFieldMapping("Department", "\u2003"),
    ],
)
def test_result_requires_complete_nonblank_comparison_mappings(mapping) -> None:
    with pytest.raises(ValueError, match="nonblank columns for both files"):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), comparison_fields=(mapping,))


@pytest.mark.parametrize(
    ("side", "second"),
    [
        ("A", ComparisonFieldMapping("Department", "Document Date")),
        ("B", ComparisonFieldMapping("Posting Date", "Cost Center")),
        ("A", DEPARTMENT),
    ],
)
def test_result_rejects_repeated_directional_comparison_columns(side, second) -> None:
    with pytest.raises(
        ValueError, match=f"each File {side} comparison column must be selected only once"
    ):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), comparison_fields=(DEPARTMENT, second))


@pytest.mark.parametrize(
    ("configured", "observed"),
    [
        ((DEPARTMENT,), ()),
        (
            (DEPARTMENT,),
            (MATCH, FieldComparison(POSTING_DATE, (), (), FieldComparisonStatus.NOT_COMPARABLE)),
        ),
        (
            (DEPARTMENT, POSTING_DATE),
            (FieldComparison(POSTING_DATE, (), (), FieldComparisonStatus.NOT_COMPARABLE), MATCH),
        ),
        ((POSTING_DATE,), (MATCH,)),
        ((), (MATCH,)),
    ],
)
def test_result_rejects_finding_configuration_count_or_mapping_order_mismatch(
    configured, observed
) -> None:
    with pytest.raises(
        ValueError, match="field_comparisons must match comparison_fields in count and order"
    ):
        ReconciliationResult(
            Decimal("100"), Decimal("100"), (_finding(observed),), comparison_fields=configured
        )


def test_result_checks_configuration_consistency_for_every_finding() -> None:
    first = _finding((MATCH,))
    second = replace(_finding(), key=("OTHER",))

    with pytest.raises(ValueError, match="field_comparisons must match comparison_fields"):
        ReconciliationResult(
            Decimal("200"), Decimal("200"), (first, second), comparison_fields=(DEPARTMENT,)
        )


def test_result_retains_ordered_configuration_and_compares_mappings_by_value() -> None:
    equivalent = ComparisonFieldMapping("Department", "Cost Center")
    date_comparison = FieldComparison(POSTING_DATE, (), (), FieldComparisonStatus.NOT_COMPARABLE)
    department_comparison = replace(MATCH, mapping=equivalent)
    configured = (POSTING_DATE, DEPARTMENT)
    finding = _finding((date_comparison, department_comparison))
    result = ReconciliationResult(
        Decimal("100"), Decimal("100"), (finding,), comparison_fields=configured
    )

    assert equivalent is not DEPARTMENT
    assert result.comparison_fields is configured
    assert result.findings[0] is finding
    assert result == replace(result, comparison_fields=(POSTING_DATE, equivalent))
    with pytest.raises(FrozenInstanceError):
        result.comparison_fields = ()


def test_header_only_result_retains_nonempty_comparison_configuration() -> None:
    configured = (POSTING_DATE, DEPARTMENT)
    result = ReconciliationResult(Decimal("0"), Decimal("0"), (), comparison_fields=configured)

    assert result.comparison_fields is configured
    assert result.findings == result.exceptions == result.tolerated_findings == ()
    assert result.control_difference == result.finding_delta_sum == Decimal("0")


def test_secondary_mismatch_does_not_yet_change_result_exceptions_or_accepted_tolerance() -> None:
    exact = _finding((MISMATCH,))
    tolerated = _finding((MISMATCH,), FindingCategory.WITHIN_TOLERANCE, Decimal("100.01"))
    exact_result = ReconciliationResult(
        Decimal("100"), Decimal("100"), (exact,), comparison_fields=(DEPARTMENT,)
    )
    tolerated_result = ReconciliationResult(
        Decimal("100"),
        Decimal("100.01"),
        (tolerated,),
        Decimal("0.01"),
        comparison_fields=(DEPARTMENT,),
    )

    assert exact_result.exceptions == exact_result.tolerated_findings == ()
    assert exact_result.control_difference == exact_result.finding_delta_sum == Decimal("0")
    assert tolerated_result.exceptions == ()
    assert tolerated_result.tolerated_findings == (tolerated,)
    assert tolerated_result.tolerated_delta_total == Decimal("-0.01")
    assert (
        tolerated_result.control_difference
        == tolerated_result.finding_delta_sum
        == Decimal("-0.01")
    )


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("tolerance", ["0", "0.01"])
@pytest.mark.parametrize("normalized", [False, True])
def test_current_reconcile_outputs_equal_primary_only_results_in_both_modes(
    mode, tolerance, normalized
) -> None:
    configuration = (
        KeyNormalizationConfig((KeyNormalizationRules(casefold=True),)) if normalized else None
    )
    a = tuple(
        SourceRecord(Source.A, row, (key,), Decimal(amount), {"Department": "Sales"})
        for row, (key, amount) in enumerate(
            [
                ("EXACT", "100"),
                ("SMALL", "100"),
                ("DIFFERENT", "10"),
                ("A_ONLY", "0"),
                ("DUP", "10"),
                ("DUP", "20"),
            ],
            start=2,
        )
    )
    b = tuple(
        SourceRecord(
            Source.B,
            row,
            (key.lower() if normalized else key,),
            Decimal(amount),
            {"Cost Center": "Marketing"},
        )
        for row, (key, amount) in enumerate(
            [
                ("EXACT", "100"),
                ("SMALL", "100.01"),
                ("DIFFERENT", "8"),
                ("B_ONLY", "0"),
                ("DUP", "15"),
                ("DUP", "15"),
            ],
            start=2,
        )
    )
    duplicate_category = (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.EXACT_MATCH
    )
    small_category = (
        FindingCategory.AMOUNT_MISMATCH if tolerance == "0" else FindingCategory.WITHIN_TOLERANCE
    )
    expected_findings = tuple(
        ReconciliationFinding(
            category,
            (key.lower() if normalized else key,),
            rows_a,
            rows_b,
            Decimal(amount_a),
            Decimal(amount_b),
        )
        for category, key, rows_a, rows_b, amount_a, amount_b in [
            (FindingCategory.A_ONLY, "A_ONLY", (a[3],), (), "0", "0"),
            (FindingCategory.B_ONLY, "B_ONLY", (), (b[3],), "0", "0"),
            (FindingCategory.AMOUNT_MISMATCH, "DIFFERENT", (a[2],), (b[2],), "10", "8"),
            (duplicate_category, "DUP", a[4:], b[4:], "30", "30"),
            (FindingCategory.EXACT_MATCH, "EXACT", (a[0],), (b[0],), "100", "100"),
            (small_category, "SMALL", (a[1],), (b[1],), "100", "100.01"),
        ]
    )
    expected = ReconciliationResult(
        Decimal("240"),
        Decimal("238.01"),
        expected_findings,
        Decimal(tolerance),
        mode,
        configuration,
    )
    result = reconcile(
        iter(a),
        reversed(b),
        amount_tolerance=Decimal(tolerance),
        mode=mode,
        key_normalization=configuration,
    )

    assert result == expected
    assert result.comparison_fields == ()
    assert all(finding.field_comparisons == () for finding in result.findings)
    assert all(not finding.has_secondary_mismatch for finding in result.findings)
    assert result.control_difference == result.finding_delta_sum == Decimal("1.99")
    expected_exception_keys = ["A_ONLY", "B_ONLY", "DIFFERENT"]
    if mode is ReconciliationMode.UNIQUE:
        expected_exception_keys.append("DUP")
    if tolerance == "0":
        expected_exception_keys.append("SMALL")
    assert [finding.key for finding in result.exceptions] == [
        (key.lower() if normalized else key,) for key in expected_exception_keys
    ]
    assert result.tolerated_findings == ((expected_findings[-1],) if tolerance == "0.01" else ())
    assert result.tolerated_delta_total == Decimal("-0.01" if tolerance == "0.01" else "0")
    assert result.findings[4].rows_a[0] is a[0]
    assert result.findings[4].rows_b[0] is b[0]
