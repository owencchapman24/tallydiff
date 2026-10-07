from decimal import Decimal

from tallydiff import FindingCategory, Source, SourceRecord, reconcile


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

    try:
        reconcile(records_a, [])
    except ValueError as exc:
        assert "duplicate source row 2" in str(exc)
    else:
        raise AssertionError("expected duplicate source rows to be rejected")
