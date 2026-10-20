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


def _snapshot_csv(header, rows, *, source):
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return discovery.discover_csv(stream.getvalue(), source=source)


@pytest.fixture
def discovery_pair():
    return (
        _snapshot_csv(["id", "other", "amount"], [["A", "x", "1"]], source=Source.A),
        _snapshot_csv(["id", "other", "amount"], [["A", "x", "2"]], source=Source.B),
    )


def test_csv_snapshot_profile_parity_and_canonical_row_alignment():
    text = " id ,invoice,amount,notes\n A ,100,1, note \n,101,2,\nB, ,word,memo\n"
    snapshot = discovery.discover_csv(text, source=Source.A)
    assert snapshot.profile == profile_csv(text, source=Source.A)
    assert snapshot._key_rows == (
        ("A", "100", "1", "note"),
        (None, "101", "2", None),
        ("B", None, "word", "memo"),
    )
    assert [column.name for column in snapshot.profile.columns] == [
        " id ",
        "invoice",
        "amount",
        "notes",
    ]
    assert snapshot == discovery.discover_csv(text, source=Source.A)


def test_xlsx_snapshot_profile_parity_and_canonical_row_alignment(xlsx_bytes):
    data = xlsx_bytes(
        {
            "Data": [
                ["candidate", "second", "amount"],
                [" A ", " x ", 1],
                [True, 100, 2],
                [date(2026, 1, 2), None, 3],
                ["=1+1", "100", 4],
                ["#N/A", "B", 5],
                [timedelta(days=1), "C", 6],
            ]
        }
    )
    snapshot = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
    assert snapshot.profile == profile_xlsx(data, source=Source.B, worksheet="Data")
    assert snapshot._key_rows == (
        ("A", "x", "1"),
        (None, "100", "2"),
        ("2026-01-02", None, "3"),
        (None, "100", "4"),
        (None, "B", "5"),
        (None, "C", "6"),
    )
    assert snapshot == discovery.discover_xlsx(data, source=Source.B, worksheet="Data")


@pytest.mark.parametrize("format_name", ["csv", "xlsx"])
def test_profile_functions_delegate_to_discovery_and_return_the_exact_profile(
    discovery_pair, monkeypatch, format_name
):
    snapshot = discovery_pair[0]
    calls = []
    if format_name == "csv":
        data = StringIO("not parsed")

        def fake_discover(value, *, source):
            calls.append((value, source))
            return snapshot

        monkeypatch.setattr(discovery, "discover_csv", fake_discover)
        assert discovery.profile_csv(data, source=Source.A) is snapshot.profile
        assert calls == [(data, Source.A)]
    else:
        data = b"not parsed"

        def fake_discover(value, *, source, worksheet):
            calls.append((value, source, worksheet))
            return snapshot

        monkeypatch.setattr(discovery, "discover_xlsx", fake_discover)
        assert discovery.profile_xlsx(data, source=Source.A, worksheet="Data") is snapshot.profile
        assert calls == [(data, Source.A, "Data")]


def test_snapshots_and_diagnostics_have_immutable_slotted_storage(discovery_pair):
    snapshot_a, snapshot_b = discovery_pair
    keys = discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=[("id", "id")])
    amounts = discovery.compare_amount_columns(
        snapshot_a, snapshot_b, column_a="amount", column_b="amount"
    )
    assert [field.name for field in fields(discovery.DiscoverySnapshot)] == ["profile", "_key_rows"]
    assert [field.name for field in fields(discovery.KeyCompatibility)] == [
        "key_pairs",
        "row_count_a",
        "row_count_b",
        "eligible_row_count_a",
        "eligible_row_count_b",
        "distinct_key_count_a",
        "distinct_key_count_b",
        "duplicate_excess_count_a",
        "duplicate_excess_count_b",
        "overlap_distinct_count",
        "overlap_row_count_a",
        "overlap_row_count_b",
        "only_a_distinct_count",
        "only_b_distinct_count",
    ]
    assert [field.name for field in fields(discovery.AmountCompatibility)] == [
        "column_a",
        "column_b",
        "row_count_a",
        "row_count_b",
        "parseable_count_a",
        "parseable_count_b",
        "blank_count_a",
        "blank_count_b",
        "total_a",
        "total_b",
        "control_difference",
    ]
    for value, name in ((snapshot_a, "profile"), (keys, "key_pairs"), (amounts, "total_a")):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, None)
        assert isinstance(hash(value), int)
    assert isinstance(snapshot_a._key_rows, tuple)
    assert all(isinstance(row, tuple) for row in snapshot_a._key_rows)
    with pytest.raises(TypeError):
        snapshot_a._key_rows[0][0] = "changed"


