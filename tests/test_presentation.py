from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

from tallydiff import IngestionError, Source, SourceRecord, ingest_csv, reconcile
from tallydiff.presentation import (
    ColumnMapping,
    configuration_id,
    decode_upload,
    display_amount,
    evidence_rows,
    finding_rows,
)


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf"])
def test_utf8_decoding_preserves_source_text_with_optional_bom(prefix: bytes) -> None:
    text = "id,amount,notes\r\n001,1,caf\u00e9\r\n"
    assert decode_upload(prefix + text.encode("utf-8"), source=Source.A) == text


def test_invalid_encoding_is_a_blocking_contextual_error() -> None:
    with pytest.raises(IngestionError, match="UTF-8") as error:
        decode_upload(b"id,amount\n\xff,1", source=Source.B)
    assert error.value.source is Source.B
    assert error.value.__suppress_context__


def test_ordered_pairs_transform_to_each_files_key_order() -> None:
    mapping = ColumnMapping((("invoice", "reference"), ("vendor", "supplier")), "amount", "gross")
    assert (
        mapping.problem(["vendor", "invoice", "amount"], ["supplier", "reference", "gross"]) is None
    )
    assert mapping.keys_a == ("invoice", "vendor")
    assert mapping.keys_b == ("reference", "supplier")


@pytest.mark.parametrize(
    "mapping",
    [
        ColumnMapping((), "amount", "gross"),
        ColumnMapping(((None, "supplier"),), "amount", "gross"),
        ColumnMapping((("vendor", None),), "amount", "gross"),
        ColumnMapping((("unknown", "supplier"),), "amount", "gross"),
        ColumnMapping((("vendor", "supplier"), ("vendor", "reference")), "amount", "gross"),
        ColumnMapping((("vendor", "supplier"), ("invoice", "supplier")), "amount", "gross"),
        ColumnMapping((("vendor", "supplier"),), None, "gross"),
        ColumnMapping((("vendor", "supplier"),), "amount", None),
        ColumnMapping((("vendor", "supplier"),), "unknown", "gross"),
    ],
)
def test_incomplete_duplicate_or_stale_mappings_cannot_run(mapping: ColumnMapping) -> None:
    assert mapping.problem(["vendor", "invoice", "amount"], ["supplier", "reference", "gross"])


@pytest.mark.parametrize(
    ("value", "signed", "expected"),
    [
        ("1250", False, "1250"),
        ("45", True, "+45"),
        ("-300", True, "-300"),
        ("0.00", True, "0.00"),
        ("-0.00", True, "-0.00"),
        ("0.12345678901234567890", True, "+0.12345678901234567890"),
        ("1E+30", False, "1000000000000000000000000000000"),
    ],
)
def test_decimal_display_is_exact_even_under_low_precision(
    value: str, signed: bool, expected: str
) -> None:
    with localcontext() as context:
        context.prec = 2
        assert display_amount(Decimal(value), signed=signed) == expected


def test_finding_table_uses_exact_strings_and_includes_all_duplicate_source_numbers() -> None:
    a = ingest_csv(
        "id,amount\nINV,0.1234\nINV,0.0001\n",
        source=Source.A,
        key_columns=["id"],
        amount_column="amount",
    )
    b = ingest_csv(
        "id,amount\nINV,0.1200\n", source=Source.B, key_columns=["id"], amount_column="amount"
    )
    result = reconcile(a, b)
    assert finding_rows(result.exceptions) == [
        {
            "Category": "Duplicate / ambiguous",
            "Matching key": "INV",
            "File A amount": "0.1235",
            "File B amount": "0.1200",
            "Delta (A - B)": "+0.0035",
            "File A records": "2, 3",
            "File B records": "2",
        }
    ]


def test_evidence_preserves_fields_including_names_that_overlap_presentation_labels() -> None:
    raw = {"Source record": "003", "id": " 00123 ", "amount": " $1,200.00 ", "memo": "a\nb"}
    record = SourceRecord(Source.A, 2, ("00123",), Decimal("1200.00"), raw)
    assert evidence_rows(record) == [
        {"Field": field, "Original value": value} for field, value in raw.items()
    ]


