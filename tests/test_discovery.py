import ast
import csv
import inspect
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, time, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext
from io import BytesIO, StringIO
from zipfile import ZipFile

import pytest
from openpyxl.styles import Font

import tallydiff
import tallydiff.discovery as discovery
from tallydiff import (
    FindingCategory,
    IngestionError,
    Source,
    export_exceptions_csv,
    ingest_csv,
    ingest_xlsx,
    reconcile,
)
from tallydiff.discovery import ColumnProfile, TableProfile, profile_csv, profile_xlsx


def _csv(values):
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["candidate", "amount"])
    writer.writerows((value, "1") for value in values)
    return stream.getvalue()


def _rewrite_xml(data, path, transform):
    with BytesIO() as output, ZipFile(BytesIO(data)) as original:
        with ZipFile(output, "w") as rewritten:
            for name in original.namelist():
                content = original.read(name)
                if name == path:
                    changed = transform(content)
                    assert changed != content, "fixture XML mutation must change the input"
                    content = changed
                rewritten.writestr(name, content)
        return output.getvalue()


def test_profile_contracts_are_frozen_slotted_and_keep_columns_in_a_tuple():
    column = ColumnProfile("id", 1, 0, 1, 1, 1, 1, 0, 0, None)
    table = TableProfile(Source.A, None, 1, (column,))
    assert [field.name for field in fields(ColumnProfile)] == [
        "name",
        "row_count",
        "blank_count",
        "nonblank_count",
        "distinct_evidence_count",
        "key_eligible_count",
        "distinct_key_count",
        "duplicate_excess_count",
        "amount_parseable_count",
        "amount_total",
    ]
    assert [field.name for field in fields(TableProfile)] == [
        "source",
        "worksheet",
        "row_count",
        "columns",
    ]
    assert not hasattr(column, "__dict__")
    assert not hasattr(table, "__dict__")
    with pytest.raises(FrozenInstanceError):
        column.blank_count = 1
    with pytest.raises(FrozenInstanceError):
        table.columns = ()
    with pytest.raises(TypeError):
        table.columns[0] = column
    assert hash(table) == hash(TableProfile(Source.A, None, 1, (column,)))


@pytest.mark.parametrize("source", [Source.A, Source.B])
def test_csv_counts_exact_headers_evidence_keys_and_amount_totals(source):
    text = (
        " Notes , id ,Gross,Mixed,Blank\n"
        " memo , X ,0.1,1,\n"
        "memo,X,0.2,no, \n"
        " Memo ,X,0.3, ,\n"
        ",,0.4,2,\n"
    )
    assert profile_csv(text, source=source) == TableProfile(
        source,
        None,
        4,
        (
            ColumnProfile(" Notes ", 4, 1, 3, 3, 3, 2, 1, 0, None),
            ColumnProfile(" id ", 4, 1, 3, 2, 3, 1, 2, 0, None),
            ColumnProfile("Gross", 4, 0, 4, 4, 4, 4, 0, 4, Decimal("1.0")),
            ColumnProfile("Mixed", 4, 1, 3, 3, 3, 3, 0, 2, None),
            ColumnProfile("Blank", 4, 4, 0, 0, 0, 0, 0, 0, None),
        ),
    )


def test_csv_only_trims_keys_and_counts_excess_observations():
    values = ["00123", "123", "ABC", "abc", "AB-C", "ABC", "A  B", "A B", " 00123 "]
    column = profile_csv(_csv(values), source=Source.A).columns[0]
    records = ingest_csv(
        _csv(values), source=Source.A, key_columns=["candidate"], amount_column="amount"
    )
    assert column.distinct_evidence_count == len({row.raw_fields["candidate"] for row in records})
    assert column.distinct_evidence_count == 8
    assert column.key_eligible_count == 9
    assert column.distinct_key_count == len({row.key for row in records}) == 7
    assert column.duplicate_excess_count == 2
    repeated = profile_csv(_csv(["A", "A", "A", "B", "B", "B"]), source=Source.A)
    assert repeated.columns[0].duplicate_excess_count == 4


