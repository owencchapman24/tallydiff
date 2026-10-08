from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
from itertools import product

import pytest

from tallydiff import (
    FindingCategory,
    ReconciliationIntegrityError,
    Source,
    SourceRecord,
    reconcile,
)
from tallydiff.engine import _assert_integrity


def record(
    source: Source,
    source_row: int,
    key: tuple[str, ...],
    amount: str,
) -> SourceRecord:
    return SourceRecord(
        source=source,
        source_row=source_row,
        key=key,
        amount=Decimal(amount),
    )


def test_example_reconciliation_explains_control_difference() -> None:
    records_a = [
        record(Source.A, 2, ("V001", "1042"), "1250"),
        record(Source.A, 3, ("V002", "1043"), "800"),
        record(Source.A, 4, ("V003", "1044"), "500"),
    ]
    records_b = [
        record(Source.B, 2, ("V001", "1042"), "1205"),
        record(Source.B, 3, ("V002", "1043"), "800"),
        record(Source.B, 4, ("V004", "1045"), "300"),
    ]

    result = reconcile(records_a, records_b)
    by_key = {finding.key: finding for finding in result.findings}

    assert result.total_a == Decimal("2550")
    assert result.total_b == Decimal("2305")
    assert result.control_difference == Decimal("245")
    assert result.finding_delta_sum == Decimal("245")

    assert by_key[("V001", "1042")].category is FindingCategory.AMOUNT_MISMATCH
    assert by_key[("V001", "1042")].delta == Decimal("45")

    assert by_key[("V002", "1043")].category is FindingCategory.EXACT_MATCH
    assert by_key[("V002", "1043")].delta == Decimal("0")

    assert by_key[("V003", "1044")].category is FindingCategory.A_ONLY
    assert by_key[("V003", "1044")].delta == Decimal("500")

    assert by_key[("V004", "1045")].category is FindingCategory.B_ONLY
    assert by_key[("V004", "1045")].delta == Decimal("-300")

    assert len(result.exceptions) == 3


def test_offsetting_duplicate_remains_an_exception() -> None:
    records_a = [
        record(Source.A, 2, ("INV001",), "100"),
        record(Source.A, 3, ("INV001",), "200"),
    ]
    records_b = [
        record(Source.B, 2, ("INV001",), "150"),
        record(Source.B, 3, ("INV001",), "150"),
    ]

    result = reconcile(records_a, records_b)

    assert result.control_difference == Decimal("0")
    assert result.finding_delta_sum == Decimal("0")
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert finding.is_exception
    assert finding.amount_a == Decimal("300")
    assert finding.amount_b == Decimal("300")
    assert len(finding.rows_a) == 2
    assert len(finding.rows_b) == 2


def test_duplicate_on_one_side_is_not_silently_paired() -> None:
    records_a = [
        record(Source.A, 2, ("INV002",), "50"),
        record(Source.A, 3, ("INV002",), "50"),
    ]
    records_b = [record(Source.B, 2, ("INV002",), "100")]

    result = reconcile(records_a, records_b)

    finding = result.findings[0]
    assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert finding.delta == Decimal("0")
    assert len(finding.rows_a) == 2
    assert len(finding.rows_b) == 1


def test_negative_amounts_reconcile_exactly() -> None:
    records_a = [record(Source.A, 2, ("CREDIT",), "-42.15")]
    records_b = [record(Source.B, 2, ("CREDIT",), "-42.15")]

    result = reconcile(records_a, records_b)

    assert result.findings[0].category is FindingCategory.EXACT_MATCH
    assert result.control_difference == Decimal("0.00")


def test_rejects_duplicate_source_row_numbers() -> None:
    records_a = [
        record(Source.A, 2, ("A",), "10"),
        record(Source.A, 2, ("B",), "20"),
    ]

    with pytest.raises(ValueError, match="duplicate source row 2"):
        reconcile(records_a, [])


@pytest.mark.parametrize("source", [Source.A, Source.B])
def test_rejects_records_supplied_to_the_wrong_file(source: Source) -> None:
    rows = [record(source, 2, ("INV",), "10")]
    with pytest.raises(ValueError, match="belongs to File"):
        reconcile(rows if source is Source.B else [], rows if source is Source.A else [])


