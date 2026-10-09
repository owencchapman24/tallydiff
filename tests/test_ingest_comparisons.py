"""Selected secondary fields preserve evidence and validate source-specific cells."""

import csv
from datetime import date, datetime, time, timedelta
from io import StringIO

import pytest

from tallydiff import IngestionError, Source, ingest_csv, ingest_xlsx

ROWS = [["id", "amount", "Department"], [" INV ", "1.00", "Sales"]]


def _ingest(kind, rows, xlsx_bytes, **options):
    options = {"source": Source.A, "key_columns": ("id",), "amount_column": "amount", **options}
    if kind == "xlsx":
        return ingest_xlsx(xlsx_bytes({"Ledger": rows}), worksheet="Ledger", **options)
    stream = StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(rows)
    return ingest_csv(stream.getvalue(), **options)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_empty_secondary_selection_preserves_default_records_and_evidence(kind, xlsx_bytes):
    default = _ingest(kind, ROWS, xlsx_bytes)
    explicit = _ingest(kind, ROWS, xlsx_bytes, comparison_columns=())
    selected = _ingest(kind, ROWS, xlsx_bytes, comparison_columns=("Department",))

    assert default == explicit == selected
    assert default[0].key == ("INV",)
    assert default[0].source_row == 2
    assert (
        dict(default[0].raw_fields)
        == dict(explicit[0].raw_fields)
        == dict(selected[0].raw_fields)
        == {"id": " INV ", "amount": "1.00", "Department": "Sales"}
    )


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "columns",
    [
        None,
        "Department",
        b"",
        b"Department",
        bytearray(),
        {"Department"},
        {"Department": 1},
        iter(("Department",)),
        42,
    ],
)
def test_secondary_columns_require_a_non_string_sequence(kind, columns, xlsx_bytes):
    with pytest.raises(IngestionError, match="comparison_columns must be a sequence") as error:
        _ingest(kind, ROWS, xlsx_bytes, comparison_columns=columns)
    assert error.value.source is Source.A
    assert error.value.worksheet == ("Ledger" if kind == "xlsx" else None)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
@pytest.mark.parametrize(
    "columns", [("",), (" \t",), ("\u2003",), (None,), (1,), (True,), ("Department", None)]
)
def test_secondary_column_names_must_be_nonblank_strings(kind, columns, xlsx_bytes):
    with pytest.raises(IngestionError, match="nonblank comparison column names"):
        _ingest(kind, ROWS, xlsx_bytes, comparison_columns=columns)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_secondary_column_selections_must_be_distinct(kind, xlsx_bytes):
    with pytest.raises(IngestionError, match="comparison columns must be distinct"):
        _ingest(kind, ROWS, xlsx_bytes, comparison_columns=("Department", "Department"))


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_secondary_selection_cannot_use_the_primary_amount(kind, xlsx_bytes):
    with pytest.raises(IngestionError, match="must not use the amount column") as error:
        _ingest(kind, ROWS, xlsx_bytes, comparison_columns=("amount",))
    assert error.value.column == "amount"


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_secondary_selection_may_overlap_keys_and_preserves_untrimmed_evidence(kind, xlsx_bytes):
    record = _ingest(kind, ROWS, xlsx_bytes, comparison_columns=["id"])[0]

    assert record.key == ("INV",)
    assert record.raw_fields["id"] == " INV "


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
@pytest.mark.parametrize("header_only", [False, True])
def test_missing_secondary_header_blocks_even_without_data(kind, header_only, xlsx_bytes):
    rows = ROWS[:1] if header_only else ROWS
    with pytest.raises(IngestionError, match="selected.*column is missing") as error:
        _ingest(kind, rows, xlsx_bytes, source=Source.B, comparison_columns=("missing",))
    assert error.value.source is Source.B
    assert error.value.source_row == 1
    assert error.value.column == "missing"
    assert error.value.worksheet == ("Ledger" if kind == "xlsx" else None)


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_header_only_input_accepts_valid_secondary_selections(kind, xlsx_bytes):
    assert _ingest(kind, ROWS[:1], xlsx_bytes, comparison_columns=["Department"]) == ()


@pytest.mark.parametrize("kind", ["csv", "xlsx"])
def test_secondary_column_names_are_exact_and_never_trimmed(kind, xlsx_bytes):
    rows = [["id", "amount", " Department "], ["INV", "1", "Sales"]]
    assert (
        _ingest(kind, rows, xlsx_bytes, comparison_columns=(" Department ",))[0].raw_fields[
            " Department "
        ]
        == "Sales"
    )
    with pytest.raises(IngestionError, match="missing"):
        _ingest(kind, rows, xlsx_bytes, comparison_columns=("Department",))