@pytest.mark.parametrize(
    "value",
    ["0", "-0.00", "+42.15", ".123456", " $1,200.0010 ", "(42.50)", "($1,200.00)"],
)
def test_csv_amount_candidates_match_ingestion_without_float_conversion(value):
    text = _csv([value])
    column = profile_csv(text, source=Source.B).columns[0]
    record = ingest_csv(text, source=Source.B, key_columns=["amount"], amount_column="candidate")[0]
    assert column.amount_parseable_count == 1
    assert column.amount_total == record.amount
    assert isinstance(column.amount_total, Decimal)


@pytest.mark.parametrize(
    "value", ["", " \t", "word", "NaN", "Infinity", "1e3", "$1,2,00", "12.34.56"]
)
def test_csv_invalid_amount_candidates_do_not_block_profile(value):
    column = profile_csv(_csv(["1", value, "2"]), source=Source.A).columns[0]
    assert column.amount_parseable_count == 2
    assert column.amount_total is None
    assert column.blank_count == (1 if not value.strip() else 0)
    with pytest.raises(IngestionError):
        ingest_csv(
            _csv(["1", value, "2"]),
            source=Source.A,
            key_columns=["amount"],
            amount_column="candidate",
        )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_csv_multiline_fields_count_logical_records_and_preserve_evidence(newline):
    text = f'notes,amount{newline}"first{newline}second",1{newline}"first second",2{newline}'
    result = profile_csv(text, source=Source.A)
    assert result.row_count == 2
    assert result.columns[0] == ColumnProfile("notes", 2, 0, 2, 2, 2, 2, 0, 0, None)
    assert result.columns[1].amount_total == Decimal("3")


def test_csv_caller_stream_is_consumed_from_current_position_and_never_closed():
    stream = StringIO("ignored\n id ,amount\n A ,1\n", newline="")
    stream.readline()
    result = profile_csv(stream, source=Source.B)
    assert result == profile_csv(" id ,amount\n A ,1\n", source=Source.B)
    assert stream.read() == ""
    assert not stream.closed


def test_csv_header_only_table_has_zero_counts_and_no_amount_totals():
    result = profile_csv("z, a ,amount\n", source=Source.A)
    assert result == TableProfile(
        Source.A,
        None,
        0,
        tuple(ColumnProfile(name, 0, 0, 0, 0, 0, 0, 0, 0, None) for name in ("z", " a ", "amount")),
    )


@pytest.mark.parametrize("row", ["A", "A,1,extra", "A,1,", "", "A,1,200.00"])
def test_csv_malformed_row_counts_remain_contextual_structural_errors(row):
    text = f"id,amount\nGOOD,1\n{row}\n"
    with pytest.raises(IngestionError, match="expected 2 fields") as error:
        profile_csv(text, source=Source.B)
    assert error.value.source is Source.B
    assert error.value.source_row == 3
    assert error.value.worksheet is None
    with pytest.raises(IngestionError) as ingest_error:
        ingest_csv(text, source=Source.B, key_columns=["id"], amount_column="amount")
    assert str(error.value) == str(ingest_error.value)


@pytest.mark.parametrize(
    ("text", "source_row"),
    [
        ('"id,amount\n', 1),
        ('id,amount\nGOOD,1\n"BAD,2\n', 3),
        ('id,amount\n"BAD"junk,2\n', 2),
        ('id,amount,notes\nGOOD,1,"first\nsecond"\n"BAD,2', 3),
    ],
)
def test_csv_strict_parser_reports_logical_record_ordinals(text, source_row):
    with pytest.raises(IngestionError, match="invalid CSV") as error:
        profile_csv(text, source=Source.A)
    assert error.value.source_row == source_row


