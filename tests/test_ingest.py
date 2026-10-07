from decimal import Decimal
from io import StringIO

import pytest

from tallydiff import FindingCategory, IngestionError, Source, ingest_csv, reconcile


@pytest.mark.parametrize("source", [Source.A, Source.B])
def test_ordinary_csv_produces_validated_records_in_source_order(source: Source) -> None:
    records = ingest_csv(
        "vendor,invoice,amount\nV002,1043,800.25\nV001,1042,1250.00\n",
        source=source,
        key_columns=["vendor", "invoice"],
        amount_column="amount",
    )

    assert isinstance(records, tuple)
    assert [row.source for row in records] == [source, source]
    assert [row.source_row for row in records] == [2, 3]
    assert [row.key for row in records] == [("V002", "1043"), ("V001", "1042")]
    assert [row.amount for row in records] == [Decimal("800.25"), Decimal("1250.00")]
    assert all(isinstance(row.amount, Decimal) for row in records)


def test_composite_key_order_is_selected_order_and_raw_fields_remain_untouched() -> None:
    records = ingest_csv(
        'vendor,invoice,amount,notes\n"  V-001  "," 00123 "," $1,200.0010 ",'
        '"  memo, says ""paid""  "\n',
        source=Source.A,
        key_columns=["invoice", "vendor"],
        amount_column="amount",
    )

    row = records[0]
    assert row.key == ("00123", "V-001")
    assert row.amount.as_tuple() == Decimal("1200.0010").as_tuple()
    assert dict(row.raw_fields) == {
        "vendor": "  V-001  ",
        "invoice": " 00123 ",
        "amount": " $1,200.0010 ",
        "notes": '  memo, says "paid"  ',
    }
    with pytest.raises(TypeError):
        row.raw_fields["invoice"] = "123"


@pytest.mark.parametrize(
    ("left", "right", "matches"),
    [
        ("00123", "123", False),
        ("ABC", "abc", False),
        ("AB-C", "ABC", False),
        ("A  B", "A B", False),
        ("  V001  ", "V001", True),
    ],
)
def test_only_surrounding_key_whitespace_is_normalized(
    left: str, right: str, matches: bool
) -> None:
    a = ingest_csv(
        f"id,amount\n{left},1\n", source=Source.A, key_columns=["id"], amount_column="amount"
    )
    b = ingest_csv(
        f"id,amount\n{right},1\n", source=Source.B, key_columns=["id"], amount_column="amount"
    )

    result = reconcile(a, b)

    assert a[0].raw_fields["id"] == left
    assert b[0].raw_fields["id"] == right
    if matches:
        assert len(result.findings) == 1
        assert result.findings[0].category is FindingCategory.EXACT_MATCH
    else:
        assert {finding.category for finding in result.findings} == {
            FindingCategory.A_ONLY,
            FindingCategory.B_ONLY,
        }
    assert result.control_difference == result.finding_delta_sum == Decimal("0")