@pytest.mark.parametrize(
    ("rows", "error_type"),
    [
        ([], TypeError),
        ([tuple(["A", "x", "1"])], TypeError),
        ((["A", "x", "1"],), TypeError),
        (((1, "x", "1"),), TypeError),
        ((([], "x", "1"),), TypeError),
        ((), ValueError),
        ((("A", "x"),), ValueError),
        ((("A", "x", "1", "extra"),), ValueError),
        ((("A", "x", "1"), ("B", "y", "2")), ValueError),
        ((("", "x", "1"),), ValueError),
        (((" ", "x", "1"),), ValueError),
        (((" A ", "x", "1"),), ValueError),
    ],
)
def test_snapshot_constructor_rejects_mutable_misaligned_or_noncanonical_rows(
    discovery_pair, rows, error_type
):
    with pytest.raises(error_type):
        discovery.DiscoverySnapshot(discovery_pair[0].profile, rows)


@pytest.mark.parametrize("profile", [None, {}, "profile", object()])
def test_snapshot_constructor_requires_a_table_profile(profile):
    with pytest.raises(TypeError):
        discovery.DiscoverySnapshot(profile, ())


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    [
        ("source", "A", TypeError),
        ("row_count", "1", TypeError),
        ("row_count", True, TypeError),
        ("row_count", -1, ValueError),
        ("worksheet", [], TypeError),
        ("columns", [], TypeError),
        ("columns", ({},), TypeError),
    ],
)
def test_snapshot_constructor_rejects_invalid_or_mutable_table_profile_fields(
    discovery_pair, field_name, value, error_type
):
    from dataclasses import replace

    profile = replace(discovery_pair[0].profile, **{field_name: value})
    with pytest.raises(error_type):
        discovery.DiscoverySnapshot(profile, discovery_pair[0]._key_rows)


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    [
        ("name", [], TypeError),
        ("name", "", ValueError),
        ("name", " \t", ValueError),
        ("row_count", 0, ValueError),
        ("blank_count", [], TypeError),
        ("amount_total", [], TypeError),
        ("amount_total", 1, TypeError),
        ("amount_total", Decimal("NaN"), ValueError),
        ("amount_total", Decimal("Infinity"), ValueError),
    ],
)
def test_snapshot_constructor_rejects_invalid_or_mutable_column_profile_fields(
    discovery_pair, field_name, value, error_type
):
    from dataclasses import replace

    original = discovery_pair[0]
    column = replace(original.profile.columns[0], **{field_name: value})
    profile = replace(original.profile, columns=(column, *original.profile.columns[1:]))
    with pytest.raises(error_type):
        discovery.DiscoverySnapshot(profile, original._key_rows)


def test_snapshot_constructor_rejects_duplicate_profile_headers(discovery_pair):
    from dataclasses import replace

    original = discovery_pair[0]
    repeated = replace(original.profile.columns[1], name=original.profile.columns[0].name)
    profile = replace(
        original.profile,
        columns=(original.profile.columns[0], repeated, original.profile.columns[2]),
    )
    with pytest.raises(ValueError):
        discovery.DiscoverySnapshot(profile, original._key_rows)