def test_configuration_identity_changes_for_every_result_relevant_input() -> None:
    mapping = ColumnMapping((("id", "ref"), ("vendor", "supplier")), "amount", "gross")
    original = configuration_id(b"a", b"b", mapping, name_a="a.csv", name_b="b.csv")
    assert original == configuration_id(b"a", b"b", mapping, name_a="a.csv", name_b="b.csv")
    variants = [
        (b"changed", b"b", mapping, "a.csv", "b.csv"),
        (b"a", b"changed", mapping, "a.csv", "b.csv"),
        (b"a", b"b", mapping, "renamed.csv", "b.csv"),
        (b"a", b"b", mapping, "a.csv", "renamed.csv"),
        (
            b"a",
            b"b",
            replace(mapping, key_pairs=tuple(reversed(mapping.key_pairs))),
            "a.csv",
            "b.csv",
        ),
        (b"a", b"b", replace(mapping, key_pairs=(("other", "ref"),)), "a.csv", "b.csv"),
        (b"a", b"b", replace(mapping, amount_a="other"), "a.csv", "b.csv"),
        (b"a", b"b", replace(mapping, amount_b="other"), "a.csv", "b.csv"),
    ]
    for data_a, data_b, changed_mapping, name_a, name_b in variants:
        assert (
            configuration_id(data_a, data_b, changed_mapping, name_a=name_a, name_b=name_b)
            != original
        )


@pytest.mark.parametrize(
    ("amounts_a", "amounts_b", "shown_a", "shown_b", "delta"),
    [
        (("500",), (), "500", "\u2014", "+500"),
        ((), ("300",), "\u2014", "300", "-300"),
        (("0.00",), (), "0.00", "\u2014", "0.00"),
        ((), ("0.00",), "\u2014", "0.00", "0.00"),
        (("0.123400",), (), "0.123400", "\u2014", "+0.123400"),
        ((), ("0.123400",), "\u2014", "0.123400", "-0.123400"),
        (("0.00",), ("0.0001",), "0.00", "0.0001", "-0.0001"),
        (("1.00", "-1.00"), ("0.00",), "0.00", "0.00", "0.00"),
    ],
)
def test_finding_table_distinguishes_missing_records_from_present_zero_amounts(
    amounts_a: tuple[str, ...],
    amounts_b: tuple[str, ...],
    shown_a: str,
    shown_b: str,
    delta: str,
) -> None:
    records_a = [
        SourceRecord(Source.A, row, ("INV",), Decimal(amount))
        for row, amount in enumerate(amounts_a, start=2)
    ]
    records_b = [
        SourceRecord(Source.B, row, ("INV",), Decimal(amount))
        for row, amount in enumerate(amounts_b, start=2)
    ]
    result = reconcile(records_a, records_b)
    finding = result.exceptions[0]

    displayed = finding_rows(result.exceptions)[0]

    assert displayed["File A amount"] == shown_a
    assert displayed["File B amount"] == shown_b
    assert displayed["Delta (A - B)"] == delta
    assert finding.delta == result.control_difference == result.finding_delta_sum == Decimal(delta)
    if not amounts_a:
        assert finding.amount_a == Decimal("0")
    if not amounts_b:
        assert finding.amount_b == Decimal("0")


def test_configuration_identity_includes_exact_decimal_tolerance() -> None:
    mapping = ColumnMapping((("id", "ref"),), "amount", "gross")

    def identity(tolerance: Decimal) -> str:
        return configuration_id(
            b"a", b"b", mapping, name_a="a.csv", name_b="b.csv", amount_tolerance=tolerance
        )

    assert identity(Decimal("0")) == configuration_id(
        b"a", b"b", mapping, name_a="a.csv", name_b="b.csv"
    )
    assert (
        len({identity(Decimal(value)) for value in ("0", "0.01", "0.010000000000000000001")}) == 3
    )
