from collections import Counter
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from itertools import product

import pytest

from tallydiff import (
    FindingCategory,
    ReconciliationIntegrityError,
    ReconciliationMode,
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


@pytest.mark.parametrize("tolerance", ["0", "0.01"])
def test_explicit_unique_preserves_default_and_legacy_categories(tolerance: str) -> None:
    a = [
        record(Source.A, 1, ("a-only",), "0"),
        record(Source.A, 2, ("duplicate",), "50"),
        record(Source.A, 3, ("duplicate",), "50"),
        record(Source.A, 4, ("exact",), "100"),
        record(Source.A, 5, ("mismatch",), "100"),
        record(Source.A, 6, ("small-delta",), "100"),
    ]
    b = [
        record(Source.B, 1, ("b-only",), "0"),
        record(Source.B, 2, ("duplicate",), "100.001"),
        record(Source.B, 3, ("exact",), "100"),
        record(Source.B, 4, ("mismatch",), "101"),
        record(Source.B, 5, ("small-delta",), "100.005"),
    ]
    default = reconcile(a, b, amount_tolerance=Decimal(tolerance))
    explicit = reconcile(a, b, amount_tolerance=Decimal(tolerance), mode=ReconciliationMode.UNIQUE)

    assert default.mode is explicit.mode is ReconciliationMode.UNIQUE
    assert default == explicit
    assert [finding.category for finding in explicit.findings] == [
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
        FindingCategory.DUPLICATE_AMBIGUOUS,
        FindingCategory.EXACT_MATCH,
        FindingCategory.AMOUNT_MISMATCH,
        (
            FindingCategory.WITHIN_TOLERANCE
            if tolerance == "0.01"
            else FindingCategory.AMOUNT_MISMATCH
        ),
    ]


@pytest.mark.parametrize(
    "mode",
    ["unique", "grouped_by_key", "invalid", "", None, 0, True, Decimal("0"), Source.A],
)
def test_reconcile_rejects_invalid_mode_types(mode: object) -> None:
    with pytest.raises(TypeError, match="mode must be a ReconciliationMode enum member"):
        reconcile([], [], mode=mode)


def test_invalid_mode_is_rejected_before_consuming_inputs() -> None:
    a = record(Source.A, 1, ("INV",), "1")
    b = record(Source.B, 1, ("INV",), "1")
    records_a, records_b = iter([a]), iter([b])

    with pytest.raises(TypeError, match="mode.*ReconciliationMode"):
        reconcile(records_a, records_b, mode="grouped_by_key")

    assert next(records_a) is a
    assert next(records_b) is b


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_empty_reconciliation_retains_requested_mode(mode: ReconciliationMode) -> None:
    result = reconcile(iter(()), iter(()), mode=mode)

    assert result.mode is mode
    assert result.findings == result.exceptions == result.tolerated_findings == ()
    assert result.total_a == result.total_b == Decimal("0")
    assert result.control_difference == result.finding_delta_sum == Decimal("0")


@pytest.mark.parametrize("tolerance", ["0", "1"])
@pytest.mark.parametrize(
    ("amounts_a", "amounts_b", "total"),
    [
        (["300"], ["300"], "300"),
        (["300"], ["100", "200"], "300"),
        (["100", "200"], ["300"], "300"),
        (["100", "200"], ["150", "150"], "300"),
        (["100", "-100"], ["50", "-50"], "0"),
        (["-10", "-20"], ["-30"], "-30"),
        (["0.0001", "0.0002"], ["0.0003"], "0.0003"),
    ],
    ids=["one-one", "one-many", "many-one", "many-many", "offsets", "credits", "sub-cent"],
)
def test_grouped_exact_compares_totals_without_pairing_rows(
    amounts_a: list[str], amounts_b: list[str], total: str, tolerance: str
) -> None:
    a = [record(Source.A, row, ("V", "INV"), amount) for row, amount in enumerate(amounts_a, 1)]
    b = [record(Source.B, row, ("V", "INV"), amount) for row, amount in enumerate(amounts_b, 1)]

    result = reconcile(
        a, b, amount_tolerance=Decimal(tolerance), mode=ReconciliationMode.GROUPED_BY_KEY
    )

    assert result.mode is ReconciliationMode.GROUPED_BY_KEY
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.category is FindingCategory.EXACT_MATCH
    assert (
        finding.amount_a == finding.amount_b == result.total_a == result.total_b == Decimal(total)
    )
    assert finding.delta == result.control_difference == result.finding_delta_sum == Decimal("0")
    assert finding.rows_a == tuple(a) and finding.rows_b == tuple(b)
    assert result.exceptions == result.tolerated_findings == ()


@pytest.mark.parametrize(
    ("last_b", "tolerance", "category", "delta"),
    [
        ("51", "0", FindingCategory.AMOUNT_MISMATCH, "-1"),
        ("50.005", "0.01", FindingCategory.WITHIN_TOLERANCE, "-0.005"),
        ("49.995", "0.01", FindingCategory.WITHIN_TOLERANCE, "0.005"),
        ("50.01", "0.01", FindingCategory.WITHIN_TOLERANCE, "-0.01"),
        ("49.99", "0.01", FindingCategory.WITHIN_TOLERANCE, "0.01"),
        ("50.0101", "0.01", FindingCategory.AMOUNT_MISMATCH, "-0.0101"),
        ("50.0001", "0", FindingCategory.AMOUNT_MISMATCH, "-0.0001"),
        ("50.0001", "0.0001", FindingCategory.WITHIN_TOLERANCE, "-0.0001"),
    ],
    ids=[
        "mismatch",
        "negative-inside",
        "positive-inside",
        "negative-boundary",
        "positive-boundary",
        "outside",
        "zero-tolerance",
        "sub-cent-boundary",
    ],
)
def test_grouped_tolerance_preserves_true_group_delta(
    last_b: str, tolerance: str, category: FindingCategory, delta: str
) -> None:
    a = [record(Source.A, 1, ("INV",), "40"), record(Source.A, 2, ("INV",), "60")]
    b = [record(Source.B, 1, ("INV",), "50"), record(Source.B, 2, ("INV",), last_b)]

    result = reconcile(
        a, b, amount_tolerance=Decimal(tolerance), mode=ReconciliationMode.GROUPED_BY_KEY
    )

    finding = result.findings[0]
    assert finding.category is category
    assert finding.amount_a == result.total_a == Decimal("100")
    assert finding.amount_b == result.total_b
    assert finding.delta == result.control_difference == result.finding_delta_sum == Decimal(delta)
    assert finding.rows_a == tuple(a) and finding.rows_b == tuple(b)
    assert result.amount_tolerance == Decimal(tolerance)
    accepted = category is FindingCategory.WITHIN_TOLERANCE
    assert result.tolerated_findings == ((finding,) if accepted else ())
    assert result.tolerated_delta_total == (Decimal(delta) if accepted else Decimal("0"))
    assert result.exceptions == (() if accepted else (finding,))


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("tolerance", ["0", "1000"])
@pytest.mark.parametrize(
    ("amounts", "total"),
    [(["10", "-3"], "7"), (["100", "-100"], "0"), (["0", "0"], "0")],
    ids=["nonzero-net", "zero-net", "zero-rows"],
)
def test_grouped_one_sided_presence_outranks_totals_and_tolerance(
    source: Source, tolerance: str, amounts: list[str], total: str
) -> None:
    rows = [record(source, row, ("INV",), amount) for row, amount in enumerate(amounts, 1)]
    a, b = (rows, []) if source is Source.A else ([], rows)

    result = reconcile(
        a, b, amount_tolerance=Decimal(tolerance), mode=ReconciliationMode.GROUPED_BY_KEY
    )

    finding = result.findings[0]
    assert finding.category is (
        FindingCategory.A_ONLY if source is Source.A else FindingCategory.B_ONLY
    )
    assert finding.rows_a == tuple(a) and finding.rows_b == tuple(b)
    assert finding.amount_a == result.total_a == (Decimal(total) if a else Decimal("0"))
    assert finding.amount_b == result.total_b == (Decimal(total) if b else Decimal("0"))
    expected_delta = Decimal(total) if a else Decimal(total).copy_negate()
    assert finding.delta == result.control_difference == result.finding_delta_sum == expected_delta
    assert result.exceptions == (finding,)
    assert result.tolerated_findings == ()


@pytest.mark.parametrize(
    ("count_a", "count_b", "category"),
    [
        (2, 1, FindingCategory.WITHIN_TOLERANCE),
        (1, 2, FindingCategory.WITHIN_TOLERANCE),
        (2, 2, FindingCategory.EXACT_MATCH),
        (2, 0, FindingCategory.A_ONLY),
        (0, 2, FindingCategory.B_ONLY),
    ],
)
def test_grouped_never_emits_ambiguity_while_unique_ignores_tolerance(
    count_a: int, count_b: int, category: FindingCategory
) -> None:
    a = [record(Source.A, row, ("INV",), "0.001") for row in range(1, count_a + 1)]
    b = [record(Source.B, row, ("INV",), "0.001") for row in range(1, count_b + 1)]
    grouped = reconcile(a, b, amount_tolerance=Decimal("1"), mode=ReconciliationMode.GROUPED_BY_KEY)
    unique = reconcile(a, b, amount_tolerance=Decimal("1"), mode=ReconciliationMode.UNIQUE)

    assert grouped.findings[0].category is category
    assert grouped.findings[0].category is not FindingCategory.DUPLICATE_AMBIGUOUS
    assert unique.findings[0].category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert unique.exceptions == unique.findings
    assert unique.tolerated_findings == ()
    assert grouped.findings[0].rows_a == unique.findings[0].rows_a == tuple(a)
    assert grouped.findings[0].rows_b == unique.findings[0].rows_b == tuple(b)
    assert grouped.control_difference == grouped.finding_delta_sum == unique.control_difference
    assert unique.control_difference == unique.finding_delta_sum


def test_grouped_duplicate_evidence_retains_identity_order_and_immutability() -> None:
    raw = {"invoice": "INV", "amount": "2.00"}
    a = [SourceRecord(Source.A, row, ("INV",), Decimal("2.00"), raw) for row in (9, 2)]
    b = [record(Source.B, 8, ("INV",), "3"), record(Source.B, 1, ("INV",), "1")]
    raw["amount"] = "999"
    result = reconcile(a, b, mode=ReconciliationMode.GROUPED_BY_KEY)

    finding = result.findings[0]
    assert finding.category is FindingCategory.EXACT_MATCH
    assert finding.rows_a[0] is a[1] and finding.rows_a[1] is a[0]
    assert finding.rows_b[0] is b[1] and finding.rows_b[1] is b[0]
    assert all(dict(row.raw_fields) == {"invoice": "INV", "amount": "2.00"} for row in a)
    with pytest.raises(TypeError):
        finding.rows_a[0].raw_fields["amount"] = "0"
    with pytest.raises(FrozenInstanceError):
        finding.rows_a[0].amount = Decimal("0")
    with pytest.raises(FrozenInstanceError):
        finding.rows_a = ()


def test_grouped_mixed_findings_preserve_control_totals_rows_order_and_one_pass_inputs() -> None:
    a = [
        record(Source.A, 9, ("V", "exact"), "100"),
        record(Source.A, 2, ("V", "exact"), "200"),
        record(Source.A, 8, ("V", "tolerated"), "40"),
        record(Source.A, 3, ("V", "tolerated"), "60"),
        record(Source.A, 7, ("V", "mismatch"), "10"),
        record(Source.A, 4, ("V", "mismatch"), "20"),
        record(Source.A, 6, ("V", "a-only"), "5"),
        record(Source.A, 5, ("V", "a-only"), "-2"),
        record(Source.A, 11, ("V", "a-zero"), "100"),
        record(Source.A, 12, ("V", "a-zero"), "-100"),
    ]
    b = [
        record(Source.B, 9, ("V", "exact"), "150"),
        record(Source.B, 2, ("V", "exact"), "150"),
        record(Source.B, 8, ("V", "tolerated"), "50"),
        record(Source.B, 3, ("V", "tolerated"), "50.005"),
        record(Source.B, 7, ("V", "mismatch"), "5"),
        record(Source.B, 4, ("V", "mismatch"), "20"),
        record(Source.B, 6, ("V", "b-only"), "1"),
        record(Source.B, 5, ("V", "b-only"), "2"),
        record(Source.B, 11, ("V", "b-zero"), "10"),
        record(Source.B, 12, ("V", "b-zero"), "-10"),
    ]
    result = reconcile(
        iter(a),
        (row for row in b),
        amount_tolerance=Decimal("0.01"),
        mode=ReconciliationMode.GROUPED_BY_KEY,
    )

    assert result == reconcile(
        reversed(a),
        reversed(b),
        amount_tolerance=Decimal("0.01"),
        mode=ReconciliationMode.GROUPED_BY_KEY,
    )
    assert [finding.key for finding in result.findings] == [
        ("V", name)
        for name in ("a-only", "a-zero", "b-only", "b-zero", "exact", "mismatch", "tolerated")
    ]
    assert [finding.category for finding in result.findings] == [
        FindingCategory.A_ONLY,
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
        FindingCategory.B_ONLY,
        FindingCategory.EXACT_MATCH,
        FindingCategory.AMOUNT_MISMATCH,
        FindingCategory.WITHIN_TOLERANCE,
    ]
    assert result.total_a == Decimal("433")
    assert result.total_b == Decimal("428.005")
    assert result.control_difference == result.finding_delta_sum == Decimal("4.995")
    assert result.tolerated_delta_total == Decimal("-0.005")
    assert len(result.exceptions) == 5
    accounted = [row for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)]
    assert Counter(map(id, accounted)) == Counter(map(id, [*a, *b]))
    assert len(accounted) == len(a) + len(b) == 20
    for finding in result.findings:
        assert [row.source_row for row in finding.rows_a] == sorted(
            row.source_row for row in finding.rows_a
        )
        assert [row.source_row for row in finding.rows_b] == sorted(
            row.source_row for row in finding.rows_b
        )