@pytest.mark.parametrize(
    ("values_a", "values_b", "counts"),
    [
        (["A", "B"], ["A", "B"], (2, 2, 2, 2, 2, 2, 0, 0, 2, 2, 2, 0, 0)),
        (["A", "B", "C"], ["B", "C", "D", "E"], (3, 4, 3, 4, 3, 4, 0, 0, 2, 2, 2, 1, 2)),
        (["a", "b"], ["A", "B"], (2, 2, 2, 2, 2, 2, 0, 0, 0, 0, 0, 2, 2)),
        (
            [" A ", "A", "A", "B", "X", "", " \t"],
            ["A", "B", "B", "Y", "Z", ""],
            (7, 6, 5, 5, 3, 4, 2, 1, 2, 4, 3, 1, 2),
        ),
        ([], [], (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    ],
)
def test_single_key_compatibility_counts_distinct_overlap_and_physical_coverage(
    values_a, values_b, counts
):
    snapshot_a = discovery.discover_csv(_csv(values_a), source=Source.A)
    snapshot_b = discovery.discover_csv(_csv(values_b), source=Source.B)
    result = discovery.compare_key_mapping(
        snapshot_a, snapshot_b, key_pairs=[("candidate", "candidate")]
    )
    assert result == discovery.KeyCompatibility((("candidate", "candidate"),), *counts)


def test_cross_format_keys_match_only_current_trimmed_and_numeric_canonicalization(xlsx_bytes):
    snapshot_a = discovery.discover_csv(_csv(["1 ", "001"]), source=Source.A)
    data = xlsx_bytes({"Data": [["id", "amount"], [1, 1], ["1", 2]]})
    snapshot_b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
    result = discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=[("candidate", "id")])
    assert result == discovery.KeyCompatibility(
        (("candidate", "id"),), 2, 2, 2, 2, 2, 1, 0, 1, 1, 1, 2, 1, 0
    )
    records_b = ingest_xlsx(
        data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
    )
    assert snapshot_b._key_rows == tuple(
        (record.key[0], str(record.amount)) for record in records_b
    )


def test_xlsx_invalid_key_types_cannot_overlap_csv_evidence_text(xlsx_bytes):
    values = [True, False, "=1+1", "#N/A", timedelta(days=1)]
    text_values = ["TRUE", "FALSE", "=1+1", "#N/A", "1 day, 0:00:00"]
    snapshot_a = discovery.discover_csv(_csv(text_values), source=Source.A)
    data = xlsx_bytes({"Data": [["id", "amount"], *[[value, 1] for value in values]]})
    snapshot_b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
    result = discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=[("candidate", "id")])
    assert result == discovery.KeyCompatibility(
        (("candidate", "id"),), 5, 5, 5, 0, 5, 0, 0, 0, 0, 0, 0, 5, 0
    )


def test_composite_second_component_resolves_duplicates_without_recommending_a_mode():
    rows = [["V1", "100"], ["V1", "101"], ["V2", "200"]]
    snapshot_a = _snapshot_csv(["vendor", "invoice"], rows, source=Source.A)
    snapshot_b = _snapshot_csv(["supplier", "ref"], rows, source=Source.B)
    single = discovery.compare_key_mapping(
        snapshot_a, snapshot_b, key_pairs=[("vendor", "supplier")]
    )
    composite = discovery.compare_key_mapping(
        snapshot_a, snapshot_b, key_pairs=[("vendor", "supplier"), ("invoice", "ref")]
    )
    assert single.duplicate_excess_count_a == single.duplicate_excess_count_b == 1
    assert single.overlap_distinct_count == 2
    assert composite == discovery.KeyCompatibility(
        (("vendor", "supplier"), ("invoice", "ref")), 3, 3, 3, 3, 3, 3, 0, 0, 3, 3, 3, 0, 0
    )