@pytest.mark.parametrize(("count_a", "count_b"), list(product(range(3), repeat=2)))
def test_all_group_cardinalities_preserve_rows_and_duplicate_exceptions(
    count_a: int, count_b: int
) -> None:
    # Equal zero totals must never hide duplicates or turn one-sided rows into matches.
    records_a = [record(Source.A, row, ("INV",), "0.00") for row in range(1, count_a + 1)]
    records_b = [record(Source.B, row, ("INV",), "0.00") for row in range(1, count_b + 1)]

    result = reconcile(records_a, records_b)

    assert result.total_a == result.total_b == Decimal("0.00")
    assert result.control_difference == result.finding_delta_sum == Decimal("0")
    accounted = [row for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)]
    assert Counter((row.source, row.source_row) for row in accounted) == Counter(
        (row.source, row.source_row) for row in (*records_a, *records_b)
    )
    assert all(any(row is original for original in (*records_a, *records_b)) for row in accounted)
    if not count_a and not count_b:
        assert result.findings == result.exceptions == ()
        return

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rows_a == tuple(records_a)
    assert finding.rows_b == tuple(records_b)
    if count_a > 1 or count_b > 1:
        category = FindingCategory.DUPLICATE_AMBIGUOUS
    elif not count_a:
        category = FindingCategory.B_ONLY
    elif not count_b:
        category = FindingCategory.A_ONLY
    else:
        category = FindingCategory.EXACT_MATCH
    assert finding.category is category
    assert finding.is_exception is (category is not FindingCategory.EXACT_MATCH)
    assert result.exceptions == ((finding,) if finding.is_exception else ())
    assert all(
        isinstance(amount, Decimal)
        for amount in (
            result.total_a,
            result.total_b,
            result.control_difference,
            result.finding_delta_sum,
            finding.amount_a,
            finding.amount_b,
            finding.delta,
        )
    )


@pytest.mark.parametrize("duplicate_source", [Source.A, Source.B])
def test_nonzero_duplicate_delta_includes_every_row(duplicate_source: Source) -> None:
    duplicates = [
        record(duplicate_source, 2, ("INV",), "10.25"),
        record(duplicate_source, 3, ("INV",), "-3.10"),
    ]
    other_source = Source.B if duplicate_source is Source.A else Source.A
    other = [record(other_source, 2, ("INV",), "2.00")]
    result = reconcile(
        duplicates if duplicate_source is Source.A else other,
        duplicates if duplicate_source is Source.B else other,
    )

    finding = result.findings[0]
    expected = Decimal("5.15") if duplicate_source is Source.A else Decimal("-5.15")
    assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert result.exceptions == (finding,)
    assert finding.delta == result.control_difference == result.finding_delta_sum == expected
    assert len(finding.rows_a) + len(finding.rows_b) == 3


def test_decimal_arithmetic_is_independent_of_the_callers_context() -> None:
    records_a = [record(Source.A, 1, ("INV",), "1000.01")]
    records_b = [record(Source.B, 1, ("INV",), "1000.02")]
    result = reconcile(records_a, records_b)

    with localcontext() as context:
        context.prec = 3
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        before = context.copy()

        assert reconcile(records_a, records_b) == result
        assert result.total_a == Decimal("1000.01")
        assert result.total_b == Decimal("1000.02")
        assert result.findings[0].category is FindingCategory.AMOUNT_MISMATCH
        assert result.findings[0].delta == Decimal("-0.01")
        assert result.control_difference == result.finding_delta_sum == Decimal("-0.01")
        assert context.prec == before.prec
        assert context.Emax == before.Emax
        assert context.Emin == before.Emin
        assert context.traps == before.traps
        assert context.flags == before.flags


@pytest.mark.parametrize("duplicate_key", [False, True])
def test_large_amounts_and_cancellation_do_not_lose_fractional_values(duplicate_key: bool) -> None:
    keys = [("INV",)] * 3 if duplicate_key else [("A",), ("B",), ("C",)]
    records_a = [
        record(Source.A, 1, keys[0], "1000000000000000000000000000000"),
        record(Source.A, 2, keys[1], "0.01"),
        record(Source.A, 3, keys[2], "-1000000000000000000000000000000"),
    ]

    result = reconcile(records_a, [])
    assert (
        result.total_a == result.control_difference == result.finding_delta_sum == Decimal("0.01")
    )
    assert reconcile(list(reversed(records_a)), []) == result
    if duplicate_key:
        assert result.findings[0].amount_a == Decimal("0.01")
        assert result.findings[0].category is FindingCategory.DUPLICATE_AMBIGUOUS


def test_input_order_does_not_change_findings_or_source_row_order() -> None:
    records_a = [
        record(Source.A, 9, ("Z",), "1.20"),
        record(Source.A, 4, ("A", "2"), "3.40"),
        record(Source.A, 2, ("Z",), "5.60"),
        record(Source.A, 3, ("A", "1"), "0.00"),
    ]
    records_b = [
        record(Source.B, 8, ("Z",), "-1.25"),
        record(Source.B, 1, ("Z",), "1.25"),
    ]

    result = reconcile(records_a, records_b)

    assert result == reconcile(list(reversed(records_a)), list(reversed(records_b)))
    assert [finding.key for finding in result.findings] == [("A", "1"), ("A", "2"), ("Z",)]
    assert [row.source_row for row in result.findings[-1].rows_a] == [2, 9]
    assert [row.source_row for row in result.findings[-1].rows_b] == [1, 8]