def test_csv_ingests_negative_zero_and_fractional_amounts() -> None:
    records = ingest_csv(
        "id,amount\nA,-42.15\nB,0\nC,0.00\nD,0.123456\n"
        'E,"(1,200.00)"\nF,"($1,200.00)"\nG,"-$1,200.00"\n',
        source=Source.B,
        key_columns=["id"],
        amount_column="amount",
    )

    assert [row.amount.as_tuple() for row in records] == [
        Decimal(text).as_tuple()
        for text in ["-42.15", "0", "0.00", "0.123456", "-1200.00", "-1200.00", "-1200.00"]
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_multiline_fields_use_csv_record_ordinals_and_preserve_line_endings(newline: str) -> None:
    text = f'id,amount,notes{newline}A,1,"first{newline}second"{newline}B,2,third{newline}'
    records = ingest_csv(text, source=Source.A, key_columns=["id"], amount_column="amount")

    assert [row.source_row for row in records] == [2, 3]
    assert records[0].raw_fields["notes"] == f"first{newline}second"
    assert records[1].raw_fields["notes"] == "third"


def test_text_file_like_input_is_not_closed(tmp_path) -> None:
    path = tmp_path / "synthetic.csv"
    path.write_bytes(b'id,amount,notes\r\nA,1,"first\r\nsecond"\r\n')
    with path.open(encoding="utf-8", newline="") as stream:
        records = ingest_csv(stream, source=Source.B, key_columns=["id"], amount_column="amount")
        assert not stream.closed
    assert records[0].raw_fields["notes"] == "first\r\nsecond"


def test_header_only_csv_is_valid_empty_input() -> None:
    assert (
        ingest_csv("id,amount\n", source=Source.A, key_columns=["id"], amount_column="amount") == ()
    )


@pytest.mark.parametrize("text", ["", "\n"])
def test_missing_header_blocks_ingestion(text: str) -> None:
    with pytest.raises(IngestionError, match="missing header") as error:
        ingest_csv(text, source=Source.A, key_columns=["id"], amount_column="amount")
    assert error.value.source_row == 1


@pytest.mark.parametrize(
    ("header", "reason", "column"),
    [
        ("id,,amount", "blank column name", ""),
        ("id,  ,amount", "blank column name", "  "),
        ("id,id,amount", "duplicate column name", "id"),
        ("id,amount,notes,notes", "duplicate column name", "notes"),
        ("other,amount", "selected key column is missing", "id"),
        ("id,other", "selected amount column is missing", "amount"),
    ],
)
def test_invalid_headers_report_source_column_and_reason(
    header: str, reason: str, column: str
) -> None:
    with pytest.raises(IngestionError, match=reason) as error:
        ingest_csv(header + "\n", source=Source.B, key_columns=["id"], amount_column="amount")
    assert error.value.source is Source.B
    assert error.value.source_row == 1
    assert error.value.column == column
    assert "File B" in str(error.value)


def test_header_names_are_exact_and_are_not_silently_trimmed() -> None:
    text = " id ,amount\n00123,1\n"
    with pytest.raises(IngestionError, match="selected key column is missing"):
        ingest_csv(text, source=Source.A, key_columns=["id"], amount_column="amount")
    records = ingest_csv(text, source=Source.A, key_columns=[" id "], amount_column="amount")
    assert records[0].raw_fields[" id "] == "00123"


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_blank_composite_key_component_blocks_ingestion_with_original_evidence(value: str) -> None:
    with pytest.raises(IngestionError, match="blank after trimming") as error:
        ingest_csv(
            f"vendor,invoice,amount\nV001,{value},1\n",
            source=Source.A,
            key_columns=["vendor", "invoice"],
            amount_column="amount",
        )
    assert error.value.source_row == 2
    assert error.value.column == "invoice"
    assert error.value.value == value


@pytest.mark.parametrize("value", ["", "abc", "12.34.56", "$1,2,00", "NaN", "Infinity"])
def test_invalid_amount_in_a_later_record_blocks_the_entire_ingestion(value: str) -> None:
    stream = StringIO(f'id,amount\nGOOD,1\nBAD,"{value}"\n', newline="")
    with pytest.raises(IngestionError) as error:
        ingest_csv(stream, source=Source.B, key_columns=["id"], amount_column="amount")

    failure = error.value
    assert failure.source is Source.B
    assert failure.source_row == 3
    assert failure.column == "amount"
    assert failure.value == value
    assert failure.reason
    assert "File B, source record 3, column 'amount'" in str(failure)
    assert repr(value) in str(failure)
    assert failure.__suppress_context__
    assert not stream.closed


@pytest.mark.parametrize("row", ["A", "A,1,extra", "A,1,", "", "A,1,200.00"])
def test_missing_extra_or_blank_records_are_not_silently_skipped(row: str) -> None:
    with pytest.raises(IngestionError, match="expected 2 fields") as error:
        ingest_csv(
            f"id,amount\nGOOD,1\n{row}\n",
            source=Source.A,
            key_columns=["id"],
            amount_column="amount",
        )
    assert error.value.source_row == 3


@pytest.mark.parametrize(
    ("text", "source_row"),
    [
        ('"id,amount\n', 1),
        ('id,amount\nGOOD,1\n"BAD,2\n', 3),
        ('id,amount\n"BAD"junk,2\n', 2),
        ('id,amount,notes\nGOOD,1,"first\nsecond"\n"BAD,2', 3),
    ],
)
def test_malformed_csv_reports_record_ordinal(text: str, source_row: int) -> None:
    with pytest.raises(IngestionError, match="invalid CSV") as error:
        ingest_csv(text, source=Source.B, key_columns=["id"], amount_column="amount")
    assert error.value.source_row == source_row
    assert error.value.source is Source.B


@pytest.mark.parametrize(
    "keys", [[], ["id", "id"], [""], [" "], "id", None, {"id"}, {"id": "amount"}, [1], [None]]
)
def test_invalid_key_selections_block_ingestion(keys: object) -> None:
    with pytest.raises(IngestionError):
        ingest_csv("id,amount\n", source=Source.A, key_columns=keys, amount_column="amount")


@pytest.mark.parametrize("amount_column", ["", " "])
def test_blank_amount_column_selection_blocks_ingestion(amount_column: str) -> None:
    with pytest.raises(IngestionError, match="nonblank amount column"):
        ingest_csv("id,amount\n", source=Source.A, key_columns=["id"], amount_column=amount_column)


def test_invalid_source_is_rejected_even_when_csv_has_no_data() -> None:
    with pytest.raises(TypeError, match="Source enum"):
        ingest_csv("id,amount\n", source="A", key_columns=["id"], amount_column="amount")


def test_text_stream_read_failure_blocks_ingestion() -> None:
    class BrokenStream(StringIO):
        def __next__(self) -> str:
            if self.tell() > 0:
                raise OSError("synthetic read failure")
            return super().__next__()

    with pytest.raises(IngestionError, match="could not read CSV text") as error:
        ingest_csv(
            BrokenStream("id,amount\n"), source=Source.A, key_columns=["id"], amount_column="amount"
        )
    assert error.value.source_row == 2


def test_ingested_duplicates_are_preserved_as_ambiguous_even_at_zero_delta() -> None:
    a = ingest_csv(
        "id,amount\nINV,100\nINV,200\n", source=Source.A, key_columns=["id"], amount_column="amount"
    )
    b = ingest_csv(
        "id,amount\nINV,150\nINV,150\n", source=Source.B, key_columns=["id"], amount_column="amount"
    )

    result = reconcile(a, b)
    finding = result.findings[0]
    assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert finding.rows_a == a
    assert finding.rows_b == b
    assert result.exceptions == (finding,)
    assert result.control_difference == result.finding_delta_sum == Decimal("0")


def test_ingesting_original_example_explains_the_245_control_difference() -> None:
    a = ingest_csv(
        "vendor,invoice,amount\nV001,1042,1250\nV002,1043,800\nV003,1044,500\n",
        source=Source.A,
        key_columns=["vendor", "invoice"],
        amount_column="amount",
    )
    # A different source order must still reconcile by the configured exact key.
    b = ingest_csv(
        "vendor,invoice,amount\nV004,1045,300\nV002,1043,800\nV001,1042,1205\n",
        source=Source.B,
        key_columns=["vendor", "invoice"],
        amount_column="amount",
    )

    result = reconcile(a, b)
    assert [(finding.key, finding.category, finding.delta) for finding in result.findings] == [
        (("V001", "1042"), FindingCategory.AMOUNT_MISMATCH, Decimal("45")),
        (("V002", "1043"), FindingCategory.EXACT_MATCH, Decimal("0")),
        (("V003", "1044"), FindingCategory.A_ONLY, Decimal("500")),
        (("V004", "1045"), FindingCategory.B_ONLY, Decimal("-300")),
    ]
    assert result.total_a == Decimal("2550")
    assert result.total_b == Decimal("2305")
    assert result.control_difference == result.finding_delta_sum == Decimal("245")
    assert result.findings[0].rows_a[0].source_row == 2
    assert result.findings[0].rows_b[0].source_row == 4
    assert len(result.exceptions) == 3