def test_composite_eligibility_requires_complete_components_in_the_same_physical_row():
    snapshot_a = _snapshot_csv(
        ["vendor", "invoice"],
        [["V1", "100"], ["V1", ""], ["", "101"], ["V2", "200"]],
        source=Source.A,
    )
    snapshot_b = _snapshot_csv(
        ["supplier", "ref"], [["V1", "100"], ["V1", "101"], ["V2", ""]], source=Source.B
    )
    result = discovery.compare_key_mapping(
        snapshot_a, snapshot_b, key_pairs=[("vendor", "supplier"), ("invoice", "ref")]
    )
    assert result == discovery.KeyCompatibility(
        (("vendor", "supplier"), ("invoice", "ref")), 4, 3, 2, 2, 2, 2, 0, 0, 1, 1, 1, 1, 1
    )


def test_composite_mapping_order_is_preserved_and_crossed_correspondence_changes_overlap():
    snapshot_a = _snapshot_csv(["first", "second"], [["x", "y"], ["z", "w"]], source=Source.A)
    snapshot_b = _snapshot_csv(["left", "right"], [["x", "y"], ["q", "r"]], source=Source.B)
    selected = [["first", "left"], ["second", "right"]]
    forward = discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=selected)
    reordered = discovery.compare_key_mapping(
        snapshot_a, snapshot_b, key_pairs=list(reversed(selected))
    )
    crossed = discovery.compare_key_mapping(
        snapshot_a, snapshot_b, key_pairs=[("first", "right"), ("second", "left")]
    )
    assert forward.key_pairs == (("first", "left"), ("second", "right"))
    assert reordered.key_pairs == (("second", "right"), ("first", "left"))
    assert forward != reordered
    for field in fields(discovery.KeyCompatibility):
        if field.name != "key_pairs":
            assert getattr(forward, field.name) == getattr(reordered, field.name)
    assert forward.overlap_distinct_count == 1
    assert crossed.overlap_distinct_count == 0
    selected[0][0] = "changed"
    selected.append(["amount", "amount"])
    assert forward.key_pairs == (("first", "left"), ("second", "right"))


@pytest.mark.parametrize("rows", [(["ab", "c"], ["a", "bc"]), (["a|b", "c"], ["a", "b|c"])])
def test_composite_keys_are_tuples_and_cannot_collide_through_string_concatenation(rows):
    snapshot_a = _snapshot_csv(["first", "second"], [rows[0]], source=Source.A)
    snapshot_b = _snapshot_csv(["first", "second"], [rows[1]], source=Source.B)
    result = discovery.compare_key_mapping(
        snapshot_a, snapshot_b, key_pairs=[("first", "first"), ("second", "second")]
    )
    assert result.overlap_distinct_count == 0
    assert result.only_a_distinct_count == result.only_b_distinct_count == 1


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("wrong", [None, {}, "snapshot", 1, object()])
def test_compatibility_rejects_wrong_snapshot_object_types(discovery_pair, side, wrong):
    snapshot_a, snapshot_b = discovery_pair
    if side == "a":
        snapshot_a = wrong
    else:
        snapshot_b = wrong
    with pytest.raises(TypeError):
        discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=[("id", "id")])
    with pytest.raises(TypeError):
        discovery.compare_amount_columns(
            snapshot_a, snapshot_b, column_a="amount", column_b="amount"
        )


def test_compatibility_rejects_table_profiles_without_snapshot_observations(discovery_pair):
    snapshot_a, snapshot_b = discovery_pair
    with pytest.raises(TypeError):
        discovery.compare_key_mapping(snapshot_a.profile, snapshot_b, key_pairs=[("id", "id")])
    with pytest.raises(TypeError):
        discovery.compare_amount_columns(
            snapshot_a, snapshot_b.profile, column_a="amount", column_b="amount"
        )


@pytest.mark.parametrize(
    "directions", [(Source.B, Source.A), (Source.A, Source.A), (Source.B, Source.B)]
)
def test_compatibility_requires_explicit_a_then_b_direction(directions):
    snapshot_a = discovery.discover_csv(_csv(["A"]), source=directions[0])
    snapshot_b = discovery.discover_csv(_csv(["A"]), source=directions[1])
    with pytest.raises(ValueError):
        discovery.compare_key_mapping(
            snapshot_a, snapshot_b, key_pairs=[("candidate", "candidate")]
        )
    with pytest.raises(ValueError):
        discovery.compare_amount_columns(
            snapshot_a, snapshot_b, column_a="amount", column_b="amount"
        )