@pytest.mark.parametrize(
    ("text", "reason", "column"),
    [
        ("", "missing header", None),
        ("\n", "missing header", None),
        ("id,,amount\n", "blank column name", ""),
        ("id, \t,amount\n", "blank column name", " \t"),
        ("id,id,amount\n", "duplicate column name", "id"),
    ],
)
def test_csv_uses_existing_header_validation(text, reason, column):
    with pytest.raises(IngestionError, match=reason) as error:
        profile_csv(text, source=Source.B)
    assert error.value.source_row == 1
    assert error.value.column == column
    with pytest.raises(IngestionError) as ingest_error:
        ingest_csv(text, source=Source.B, key_columns=["id"], amount_column="amount")
    assert str(error.value) == str(ingest_error.value)


@pytest.mark.parametrize("failure", [OSError, UnicodeError])
@pytest.mark.parametrize("text", ["", "id,amount\n"])
def test_csv_stream_failures_are_ingestion_errors_without_closing_stream(failure, text):
    class BrokenStream(StringIO):
        def __next__(self):
            if self.tell() >= len(text):
                raise failure("synthetic read failure")
            return super().__next__()

    stream = BrokenStream(text)
    with pytest.raises(IngestionError, match="could not read CSV text") as error:
        profile_csv(stream, source=Source.A)
    assert error.value.source_row == (2 if text else 1)
    assert not stream.closed


@pytest.mark.parametrize("source", [Source.A, Source.B])
def test_xlsx_basic_counts_follow_header_order_and_exact_evidence(xlsx_bytes, source):
    data = xlsx_bytes(
        {
            " Export ": [
                [" id ", "amount", "notes", "blank"],
                ["  A ", 0.1, "memo", None],
                ["A", 0.2, " memo ", " \t"],
                ["B", 0.3, None, None],
                ["B", 0.4, "MEMO", None],
            ]
        }
    )
    assert profile_xlsx(data, source=source, worksheet=" Export ") == TableProfile(
        source,
        " Export ",
        4,
        (
            ColumnProfile(" id ", 4, 0, 4, 3, 4, 2, 2, 0, None),
            ColumnProfile("amount", 4, 0, 4, 4, 4, 4, 0, 4, Decimal("1.0")),
            ColumnProfile("notes", 4, 1, 3, 3, 3, 2, 1, 0, None),
            ColumnProfile("blank", 4, 4, 0, 0, 0, 0, 0, 0, None),
        ),
    )


def test_xlsx_numeric_keys_and_evidence_are_identical_to_ingest(xlsx_bytes):
    values = [123, 123.0, "123", " 123 ", "00123", 0, -0.0, "0", "-0", 1e-7, "0.0000001", "1e-07"]
    data = xlsx_bytes(
        {"Data": [["candidate", "amount"], *[[value, 1] for value in values]]},
        configure=lambda book: setattr(book["Data"]["A2"], "number_format", "000000"),
    )
    records = ingest_xlsx(
        data, source=Source.A, worksheet="Data", key_columns=["candidate"], amount_column="amount"
    )
    column = profile_xlsx(data, source=Source.A, worksheet="Data").columns[0]
    assert [row.key[0] for row in records] == [
        "123",
        "123",
        "123",
        "123",
        "00123",
        "0",
        "0",
        "0",
        "-0",
        "0.0000001",
        "0.0000001",
        "1e-07",
    ]
    assert column.key_eligible_count == 12
    assert column.distinct_key_count == len({row.key for row in records}) == 6
    assert column.duplicate_excess_count == 6
    assert column.distinct_evidence_count == len({row.raw_fields["candidate"] for row in records})


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (date(2026, 1, 2), "2026-01-02"),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05"),
        (time(3, 4, 5), "03:04:05"),
    ],
)
def test_xlsx_dates_and_times_use_existing_iso_key_conversion(xlsx_bytes, value, text):
    data = xlsx_bytes({"Data": [["candidate", "amount"], [value, 1], [text, 2]]})
    column = profile_xlsx(data, source=Source.A, worksheet="Data").columns[0]
    records = ingest_xlsx(
        data, source=Source.A, worksheet="Data", key_columns=["candidate"], amount_column="amount"
    )
    assert [row.key for row in records] == [(text,), (text,)]
    assert column == ColumnProfile("candidate", 2, 0, 2, 1, 2, 1, 1, 0, None)


