"""Atomic CSV ingestion with explicit mappings and preserved source evidence."""

import csv
from collections.abc import Sequence
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
    ) -> None:
        self.source = source
        self.source_row = source_row
        self.column = column
        self.value = value
        self.reason = reason
        location = f"File {source.value}"
        if source_row is not None:
            location += f", source record {source_row}"
        if column is not None:
            location += f", column {column!r}"
        detail = f" (value {value!r})" if value is not None else ""
        super().__init__(f"{location}: {reason}{detail}")


def ingest_csv(
    data: str | TextIO,
    *,
    source: Source,
    key_columns: Sequence[str],
    amount_column: str,
) -> tuple[SourceRecord, ...]:
    """Validate an entire comma-separated CSV before returning any records.

    ``data`` is decoded CSV text or an open text stream at its current position.
    Open files with ``newline=""`` to preserve embedded line endings. Caller-owned
    streams are consumed but never closed. Header names are matched exactly.

    The header is CSV record 1 and the first data record is record 2. A quoted
    multiline field belongs to one record, irrespective of physical line count.
    Blank records and rows with missing or extra fields are rejected.
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

    stream = StringIO(data, newline="") if isinstance(data, str) else data
    reader = csv.reader(stream, strict=True)
    records: list[SourceRecord] = []
    source_row = 1
    try:
        header = _validate_header(next(reader, None), source, keys, amount_column)
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


def _validate_header(
    header: list[str] | None,
    source: Source,
    keys: tuple[str, ...],
    amount_column: str,
) -> list[str]:
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
    for column in keys:
        if column not in seen:
            raise IngestionError(
                source, "selected key column is missing", source_row=1, column=column
            )
    if amount_column not in seen:
        raise IngestionError(
            source, "selected amount column is missing", source_row=1, column=amount_column
        )
    return header