@pytest.mark.parametrize("selection", [None, "id", b"id", 1, {"id"}, {"id": "id"}])
def test_key_mapping_requires_a_nonstring_sequence(discovery_pair, selection):
    with pytest.raises(TypeError):
        discovery.compare_key_mapping(*discovery_pair, key_pairs=selection)


@pytest.mark.parametrize("selection", [[None], [1], ["id"], [b"id"], [{}], [{"id"}]])
def test_key_mapping_pair_values_must_be_nonstring_sequences(discovery_pair, selection):
    with pytest.raises(TypeError):
        discovery.compare_key_mapping(*discovery_pair, key_pairs=selection)


@pytest.mark.parametrize("selection", [[], [()], [("id",)], [("id", "id", "other")]])
def test_key_mapping_rejects_empty_or_malformed_pairs(discovery_pair, selection):
    with pytest.raises(ValueError):
        discovery.compare_key_mapping(*discovery_pair, key_pairs=selection)


@pytest.mark.parametrize("selection", [[(1, "id")], [(None, "id")], [("id", False)], [([], "id")]])
def test_key_mapping_rejects_nonstring_names_with_type_error(discovery_pair, selection):
    with pytest.raises(TypeError):
        discovery.compare_key_mapping(*discovery_pair, key_pairs=selection)


@pytest.mark.parametrize(
    "selection",
    [
        [("", "id")],
        [("id", " \t")],
        [("missing", "id")],
        [("id", "missing")],
        [(" id ", "id")],
        [("id", "id"), ("id", "other")],
        [("id", "id"), ("other", "id")],
    ],
)
def test_key_mapping_rejects_blank_missing_and_duplicate_exact_selections(
    discovery_pair, selection
):
    with pytest.raises(ValueError):
        discovery.compare_key_mapping(*discovery_pair, key_pairs=selection)


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("name", [None, 1, False, []])
def test_amount_selection_names_must_be_strings(discovery_pair, side, name):
    names = {"column_a": "amount", "column_b": "amount"}
    names[f"column_{side}"] = name
    with pytest.raises(TypeError):
        discovery.compare_amount_columns(*discovery_pair, **names)


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("name", ["", " \t", "missing", " amount "])
def test_amount_selection_names_must_be_nonblank_and_match_headers_exactly(
    discovery_pair, side, name
):
    names = {"column_a": "amount", "column_b": "amount"}
    names[f"column_{side}"] = name
    with pytest.raises(ValueError):
        discovery.compare_amount_columns(*discovery_pair, **names)


def test_cross_format_amount_compatibility_uses_exact_control_totals_and_a_minus_b(xlsx_bytes):
    snapshot_a = _snapshot_csv(
        ["gross", "id"], [["$1,200.00", "A"], ["(42.50)", "B"], ["0.125", "C"]], source=Source.A
    )
    data = xlsx_bytes({"Data": [["net", "id"], [1000, "A"], [-100.5, "B"]]})
    snapshot_b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
    assert discovery.compare_amount_columns(
        snapshot_a, snapshot_b, column_a="gross", column_b="net"
    ) == discovery.AmountCompatibility(
        "gross", "net", 3, 2, 3, 2, 0, 0, Decimal("1157.625"), Decimal("899.5"), Decimal("258.125")
    )