def test_xlsx_mixed_types_are_advisory_candidates_and_preserve_evidence(xlsx_bytes):
    values = [
        None,
        " \t",
        True,
        False,
        "=1+1",
        "#N/A",
        date(2026, 1, 2),
        datetime(2026, 1, 2, 3, 4, 5),
        time(3, 4, 5),
        timedelta(days=1),
        1,
        0.1,
        " $1,200.00 ",
        "no",
    ]
    data = xlsx_bytes({"Data": [["candidate", "amount"], *[[value, 1] for value in values]]})
    column = profile_xlsx(data, source=Source.B, worksheet="Data").columns[0]
    # The candidate remains unselected when checking preserved ingestion evidence.
    records = ingest_xlsx(
        data, source=Source.B, worksheet="Data", key_columns=["amount"], amount_column="amount"
    )
    assert column == ColumnProfile("candidate", 14, 2, 12, 12, 7, 7, 0, 3, None)
    assert column.distinct_evidence_count == len(
        {row.raw_fields["candidate"] for row in records if row.raw_fields["candidate"].strip()}
    )
    assert [row.raw_fields["candidate"] for row in records[:6]] == [
        "",
        " \t",
        "TRUE",
        "FALSE",
        "=1+1",
        "#N/A",
    ]


def test_xlsx_distinct_evidence_uses_cell_value_text_without_type_tags(xlsx_bytes):
    data = xlsx_bytes({"Data": [["candidate", "amount"], [1, 1], ["1", 1], [True, 1], ["TRUE", 1]]})
    column = profile_xlsx(data, source=Source.A, worksheet="Data").columns[0]
    assert column == ColumnProfile("candidate", 4, 0, 4, 2, 3, 2, 1, 2, None)


def test_xlsx_formula_and_error_types_are_excluded_even_when_text_is_parseable(xlsx_bytes):
    def configure(book):
        book["Data"]["A2"].data_type = "e"
        book["Data"]["A5"].data_type = "s"

    data = xlsx_bytes(
        {"Data": [["candidate", "amount"], ["100", 1], ["100", 1], ["=1+1", 1], ["=1+1", 1]]},
        configure=configure,
    )
    column = profile_xlsx(data, source=Source.A, worksheet="Data").columns[0]
    assert column == ColumnProfile("candidate", 4, 0, 4, 2, 2, 2, 0, 1, None)
    with pytest.raises(IngestionError, match="Excel error"):
        ingest_xlsx(
            data,
            source=Source.A,
            worksheet="Data",
            key_columns=["amount"],
            amount_column="candidate",
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" $1,200.00 ", "1200.00"),
        ("(42.50)", "-42.50"),
        (100, "100"),
        (100.01, "100.01"),
        (-42.5, "-42.5"),
        (0, "0"),
        (0.1, "0.1"),
        (1e-12, "0.000000000001"),
    ],
)
def test_xlsx_amount_candidates_share_ingestion_numeric_and_text_semantics(
    xlsx_bytes, value, expected
):
    data = xlsx_bytes(
        {"Data": [["id", "candidate"], ["A", value]]},
        configure=lambda book: setattr(book["Data"]["B2"], "number_format", "$#,##0.00"),
    )
    column = profile_xlsx(data, source=Source.B, worksheet="Data").columns[1]
    record = ingest_xlsx(
        data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="candidate"
    )[0]
    assert column.amount_parseable_count == 1
    assert column.amount_total == record.amount == Decimal(expected)