def test_one_pass_inputs_are_not_silently_consumed_by_validation() -> None:
    records_a = [record(Source.A, 1, ("INV",), "0.10")]
    records_b = [record(Source.B, 1, ("INV",), "0.20")]

    assert reconcile(iter(records_a), iter(records_b)) == reconcile(records_a, records_b)


@pytest.mark.parametrize("corruption", ["repeated", "missing", "unknown"])
def test_integrity_check_rejects_incorrect_row_accounting_even_with_zero_delta(
    corruption: str,
) -> None:
    rows = [record(Source.A, 1, ("INV",), "0")]
    result = reconcile(rows, [])
    finding = result.findings[0]
    if corruption == "repeated":
        finding = replace(finding, rows_a=(rows[0], rows[0]))
    elif corruption == "missing":
        finding = replace(finding, rows_a=())
    else:
        finding = replace(finding, rows_a=(record(Source.A, 2, ("INV",), "0"),))
    corrupted = replace(result, findings=(finding,))

    with pytest.raises(ReconciliationIntegrityError, match="exactly once"):
        _assert_integrity(corrupted, rows, [])


def test_integrity_check_rejects_an_unexplained_control_difference() -> None:
    rows = [record(Source.A, 1, ("INV",), "1.25")]
    result = replace(reconcile(rows, []), total_a=Decimal("2.25"))

    with pytest.raises(ReconciliationIntegrityError, match="control-total difference"):
        _assert_integrity(result, rows, [])


@pytest.mark.parametrize("explicit_zero", [False, True])
def test_default_and_zero_tolerance_preserve_all_original_categories(explicit_zero: bool) -> None:
    a = [
        record(Source.A, 2, ("exact",), "100.00"),
        record(Source.A, 3, ("mismatch",), "100.00"),
        record(Source.A, 4, ("a-only",), "0.001"),
        record(Source.A, 5, ("duplicate",), "50.00"),
        record(Source.A, 6, ("duplicate",), "50.00"),
    ]
    b = [
        record(Source.B, 2, ("exact",), "100.00"),
        record(Source.B, 3, ("mismatch",), "100.01"),
        record(Source.B, 4, ("b-only",), "0.001"),
        record(Source.B, 5, ("duplicate",), "100.00"),
    ]
    result = reconcile(a, b, amount_tolerance=Decimal("0")) if explicit_zero else reconcile(a, b)
    assert [finding.category for finding in result.findings] == [
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
        FindingCategory.DUPLICATE_AMBIGUOUS,
        FindingCategory.EXACT_MATCH,
        FindingCategory.AMOUNT_MISMATCH,
    ]
    assert len(result.exceptions) == 4
    assert result.tolerated_findings == ()
    assert result.amount_tolerance == result.tolerated_delta_total == Decimal("0")
    assert result.control_difference == result.finding_delta_sum == Decimal("-0.01")


@pytest.mark.parametrize(
    ("amount_a", "amount_b", "tolerance", "category", "delta"),
    [
        ("100.00", "100.00", "0.01", FindingCategory.EXACT_MATCH, "0.00"),
        ("100.00", "99.99", "0.01", FindingCategory.WITHIN_TOLERANCE, "0.01"),
        ("100.00", "100.01", "0.01", FindingCategory.WITHIN_TOLERANCE, "-0.01"),
        ("100.00", "100.009", "0.01", FindingCategory.WITHIN_TOLERANCE, "-0.009"),
        ("100.00", "100.0101", "0.01", FindingCategory.AMOUNT_MISMATCH, "-0.0101"),
        ("100.00", "100.02", "0.01", FindingCategory.AMOUNT_MISMATCH, "-0.02"),
        ("0.0000", "0.0001", "0.0001", FindingCategory.WITHIN_TOLERANCE, "-0.0001"),
        ("-100.00", "-100.01", "0.01", FindingCategory.WITHIN_TOLERANCE, "0.01"),
    ],
)
def test_absolute_tolerance_classification_preserves_true_delta(
    amount_a: str, amount_b: str, tolerance: str, category: FindingCategory, delta: str
) -> None:
    a = record(Source.A, 2, ("INV",), amount_a)
    b = record(Source.B, 2, ("INV",), amount_b)
    result = reconcile([a], [b], amount_tolerance=Decimal(tolerance))
    finding = result.findings[0]
    assert finding.category is category
    assert finding.amount_a == Decimal(amount_a)
    assert finding.amount_b == Decimal(amount_b)
    assert finding.delta == result.control_difference == result.finding_delta_sum == Decimal(delta)
    assert result.amount_tolerance == Decimal(tolerance)
    assert finding.rows_a[0] is a and finding.rows_b[0] is b
    accepted = category is FindingCategory.WITHIN_TOLERANCE
    assert result.tolerated_findings == ((finding,) if accepted else ())
    assert result.tolerated_delta_total == (Decimal(delta) if accepted else Decimal("0"))
    assert finding.is_exception is (category is FindingCategory.AMOUNT_MISMATCH)