@pytest.mark.parametrize("invalid", ["word", "", " \t"])
@pytest.mark.parametrize("side", ["a", "b"])
def test_amount_compatibility_with_partial_invalidity_or_blanks_has_no_difference(side, invalid):
    values_a = ["1", invalid, "2"] if side == "a" else ["1", "2"]
    values_b = ["1", invalid, "2"] if side == "b" else ["1", "2"]
    snapshot_a = discovery.discover_csv(_csv(values_a), source=Source.A)
    snapshot_b = discovery.discover_csv(_csv(values_b), source=Source.B)
    result = discovery.compare_amount_columns(
        snapshot_a, snapshot_b, column_a="candidate", column_b="candidate"
    )
    assert result.parseable_count_a == result.parseable_count_b == 2
    assert result.blank_count_a == (1 if side == "a" and not invalid.strip() else 0)
    assert result.blank_count_b == (1 if side == "b" and not invalid.strip() else 0)
    assert result.total_a == (None if side == "a" else Decimal("3"))
    assert result.total_b == (None if side == "b" else Decimal("3"))
    assert result.control_difference is None


@pytest.mark.parametrize("values_a,values_b", [([], []), ([], ["1"]), (["1"], [])])
def test_amount_compatibility_for_empty_tables_has_no_difference(values_a, values_b):
    snapshot_a = discovery.discover_csv(_csv(values_a), source=Source.A)
    snapshot_b = discovery.discover_csv(_csv(values_b), source=Source.B)
    result = discovery.compare_amount_columns(
        snapshot_a, snapshot_b, column_a="candidate", column_b="candidate"
    )
    assert result.row_count_a == result.parseable_count_a == len(values_a)
    assert result.row_count_b == result.parseable_count_b == len(values_b)
    assert result.total_a == (Decimal("1") if values_a else None)
    assert result.total_b == (Decimal("1") if values_b else None)
    assert result.control_difference is None


def test_amount_compatibility_handles_negative_totals_directionally():
    snapshot_a = discovery.discover_csv(_csv(["-10", "(0.25)"]), source=Source.A)
    snapshot_b = discovery.discover_csv(_csv(["-12.5"]), source=Source.B)
    result = discovery.compare_amount_columns(
        snapshot_a, snapshot_b, column_a="candidate", column_b="candidate"
    )
    assert result.total_a == Decimal("-10.25")
    assert result.total_b == Decimal("-12.5")
    assert result.control_difference == Decimal("2.25")


def test_amount_compatibility_is_exact_under_hostile_decimal_context(xlsx_bytes):
    text = _csv(["12345678901234567890.123456789", "0.000000001"])
    data = xlsx_bytes({"Data": [["gross", "id"], ["12345678901234567890.123456789", "A"]]})
    with localcontext() as context:
        context.prec = 2
        context.Emax = 3
        context.Emin = -3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        context.clear_flags()
        before = context.copy()
        snapshot_a = discovery.discover_csv(text, source=Source.A)
        snapshot_b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
        result = discovery.compare_amount_columns(
            snapshot_a, snapshot_b, column_a="candidate", column_b="gross"
        )
        assert context.prec == before.prec
        assert context.Emax == before.Emax
        assert context.Emin == before.Emin
        assert context.traps == before.traps
        assert context.flags == before.flags
    assert result.total_a == Decimal("12345678901234567890.123456790")
    assert result.total_b == Decimal("12345678901234567890.123456789")
    assert result.control_difference == Decimal("0.000000001")


def test_compatibility_uses_closed_stream_snapshot_without_reparsing_or_reconversion(
    xlsx_bytes, monkeypatch
):
    stream = StringIO("ignored\nid,amount\n A ,1\nA,2\n", newline="")
    stream.readline()
    snapshot_a = discovery.discover_csv(stream, source=Source.A)
    assert not stream.closed
    assert stream.read() == ""
    stream.close()
    data = xlsx_bytes({"Data": [["id", "amount"], ["A", 3]]})
    snapshot_b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")

    def fail(*args, **kwargs):
        raise AssertionError("compatibility must use immutable stored observations")

    for name in (
        "discover_csv",
        "discover_xlsx",
        "profile_csv",
        "profile_xlsx",
        "_open",
        "_iter_rows",
        "_data_rows",
        "_key",
        "_amount",
        "parse_amount",
    ):
        monkeypatch.setattr(discovery, name, fail)
    keys = discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=[("id", "id")])
    amounts = discovery.compare_amount_columns(
        snapshot_a, snapshot_b, column_a="amount", column_b="amount"
    )
    assert keys.overlap_distinct_count == 1
    assert keys.overlap_row_count_a == 2
    assert keys.overlap_row_count_b == 1
    assert amounts.total_a == amounts.total_b == Decimal("3")
    assert amounts.control_difference == Decimal("0")


