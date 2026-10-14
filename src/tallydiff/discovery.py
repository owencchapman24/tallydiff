"""Deterministic, advisory file profiles independent of reconciliation mappings."""

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from io import StringIO
from typing import TextIO

from tallydiff._decimal import sum_decimals
from tallydiff.amounts import AmountParseError, parse_amount
from tallydiff.ingest import IngestionError, _read_header
from tallydiff.models import Source
from tallydiff.xlsx import _amount, _data_rows, _evidence, _header, _iter_rows, _key, _open, _sheet


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """Counts of preserved evidence and baseline key/amount candidates."""

    name: str
    row_count: int
    blank_count: int
    nonblank_count: int
    distinct_evidence_count: int
    key_eligible_count: int
    distinct_key_count: int
    duplicate_excess_count: int
    amount_parseable_count: int
    amount_total: Decimal | None


@dataclass(frozen=True, slots=True)
class TableProfile:
    """One structurally validated table, in exact source column order."""

    source: Source
    worksheet: str | None
    row_count: int
    columns: tuple[ColumnProfile, ...]


@dataclass(slots=True)
class _ColumnAccumulator:
    name: str
    blank_count: int = 0
    evidence: set[str] = field(default_factory=set)
    key_eligible_count: int = 0
    keys: set[str] = field(default_factory=set)
    amount_parseable_count: int = 0
    amounts: list[Decimal] | None = field(default_factory=list)

    def add(self, evidence: str, key: str | None, amount: Decimal | None) -> None:
        if not evidence.strip():
            self.blank_count += 1
        else:
            self.evidence.add(evidence)
        if key is not None:
            self.key_eligible_count += 1
            self.keys.add(key)
        if amount is None:
            # A total can no longer be produced; retain only the candidate count.
            self.amounts = None
        else:
            self.amount_parseable_count += 1
            if self.amounts is not None:
                self.amounts.append(amount)

    def finish(self, row_count: int) -> ColumnProfile:
        return ColumnProfile(
            name=self.name,
            row_count=row_count,
            blank_count=self.blank_count,
            nonblank_count=row_count - self.blank_count,
            distinct_evidence_count=len(self.evidence),
            key_eligible_count=self.key_eligible_count,
            distinct_key_count=len(self.keys),
            duplicate_excess_count=self.key_eligible_count - len(self.keys),
            amount_parseable_count=self.amount_parseable_count,
            amount_total=(
                sum_decimals(self.amounts) if row_count and self.amounts is not None else None
            ),
        )


def profile_csv(data: str | TextIO, *, source: Source) -> TableProfile:
    """Profile decoded CSV text without selecting any key or amount columns.

    Strict parsing, exact headers and logical record lengths follow ``ingest_csv``.
    Whitespace-only text is blank; other evidence remains unchanged. Key candidates
    use only surrounding trim, and amount candidates use ``parse_amount``. Invalid
    amounts are counted without failing the profile. Caller-owned streams are
    consumed from their current position and never closed.
    """

    if not isinstance(source, Source):
        raise TypeError("source must be a Source enum member")
    stream = StringIO(data, newline="") if isinstance(data, str) else data
    reader = csv.reader(stream, strict=True)
    row_count = 0
    source_row = 1
    try:
        header = _read_header(reader, source)
        columns = tuple(_ColumnAccumulator(name) for name in header)
        source_row = 2
        for values in reader:
            if len(values) != len(header):
                raise IngestionError(
                    source,
                    f"expected {len(header)} fields, found {len(values)}",
                    source_row=source_row,
                    value=tuple(values),
                )
            for column, value in zip(columns, values, strict=True):
                key = value.strip() or None
                try:
                    amount = parse_amount(value)
                except AmountParseError:
                    amount = None
                column.add(value, key, amount)
            row_count += 1
            source_row += 1
    except csv.Error as exc:
        raise IngestionError(source, f"invalid CSV: {exc}", source_row=source_row) from None
    except (OSError, UnicodeError) as exc:
        raise IngestionError(
            source, f"could not read CSV text: {exc}", source_row=source_row
        ) from None
    return TableProfile(
        source, None, row_count, tuple(column.finish(row_count) for column in columns)
    )


def profile_xlsx(data: bytes, *, source: Source, worksheet: str) -> TableProfile:
    """Profile one explicit standard XLSX worksheet using ingestion conversions.

    Stream actual worksheet XML, ignoring trailing wholly empty rows and rejecting
    interior empty rows or data beyond the headers. Evidence uses stored cell
    values, never displayed formatting or evaluated formulas. Formulas, errors and
    booleans are ineligible key/amount candidates. Other candidate failures are
    counted; failures to read or represent the source evidence remain input errors.
    """

    row_count = 0
    with _open(data, source, worksheet) as workbook:
        rows = _iter_rows(_sheet(workbook, worksheet, source), source, worksheet)
        try:
            header = _header(rows, source, worksheet)
            columns = tuple(_ColumnAccumulator(name) for name in header)
            for source_row, cells in _data_rows(rows, source, worksheet, header):
                for index, column in enumerate(columns):
                    cell = cells[index] if index < len(cells) else None
                    value = cell.value if cell is not None else None
                    try:
                        evidence = _evidence(value)
                    except ValueError as exc:
                        raise IngestionError(
                            source,
                            str(exc),
                            source_row=source_row,
                            column=column.name,
                            value=value,
                            worksheet=worksheet,
                        ) from None
                    key = None
                    amount = None
                    if cell is None or cell.data_type not in ("f", "e"):
                        try:
                            key = _key(value)
                        except ValueError:
                            pass
                        try:
                            amount = _amount(value)
                        except ValueError:
                            pass
                    column.add(evidence, key, amount)
                row_count += 1
        finally:
            rows.close()
    return TableProfile(
        source, worksheet, row_count, tuple(column.finish(row_count) for column in columns)
    )