@pytest.mark.parametrize("value", [None, " \t", True, False, "=1+1", "#N/A", "NaN", "word"])
def test_xlsx_mixed_amount_validity_does_not_block_profile(xlsx_bytes, value):
    data = xlsx_bytes({"Data": [["id", "candidate"], ["A", 1], ["B", value], ["C", 2]]})
    column = profile_xlsx(data, source=Source.A, worksheet="Data").columns[1]
    assert column.amount_parseable_count == 2
    assert column.amount_total is None
    with pytest.raises(IngestionError):
        ingest_xlsx(
            data, source=Source.A, worksheet="Data", key_columns=["id"], amount_column="candidate"
        )


def test_xlsx_trailing_blank_rows_and_formatted_columns_are_ignored(xlsx_bytes):
    def configure(book):
        book["Data"]["B50"].number_format = "0.00"
        book["Data"]["Z1"].font = Font(bold=True)

    data = xlsx_bytes({"Data": [["id", "amount"], ["A", 1], [], [None, None]]}, configure=configure)
    assert profile_xlsx(data, source=Source.A, worksheet="Data") == TableProfile(
        Source.A,
        "Data",
        1,
        (
            ColumnProfile("id", 1, 0, 1, 1, 1, 1, 0, 0, None),
            ColumnProfile("amount", 1, 0, 1, 1, 1, 1, 0, 1, Decimal("1")),
        ),
    )


def test_xlsx_whitespace_only_rows_are_actual_records_and_missing_cells_are_blank(xlsx_bytes):
    data = xlsx_bytes({"Data": [["id", "amount"], [" \t", " "], ["A"]]})
    result = profile_xlsx(data, source=Source.B, worksheet="Data")
    assert result.row_count == 2
    assert result.columns[0] == ColumnProfile("id", 2, 1, 1, 1, 1, 1, 0, 0, None)
    assert result.columns[1] == ColumnProfile("amount", 2, 2, 0, 0, 0, 0, 0, 0, None)


@pytest.mark.parametrize(
    ("rows", "reason", "source_row"),
    [
        ([["A", 1], [], ["B", 2]], "blank interior", 3),
        ([["A", 1, "extra"]], "beyond the header", 2),
    ],
)
def test_xlsx_structural_row_failures_match_ingestion(xlsx_bytes, rows, reason, source_row):
    data = xlsx_bytes({"Data": [["id", "amount"], *rows]})
    with pytest.raises(IngestionError, match=reason) as error:
        profile_xlsx(data, source=Source.B, worksheet="Data")
    assert error.value.source is Source.B
    assert error.value.worksheet == "Data"
    assert error.value.source_row == source_row
    with pytest.raises(IngestionError) as ingest_error:
        ingest_xlsx(
            data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
        )
    assert str(error.value) == str(ingest_error.value)


def test_xlsx_actual_xml_rows_are_read_despite_incorrect_declared_dimensions(xlsx_bytes):
    data = xlsx_bytes({"Data": [["id", "amount"], ["A", 1], ["B", 2]]})
    data = _rewrite_xml(
        data,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(b'ref="A1:B3"', b'ref="A1:A1"'),
    )
    result = profile_xlsx(data, source=Source.A, worksheet="Data")
    assert result.row_count == 2
    assert result.columns[1].amount_total == Decimal("3")