def test_key_and_amount_diagnostics_are_deterministic_across_physical_row_orders():
    rows_a = [["A", "x", "1"], ["B", "y", "2"], ["A", "x", "3"]]
    rows_b = [["B", "y", "2"], ["A", "x", "4"]]
    snapshot_a = _snapshot_csv(["id", "other", "amount"], rows_a, source=Source.A)
    snapshot_b = _snapshot_csv(["id", "other", "amount"], rows_b, source=Source.B)
    reverse_a = _snapshot_csv(["id", "other", "amount"], list(reversed(rows_a)), source=Source.A)
    reverse_b = _snapshot_csv(["id", "other", "amount"], list(reversed(rows_b)), source=Source.B)
    selection = [("id", "id"), ("other", "other")]
    first = discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=selection)
    assert first == discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=selection)
    assert first == discovery.compare_key_mapping(reverse_a, reverse_b, key_pairs=selection)
    amounts = discovery.compare_amount_columns(
        snapshot_a, snapshot_b, column_a="amount", column_b="amount"
    )
    assert amounts == discovery.compare_amount_columns(
        reverse_a, reverse_b, column_a="amount", column_b="amount"
    )
    assert snapshot_a._key_rows == (("A", "x", "1"), ("B", "y", "2"), ("A", "x", "3"))


def test_stage2_diagnostics_leave_ingestion_reconciliation_and_export_bytes_unchanged(xlsx_bytes):
    text = "id,amount,notes\nA,100,memo\nDUP,1,\nDUP,2,other\n"
    data = xlsx_bytes({"Data": [["id", "amount", "notes"], ["A", 100.01, "=1+1"], ["DUP", 3]]})
    before_a = ingest_csv(text, source=Source.A, key_columns=["id"], amount_column="amount")
    before_b = ingest_xlsx(
        data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
    )
    before = reconcile(before_a, before_b, amount_tolerance=Decimal("0.01"))
    exported = export_exceptions_csv(before)
    snapshot_a = discovery.discover_csv(text, source=Source.A)
    snapshot_b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
    discovery.compare_key_mapping(snapshot_a, snapshot_b, key_pairs=[("notes", "notes")])
    discovery.compare_amount_columns(snapshot_a, snapshot_b, column_a="amount", column_b="amount")
    after_a = ingest_csv(text, source=Source.A, key_columns=["id"], amount_column="amount")
    after_b = ingest_xlsx(
        data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
    )
    after = reconcile(after_a, after_b, amount_tolerance=Decimal("0.01"))
    assert before_a == after_a
    assert before_b == after_b
    assert before == after
    assert exported == export_exceptions_csv(after)
    assert snapshot_b._key_rows[0][2] is None


def test_stage2_symbols_remain_outside_the_root_public_api():
    names = {
        "DiscoverySnapshot",
        "KeyCompatibility",
        "AmountCompatibility",
        "discover_csv",
        "discover_xlsx",
        "compare_key_mapping",
        "compare_amount_columns",
    }
    assert names.isdisjoint(tallydiff.__all__)
    assert all(not hasattr(tallydiff, name) for name in names)


