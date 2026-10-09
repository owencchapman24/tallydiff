"""Atomic CSV ingestion with explicit mappings and preserved source evidence."""

import csv
from collections.abc import Iterator, Sequence
from io import StringIO
from typing import TextIO

from tallydiff.amounts import AmountParseError, parse_amount
from tallydiff.models import Source, SourceRecord


class IngestionError(ValueError):
    """A blocking input error with context suitable for display or inspection."""

    def __init__(
        self,
        source: Source,
        reason: str,
        *,
        source_row: int | None = None,
        column: str | None = None,
        value: object = None,
        worksheet: str | None = None,
    ) -> None:
        self.source = source
        self.worksheet = worksheet
        self.source_row = source_row
        self.column = column
        self.value = value
        self.reason = reason
        location = f"File {source.value}"
        if worksheet is not None:
            location += f", worksheet {worksheet!r}"
        if source_row is not None:
            row_label = "worksheet row" if worksheet is not None else "source record"
            location += f", {row_label} {source_row}"
        if column is not None:
            location += f", column {column!r}"
        detail = f" (value {value!r})" if value is not None else ""
        super().__init__(f"{location}: {reason}{detail}")


def inspect_csv_columns(data: str | TextIO, *, source: Source) -> tuple[str, ...]:
    """Read and validate only the header, preserving column order and exact names.

    This consumes the header from a caller-owned stream without closing it.
    Data records are validated later by ``ingest_csv`` with explicit mappings.
    """

    if not isinstance(source, Source):
        raise TypeError("source must be a Source enum member")
    stream = StringIO(data, newline="") if isinstance(data, str) else data
    return tuple(_read_header(csv.reader(stream, strict=True), source))


def ingest_csv(
    data: str | TextIO,
    *,
    source: Source,
    key_columns: Sequence[str],
    amount_column: str,
    comparison_columns: Sequence[str] = (),
) -> tuple[SourceRecord, ...]:
    """Validate an entire comma-separated CSV before returning any records.

    ``data`` is decoded CSV text or an open text stream at its current position.
    Open files with ``newline=""`` to preserve embedded line endings. Caller-owned
    streams are consumed but never closed. Header names are matched exactly.

    The header is CSV record 1 and the first data record is record 2. A quoted
    multiline field belongs to one record, irrespective of physical line count.
    Blank records and rows with missing or extra fields are rejected. Selected
    comparison fields retain their exact evidence strings, including blanks.
    """

    if not isinstance(source, Source):
        raise TypeError("source must be a Source enum member")
    if not isinstance(key_columns, Sequence) or isinstance(key_columns, str):
        raise IngestionError(source, "key_columns must be a sequence of column names")
    keys = tuple(key_columns)
    if not keys or any(not isinstance(name, str) or not name.strip() for name in keys):
        raise IngestionError(source, "select one or more nonblank key column names", value=keys)
    if len(set(keys)) != len(keys):
        raise IngestionError(source, "selected key columns must be distinct", value=keys)
    if not isinstance(amount_column, str) or not amount_column.strip():
        raise IngestionError(source, "select a nonblank amount column name", value=amount_column)

    comparisons = _validate_comparison_columns(
        comparison_columns, source=source, amount_column=amount_column
    )

    stream = StringIO(data, newline="") if isinstance(data, str) else data
    reader = csv.reader(stream, strict=True)
    records: list[SourceRecord] = []
    source_row = 1
    try:
        header = _read_header(reader, source)
        for column in keys:
            if column not in header:
                raise IngestionError(
                    source, "selected key column is missing", source_row=1, column=column
                )
        if amount_column not in header:
            raise IngestionError(
                source, "selected amount column is missing", source_row=1, column=amount_column
            )
        for column in comparisons:
            if column not in header:
                raise IngestionError(
                    source, "selected comparison column is missing", source_row=1, column=column
                )
        source_row = 2
        for values in reader:
            if len(values) != len(header):
                raise IngestionError(
                    source,
                    f"expected {len(header)} fields, found {len(values)}",
                    source_row=source_row,
                    value=tuple(values),
                )
            raw_fields = dict(zip(header, values, strict=True))
            key = tuple(raw_fields[column].strip() for column in keys)
            for column, component in zip(keys, key, strict=True):
                if not component:
                    raise IngestionError(
                        source,
                        "required key component is blank after trimming",
                        source_row=source_row,
                        column=column,
                        value=raw_fields[column],
                    )
            try:
                amount = parse_amount(raw_fields[amount_column])
            except AmountParseError as exc:
                raise IngestionError(
                    source,
                    str(exc),
                    source_row=source_row,
                    column=amount_column,
                    value=raw_fields[amount_column],
                ) from None
            records.append(SourceRecord(source, source_row, key, amount, raw_fields))
            source_row += 1
    except csv.Error as exc:
        raise IngestionError(source, f"invalid CSV: {exc}", source_row=source_row) from None
    except (OSError, UnicodeError) as exc:
        raise IngestionError(
            source, f"could not read CSV text: {exc}", source_row=source_row
        ) from None
    return tuple(records)


def _validate_comparison_columns(
    columns: Sequence[str],
    *,
    source: Source,
    amount_column: str,
    worksheet: str | None = None,
) -> tuple[str, ...]:
    """Validate secondary selections without changing names or other field roles."""

    if not isinstance(columns, Sequence) or isinstance(columns, str | bytes | bytearray):
        raise IngestionError(
            source, "comparison_columns must be a sequence of column names", worksheet=worksheet
        )
    comparisons = tuple(columns)
    if any(not isinstance(name, str) or not name.strip() for name in comparisons):
        raise IngestionError(
            source,
            "select nonblank comparison column names",
            value=comparisons,
            worksheet=worksheet,
        )
    if len(set(comparisons)) != len(comparisons):
        raise IngestionError(
            source,
            "selected comparison columns must be distinct",
            value=comparisons,
            worksheet=worksheet,
        )
    if amount_column in comparisons:
        raise IngestionError(
            source,
            "comparison columns must not use the amount column",
            column=amount_column,
            worksheet=worksheet,
        )
    return comparisons


def _read_header(reader: Iterator[list[str]], source: Source) -> list[str]:
    try:
        header = next(reader, None)
    except csv.Error as exc:
        raise IngestionError(source, f"invalid CSV: {exc}", source_row=1) from None
    except (OSError, UnicodeError) as exc:
        raise IngestionError(source, f"could not read CSV text: {exc}", source_row=1) from None
    if not header:
        raise IngestionError(source, "missing header", source_row=1)
    seen: set[str] = set()
    for column in header:
        if not column.strip():
            raise IngestionError(
                source, "blank column name", source_row=1, column=column, value=column
            )
        if column in seen:
            raise IngestionError(
                source, "duplicate column name", source_row=1, column=column, value=column
            )
        seen.add(column)
    return header