def test_xlsx_header_only_table_keeps_exact_order_and_no_totals(xlsx_bytes):
    data = xlsx_bytes({"Data": [["z", " a ", "amount"]]})
    assert profile_xlsx(data, source=Source.B, worksheet="Data") == TableProfile(
        Source.B,
        "Data",
        0,
        tuple(ColumnProfile(name, 0, 0, 0, 0, 0, 0, 0, 0, None) for name in ("z", " a ", "amount")),
    )


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (["id", "amount", "id"], "duplicate"),
        (["id", None, "amount"], "nonblank text"),
        (["id", " \t", "amount"], "nonblank text"),
        ([123, "amount"], "nonblank text"),
        ([date(2026, 1, 2), "amount"], "nonblank text"),
        (["=1+1", "amount"], "nonblank text"),
        (["#N/A", "amount"], "nonblank text"),
        ([], "missing header"),
    ],
)
def test_xlsx_header_failures_match_ingestion(xlsx_bytes, header, reason):
    data = xlsx_bytes({"Data": [header]})
    with pytest.raises(IngestionError, match=reason) as error:
        profile_xlsx(data, source=Source.A, worksheet="Data")
    assert error.value.source_row == 1
    assert error.value.worksheet == "Data"
    with pytest.raises(IngestionError) as ingest_error:
        ingest_xlsx(
            data, source=Source.A, worksheet="Data", key_columns=["id"], amount_column="amount"
        )
    assert str(error.value) == str(ingest_error.value)


@pytest.mark.parametrize("worksheet", ["", "data", " Data"])
def test_xlsx_worksheet_selection_is_explicit_and_exact(xlsx_bytes, worksheet):
    data = xlsx_bytes({"Other": [["id", "amount"]], "Data": [["id", "amount"], ["A", 1]]})
    with pytest.raises(IngestionError, match="does not exist") as error:
        profile_xlsx(data, source=Source.B, worksheet=worksheet)
    assert error.value.worksheet == worksheet
    assert profile_xlsx(data, source=Source.B, worksheet="Data").row_count == 1
    with pytest.raises(TypeError, match="worksheet"):
        profile_xlsx(data, source=Source.B)


@pytest.mark.parametrize("data", [b"not a workbook", b"PK\x03\x04broken"])
def test_xlsx_corrupt_workbook_is_a_contextual_structural_error(data):
    with pytest.raises(IngestionError, match="could not read XLSX workbook") as error:
        profile_xlsx(data, source=Source.A, worksheet="Data")
    assert error.value.source is Source.A
    assert error.value.worksheet == "Data"