@pytest.mark.parametrize("tolerance", [0.01, 0, "0.01", True, None])
def test_tolerance_rejects_non_decimal_values(tolerance: object) -> None:
    with pytest.raises(TypeError, match="amount_tolerance must be a Decimal"):
        reconcile([], [], amount_tolerance=tolerance)


@pytest.mark.parametrize("tolerance", ["NaN", "sNaN", "Infinity", "-Infinity", "-0.01"])
def test_tolerance_rejects_nonfinite_or_negative_decimals(tolerance: str) -> None:
    with pytest.raises(ValueError, match="amount_tolerance must be (finite|nonnegative)"):
        reconcile([], [], amount_tolerance=Decimal(tolerance))


@pytest.mark.parametrize(("count_a", "count_b"), [(2, 1), (1, 2), (2, 2), (2, 0), (0, 2)])
def test_duplicates_ignore_tolerance_even_when_the_group_delta_is_accepted(
    count_a: int, count_b: int
) -> None:
    a = [record(Source.A, row, ("INV",), "0.001") for row in range(2, count_a + 2)]
    b = [record(Source.B, row, ("INV",), "0.001") for row in range(2, count_b + 2)]
    result = reconcile(a, b, amount_tolerance=Decimal("1"))
    finding = result.findings[0]
    assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert finding.rows_a == tuple(a) and finding.rows_b == tuple(b)
    assert result.exceptions == (finding,)
    assert not result.tolerated_findings
    assert result.control_difference == result.finding_delta_sum


def test_one_sided_records_remain_exceptions_under_large_tolerance() -> None:
    a = [record(Source.A, 2, ("A",), "0.001")]
    b = [record(Source.B, 2, ("B",), "0.001")]
    result = reconcile(a, b, amount_tolerance=Decimal("1000"))
    assert [finding.category for finding in result.exceptions] == [
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
    ]
    assert not result.tolerated_findings
    assert result.control_difference == result.finding_delta_sum == Decimal("0")


def test_mixed_tolerated_deltas_remain_in_control_totals_and_keep_row_order() -> None:
    a = [record(Source.A, row, (key,), "100.00") for row, key in enumerate("DCBA", start=2)]
    b = [
        record(Source.B, 2, ("A",), "100.01"),
        record(Source.B, 3, ("B",), "99.99"),
        record(Source.B, 4, ("C",), "99.995"),
        record(Source.B, 5, ("D",), "100.02"),
    ]
    result = reconcile(iter(a), iter(b), amount_tolerance=Decimal("0.01"))
    assert result == reconcile(reversed(a), reversed(b), amount_tolerance=Decimal("0.01"))
    assert [finding.delta for finding in result.tolerated_findings] == [
        Decimal("-0.01"),
        Decimal("0.01"),
        Decimal("0.005"),
    ]
    assert result.tolerated_delta_total == Decimal("0.005")
    assert [finding.key for finding in result.exceptions] == [("D",)]
    assert result.control_difference == result.finding_delta_sum == Decimal("-0.015")
    assert (
        len([row for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)]) == 8
    )


def test_tolerance_boundary_and_accepted_sum_ignore_the_callers_decimal_context() -> None:
    a = [
        record(Source.A, 2, ("inside",), "1000.0000"),
        record(Source.A, 3, ("outside",), "1000.0000"),
    ]
    b = [
        record(Source.B, 2, ("inside",), "1000.0100"),
        record(Source.B, 3, ("outside",), "1000.0101"),
    ]
    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        result = reconcile(a, b, amount_tolerance=Decimal("0.01"))
        assert [finding.category for finding in result.findings] == [
            FindingCategory.WITHIN_TOLERANCE,
            FindingCategory.AMOUNT_MISMATCH,
        ]
        assert result.tolerated_delta_total == Decimal("-0.0100")
        assert result.control_difference == result.finding_delta_sum == Decimal("-0.0201")
        assert not any(context.flags.values())