@pytest.mark.parametrize("cancel_large_amounts", [False, True])
def test_grouped_large_decimals_and_tolerance_ignore_callers_context(
    cancel_large_amounts: bool,
) -> None:
    a = [record(Source.A, 1, ("INV",), "1E+1000"), record(Source.A, 2, ("INV",), "0.0001")]
    b = [record(Source.B, 1, ("INV",), "1E+1000"), record(Source.B, 2, ("INV",), "0.0002")]
    if cancel_large_amounts:
        a.append(record(Source.A, 3, ("INV",), "-1E+1000"))
        b.append(record(Source.B, 3, ("INV",), "-1E+1000"))
    prefix = "0" if cancel_large_amounts else "1" + "0" * 1000
    expected_a, expected_b = Decimal(prefix + ".0001"), Decimal(prefix + ".0002")

    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        before = context.copy()

        result = reconcile(
            a, b, amount_tolerance=Decimal("0.0001"), mode=ReconciliationMode.GROUPED_BY_KEY
        )

        finding = result.findings[0]
        assert finding.category is FindingCategory.WITHIN_TOLERANCE
        assert finding.amount_a == result.total_a == expected_a
        assert finding.amount_b == result.total_b == expected_b
        assert (
            finding.delta
            == result.control_difference
            == result.finding_delta_sum
            == Decimal("-0.0001")
        )
        assert result.tolerated_delta_total == Decimal("-0.0001")
        assert context.prec == before.prec
        assert context.Emax == before.Emax
        assert context.Emin == before.Emin
        assert context.traps == before.traps
        assert context.flags == before.flags