@pytest.mark.parametrize(
    ("column_a", "column_b", "tokens"),
    [
        ("Invoice Amount", "Invoice Amount", ("invoice", "amount")),
        ("INVOICE AMOUNT", "invoice amount", ("invoice", "amount")),
        ("  Invoice___Amount---", "invoice.amount", ("invoice", "amount")),
        ("Account / ID: [Number]", "account-id-number", ("account", "id", "number")),
        ("Invoice💵Amount", "invoice\tamount", ("invoice", "amount")),
        ("Invoice-2026", "invoice 2026", ("invoice", "2026")),
        ("MÉMO 账单 № １２3", "mémo_账单_１２3", ("mémo", "账单", "１２3")),
        ("Straße 99", "STRASSE_99", ("strasse", "99")),
    ],
)
def test_header_compatibility_uses_generic_casefolded_unicode_alphanumeric_tokens(
    column_a, column_b, tokens
):
    result = discovery.compare_headers(column_a, column_b)
    assert result == discovery.HeaderCompatibility(
        column_a, column_b, " ".join(tokens), " ".join(tokens), tokens, tokens, True, tokens
    )
    assert result.column_a == column_a
    assert result.column_b == column_b
    assert result == discovery.compare_headers(column_a, column_b)


def test_header_shared_tokens_are_unique_in_first_a_order_and_keep_source_repetitions():
    result = discovery.compare_headers("Id-ID invoice id", "invoice / id / invoice")
    assert result == discovery.HeaderCompatibility(
        "Id-ID invoice id",
        "invoice / id / invoice",
        "id id invoice id",
        "invoice id invoice",
        ("id", "id", "invoice", "id"),
        ("invoice", "id", "invoice"),
        False,
        ("id", "invoice"),
    )
    reversed_result = discovery.compare_headers(result.column_b, result.column_a)
    assert reversed_result.shared_tokens == ("invoice", "id")


def test_header_normalized_equality_requires_token_order():
    result = discovery.compare_headers("amount invoice", "invoice amount")
    assert result.tokens_a == ("amount", "invoice")
    assert result.tokens_b == ("invoice", "amount")
    assert result.shared_tokens == ("amount", "invoice")
    assert not result.exact_normalized_match


@pytest.mark.parametrize(
    "column_a,column_b",
    [
        ("Vendor ID", "Supplier"),
        ("Invoice", "Invoce"),
        ("Invoices Amounts", "Invoice Amount"),
        ("Gross", "Amount"),
        ("Cafe\u0301", "Café"),
    ],
)
def test_header_compatibility_does_not_stem_infer_aliases_or_fuzzy_match(column_a, column_b):
    result = discovery.compare_headers(column_a, column_b)
    assert not result.exact_normalized_match
    assert result.shared_tokens == ()


def test_header_punctuation_only_nonblank_names_have_empty_normalized_forms():
    result = discovery.compare_headers("___---", "!!!")
    assert result.normalized_a == result.normalized_b == ""
    assert result.tokens_a == result.tokens_b == result.shared_tokens == ()
    assert result.exact_normalized_match


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("name", [None, 1, True, [], b"id"])
def test_header_compatibility_requires_string_names(side, name):
    names = {"column_a": "id", "column_b": "id"}
    names[f"column_{side}"] = name
    with pytest.raises(TypeError):
        discovery.compare_headers(**names)


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("name", ["", " ", "\t\n", "\u2003"])
def test_header_compatibility_requires_nonblank_names(side, name):
    names = {"column_a": "id", "column_b": "id"}
    names[f"column_{side}"] = name
    with pytest.raises(ValueError):
        discovery.compare_headers(**names)


def test_header_compatibility_contract_is_frozen_slotted_and_has_only_factual_fields():
    result = discovery.compare_headers("Invoice Number", "Invoice Ref")
    assert [field.name for field in fields(discovery.HeaderCompatibility)] == [
        "column_a",
        "column_b",
        "normalized_a",
        "normalized_b",
        "tokens_a",
        "tokens_b",
        "exact_normalized_match",
        "shared_tokens",
    ]
    assert result.shared_tokens == ("invoice",)
    assert not result.exact_normalized_match
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.normalized_a = "changed"
    with pytest.raises(TypeError):
        result.tokens_a[0] = "changed"
    assert hash(result) == hash(discovery.compare_headers("Invoice Number", "Invoice Ref"))


def test_stage3_header_symbols_remain_outside_the_root_public_api():
    names = {"HeaderCompatibility", "compare_headers"}
    assert names.isdisjoint(tallydiff.__all__)
    assert all(not hasattr(tallydiff, name) for name in names)
