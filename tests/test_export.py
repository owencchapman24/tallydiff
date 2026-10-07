import csv
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from tallydiff import Source, SourceRecord, export_exceptions_csv, ingest_csv, reconcile

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = [
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
]


def _read_report(data: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(StringIO(data.decode("utf-8"), newline=""))
    assert reader.fieldnames == COLUMNS
    return list(reader)


def test_sample_report_has_exactly_the_three_exceptions_in_stable_column_order() -> None:
    a = ingest_csv(
        (ROOT / "sample_data/file_a.csv").read_text(encoding="utf-8"),
        source=Source.A,
        key_columns=["Vendor ID", "Invoice Number"],
        amount_column="Invoice Amount",
    )
    b = ingest_csv(
        (ROOT / "sample_data/file_b.csv").read_text(encoding="utf-8"),
        source=Source.B,
        key_columns=["Supplier", "Invoice Ref"],
        amount_column="Gross Amount",
    )
    result = reconcile(a, b)

    data = export_exceptions_csv(result)

    assert data == export_exceptions_csv(result)
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.endswith(b"\r\n")
    rows = _read_report(data)
    assert [list(row.values()) for row in rows] == [
        ["Amount mismatch", "V001 / 1042", "1250", "1205", "+45", "2", "2"],
        ["File A only", "V003 / 1044", "500", "", "+500", "4", ""],
        ["File B only", "V004 / 1045", "", "300", "-300", "", "4"],
    ]
    assert len(rows) == len(result.exceptions) == 3
    assert all(row["Matching key"] != "V002 / 1043" for row in rows)


@pytest.mark.parametrize(
    ("source", "amount", "expected_a", "expected_b", "delta"),
    [
        (Source.A, "0.00", "0.00", "", "0.00"),
        (Source.B, "0.00", "", "0.00", "0.00"),
        (Source.A, "-2.123400", "-2.123400", "", "-2.123400"),
        (Source.B, "-2.123400", "", "-2.123400", "+2.123400"),
    ],
)
def test_missing_amounts_are_blank_and_real_zero_and_negative_amounts_are_exact(
    source: Source,
    amount: str,
    expected_a: str,
    expected_b: str,
    delta: str,
) -> None:
    records = [SourceRecord(source, 2, ("00123",), Decimal(amount))]
    result = reconcile(records if source is Source.A else [], records if source is Source.B else [])

    row = _read_report(export_exceptions_csv(result))[0]

    assert row["Matching key"] == "00123"
    assert row["File A amount"] == expected_a
    assert row["File B amount"] == expected_b
    assert row["Delta (A - B)"] == delta
    assert Decimal(row["Delta (A - B)"]) == result.control_difference


@pytest.mark.parametrize(("count_a", "count_b"), [(2, 2), (2, 0), (0, 2)])
def test_zero_delta_duplicates_export_once_with_every_source_record(
    count_a: int, count_b: int
) -> None:
    a = [
        SourceRecord(Source.A, 7, ("INV",), Decimal("100.00")),
        SourceRecord(Source.A, 2, ("INV",), Decimal("-100.00")),
    ][:count_a]
    b = [
        SourceRecord(Source.B, 9, ("INV",), Decimal("200.00")),
        SourceRecord(Source.B, 3, ("INV",), Decimal("-200.00")),
    ][:count_b]
    result = reconcile(a, b)

    rows = _read_report(export_exceptions_csv(result))

    assert len(rows) == 1
    assert rows[0] == {
        "Category": "Duplicate / ambiguous",
        "Matching key": "INV",
        "File A amount": "0.00" if count_a else "",
        "File B amount": "0.00" if count_b else "",
        "Delta (A - B)": "0.00",
        "File A records": "2; 7" if count_a else "",
        "File B records": "3; 9" if count_b else "",
    }


def test_high_precision_mismatch_and_csv_special_characters_round_trip_exactly() -> None:
    key = ('Vendor, "A"', "r\u00e9f\r\n00123")
    a = [SourceRecord(Source.A, 2, key, Decimal("0.123456789012345678900"))]
    b = [SourceRecord(Source.B, 2, key, Decimal("0.100000000000000000000"))]
    result = reconcile(a, b)

    data = export_exceptions_csv(result)
    rows = _read_report(data)

    assert len(rows) == 1
    assert rows[0] == {
        "Category": "Amount mismatch",
        "Matching key": 'Vendor, "A" / r\u00e9f\r\n00123',
        "File A amount": "0.123456789012345678900",
        "File B amount": "0.100000000000000000000",
        "Delta (A - B)": "+0.023456789012345678900",
        "File A records": "2",
        "File B records": "2",
    }
    assert b'"Vendor, ""A"" / ' in data
    assert "r\u00e9f".encode("utf-8") in data


@pytest.mark.parametrize("has_records", [False, True])
def test_no_exceptions_produce_a_header_only_report(has_records: bool) -> None:
    a = [SourceRecord(Source.A, 2, ("MATCH",), Decimal("1.00"))] if has_records else []
    b = [SourceRecord(Source.B, 2, ("MATCH",), Decimal("1.00"))] if has_records else []

    data = export_exceptions_csv(reconcile(a, b))

    assert _read_report(data) == []
    assert data.decode("utf-8") == ",".join(COLUMNS) + "\r\n"