@pytest.mark.parametrize(
    "content_type",
    [
        b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
        b"application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    ],
)
def test_xlsx_only_standard_workbook_packages_are_supported(xlsx_bytes, content_type):
    data = xlsx_bytes({"Data": [["id", "amount"], ["A", 1]]})
    data = _rewrite_xml(
        data,
        "[Content_Types].xml",
        lambda xml: xml.replace(
            b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
            content_type,
        ),
    )
    with pytest.raises(IngestionError, match="only standard .xlsx"):
        profile_xlsx(data, source=Source.A, worksheet="Data")


def test_xlsx_nonfinite_numeric_evidence_remains_a_structural_error(xlsx_bytes):
    data = xlsx_bytes({"Data": [["id", "amount", "notes"], ["A", 1, 100.01]]})
    data = _rewrite_xml(
        data,
        "xl/worksheets/sheet1.xml",
        lambda xml: xml.replace(b"<v>100.01</v>", b"<v>1e309</v>"),
    )
    with pytest.raises(IngestionError, match="finite") as error:
        profile_xlsx(data, source=Source.B, worksheet="Data")
    assert error.value.column == "notes"
    assert error.value.source_row == 2
    with pytest.raises(IngestionError) as ingest_error:
        ingest_xlsx(
            data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
        )
    assert str(error.value) == str(ingest_error.value)


@pytest.mark.parametrize("format_name", ["csv", "xlsx"])
def test_amount_totals_are_exact_and_ignore_caller_decimal_context(xlsx_bytes, format_name):
    values = ["12345678901234567890.123456789", "0.000000001", "-12345678901234567890"]
    if format_name == "csv":
        data = _csv(values)
    else:
        data = xlsx_bytes({"Data": [["candidate", "amount"], *[[value, 1] for value in values]]})
    with localcontext() as context:
        context.prec = 2
        context.Emax = 3
        context.Emin = -3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        before = context.copy()
        result = (
            profile_csv(data, source=Source.A)
            if format_name == "csv"
            else profile_xlsx(data, source=Source.A, worksheet="Data")
        )
        assert context.prec == before.prec
        assert context.Emax == before.Emax
        assert context.Emin == before.Emin
        assert context.traps == before.traps
        assert context.flags == before.flags
    assert result.columns[0].amount_parseable_count == 3
    assert result.columns[0].amount_total == Decimal("0.123456790")


@pytest.mark.parametrize("format_name", ["csv", "xlsx"])
def test_profile_determinism_includes_equivalent_row_orders(xlsx_bytes, format_name):
    rows = [[" B ", "1.00"], ["A", "2"], ["B", "3.25"]]
    if format_name == "csv":
        data = "id,amount\n" + "".join(f"{key},{amount}\n" for key, amount in rows)
        reordered = "id,amount\n" + "".join(f"{key},{amount}\n" for key, amount in reversed(rows))
        first = profile_csv(data, source=Source.A)
        assert first == profile_csv(StringIO(data, newline=""), source=Source.A)
        assert first == profile_csv(reordered, source=Source.A)
    else:
        data = xlsx_bytes({"Data": [["id", "amount"], *rows]})
        reordered = xlsx_bytes({"Data": [["id", "amount"], *reversed(rows)]})
        first = profile_xlsx(data, source=Source.A, worksheet="Data")
        assert first == profile_xlsx(data, source=Source.A, worksheet="Data")
        assert first == profile_xlsx(reordered, source=Source.A, worksheet="Data")


def test_profiling_does_not_mutate_workbook_or_change_ingestion_reconciliation_or_exports(
    xlsx_bytes,
):
    csv_data = "id,amount,notes\nA,100,memo\nDUP,1,\nDUP,2,other\n"
    xlsx_data = xlsx_bytes({"Data": [["id", "amount", "notes"], ["A", 100.01, "=1+1"], ["DUP", 3]]})
    original_bytes = bytes(bytearray(xlsx_data))
    before_a = ingest_csv(csv_data, source=Source.A, key_columns=["id"], amount_column="amount")
    before_b = ingest_xlsx(
        xlsx_data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
    )
    before = reconcile(before_a, before_b, amount_tolerance=Decimal("0.01"))
    exported = export_exceptions_csv(before)
    profile_csv(csv_data, source=Source.A)
    profile_xlsx(xlsx_data, source=Source.B, worksheet="Data")
    after_a = ingest_csv(csv_data, source=Source.A, key_columns=["id"], amount_column="amount")
    after_b = ingest_xlsx(
        xlsx_data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
    )
    after = reconcile(after_a, after_b, amount_tolerance=Decimal("0.01"))
    assert xlsx_data == original_bytes
    assert before_a == after_a
    assert before_b == after_b
    assert before == after
    assert exported == export_exceptions_csv(after)
    assert [finding.category for finding in after.findings] == [
        FindingCategory.WITHIN_TOLERANCE,
        FindingCategory.DUPLICATE_AMBIGUOUS,
    ]


def test_discovery_remains_outside_public_exports_and_reconciliation_dependencies():
    assert {"ColumnProfile", "TableProfile", "profile_csv", "profile_xlsx"}.isdisjoint(
        tallydiff.__all__
    )
    imports = []
    for node in ast.walk(ast.parse(inspect.getsource(discovery))):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    for forbidden in ("streamlit", "tallydiff.app", "tallydiff.engine", "tallydiff.review"):
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in imports)


def test_invalid_sources_and_xlsx_data_types_are_rejected(xlsx_bytes):
    with pytest.raises(TypeError, match="Source enum"):
        profile_csv("id,amount\n", source="A")
    data = xlsx_bytes({"Data": [["id", "amount"]]})
    with pytest.raises(TypeError, match="Source enum"):
        profile_xlsx(data, source="A", worksheet="Data")
    with pytest.raises(TypeError, match="XLSX bytes"):
        profile_xlsx(bytearray(data), source=Source.A, worksheet="Data")