@pytest.mark.parametrize(
    "value",
    [
        "",
        " \t ",
        "Sales",
        "sales",
        "Café",
        "e\u0301",
        "first\nsecond",
        "first\rsecond",
        "first\r\nsecond",
        "=1+1",
        "+001",
        "-001",
        "@SUM(1,2)",
        "#N/A",
    ],
)
def test_csv_secondary_values_remain_exact_text(value, xlsx_bytes):
    rows = [["id", "amount", "Department"], ["INV", "1", value], ["OTHER", "2", "next"]]
    records = _ingest("csv", rows, xlsx_bytes, comparison_columns=("Department",))

    assert records[0].raw_fields["Department"] == value
    assert dict(records[0].raw_fields) == {"id": "INV", "amount": "1", "Department": value}
    assert [record.source_row for record in records] == [2, 3]
    with pytest.raises(TypeError):
        records[0].raw_fields["Department"] = "changed"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        (" Sales ", " Sales "),
        (" \t", " \t"),
        ("sales", "sales"),
        ("Café", "Café"),
        ("e\u0301", "e\u0301"),
        ("first\nsecond", "first\nsecond"),
        (100, "100"),
        (123.25, "123.25"),
        (-0.125, "-0.125"),
        (1e-7, "1E-7"),
        (True, "TRUE"),
        (False, "FALSE"),
        (date(2026, 1, 2), "2026-01-02"),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05"),
        (time(3, 4, 5), "03:04:05"),
        (timedelta(days=1, hours=2, minutes=3, seconds=4), "1 day, 2:03:04"),
    ],
)
def test_xlsx_selected_comparison_uses_existing_deterministic_evidence(value, expected, xlsx_bytes):
    rows = [["id", "amount", "Department"], ["INV", "1.00", value]]
    default = _ingest("xlsx", rows, xlsx_bytes)
    selected = _ingest("xlsx", rows, xlsx_bytes, comparison_columns=("Department",))

    assert selected == default
    assert dict(selected[0].raw_fields) == dict(default[0].raw_fields)
    assert selected[0].raw_fields["Department"] == expected
    assert selected[0].source_row == 2


def test_xlsx_secondary_numeric_values_ignore_display_formatting(xlsx_bytes):
    data = xlsx_bytes(
        {"Ledger": [["id", "amount", "Department"], ["INV", 1, 123.25]]},
        configure=lambda book: setattr(book["Ledger"]["C2"], "number_format", "000000.0000"),
    )
    record = ingest_xlsx(
        data,
        source=Source.A,
        worksheet="Ledger",
        key_columns=("id",),
        amount_column="amount",
        comparison_columns=("Department",),
    )[0]

    assert record.raw_fields["Department"] == "123.25"


@pytest.mark.parametrize(
    ("value", "reason"), [("=1+1", "formula"), ("#N/A", "Excel error"), ("#DIV/0!", "Excel error")]
)
def test_selected_xlsx_formula_or_error_blocks_atomically_with_full_context(
    value, reason, xlsx_bytes
):
    rows = [["id", "amount", "Department"], ["FIRST", 1, "Sales"], ["SECOND", 2, value]]
    with pytest.raises(IngestionError, match=reason) as error:
        _ingest("xlsx", rows, xlsx_bytes, source=Source.B, comparison_columns=("Department",))

    assert error.value.source is Source.B
    assert error.value.worksheet == "Ledger"
    assert error.value.source_row == 3
    assert error.value.column == "Department"
    assert error.value.value == value
    assert "File B, worksheet 'Ledger', worksheet row 3, column 'Department'" in str(error.value)


@pytest.mark.parametrize("value", ["=1+1", "#N/A", "#DIV/0!"])
def test_unselected_xlsx_formula_or_error_remains_evidence(value, xlsx_bytes):
    rows = [["id", "amount", "Department", "unselected"], ["INV", 1, "Sales", value]]
    record = _ingest("xlsx", rows, xlsx_bytes, comparison_columns=("Department",))[0]

    assert record.raw_fields["unselected"] == value
    assert record.raw_fields["Department"] == "Sales"


@pytest.mark.parametrize("value", ["=1+1", "#N/A"])
def test_selected_xlsx_literal_text_is_not_inferred_to_be_a_formula_or_error(value, xlsx_bytes):
    data = xlsx_bytes(
        {"Ledger": [["id", "amount", "Department"], ["INV", 1, value]]},
        configure=lambda book: setattr(book["Ledger"]["C2"], "data_type", "s"),
    )
    record = ingest_xlsx(
        data,
        source=Source.A,
        worksheet="Ledger",
        key_columns=("id",),
        amount_column="amount",
        comparison_columns=("Department",),
    )[0]

    assert record.raw_fields["Department"] == value
