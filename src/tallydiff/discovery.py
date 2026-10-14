"""Deterministic, advisory file profiles independent of reconciliation mappings."""

import csv
from collections import Counter
from collections.abc import Sequence
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


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    """A profile and row-aligned baseline key candidates, without source handles.

    Each private row has one entry per profile column, in header order. ``None``
    denotes an ineligible key component. Rows are retained before any selection
    or filtering so composite candidates preserve physical row correspondence.
    """

    profile: TableProfile
    _key_rows: tuple[tuple[str | None, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.profile, TableProfile):
            raise TypeError("profile must be a TableProfile")
        if not isinstance(self.profile.source, Source):
            raise TypeError("profile source must be a Source enum member")
        if self.profile.worksheet is not None and not isinstance(self.profile.worksheet, str):
            raise TypeError("profile worksheet must be a string or None")
        if type(self.profile.row_count) is not int:
            raise TypeError("profile row_count must be an integer")
        if self.profile.row_count < 0:
            raise ValueError("profile row_count must be nonnegative")
        if not isinstance(self.profile.columns, tuple):
            raise TypeError("profile columns must be a tuple of ColumnProfile objects")
        names: set[str] = set()
        for column in self.profile.columns:
            if not isinstance(column, ColumnProfile):
                raise TypeError("profile columns must be a tuple of ColumnProfile objects")
            if not isinstance(column.name, str):
                raise TypeError("profile column names must be strings")
            if not column.name.strip() or column.name in names:
                raise ValueError("profile column names must be nonblank and distinct")
            names.add(column.name)
            for name in (
                "row_count",
                "blank_count",
                "nonblank_count",
                "distinct_evidence_count",
                "key_eligible_count",
                "distinct_key_count",
                "duplicate_excess_count",
                "amount_parseable_count",
            ):
                if type(getattr(column, name)) is not int:
                    raise TypeError(f"profile column {name} must be an integer")
                if getattr(column, name) < 0:
                    raise ValueError(f"profile column {name} must be nonnegative")
            if column.row_count != self.profile.row_count:
                raise ValueError("profile column row_count must match the table row_count")
            if column.amount_total is not None:
                if not isinstance(column.amount_total, Decimal):
                    raise TypeError("profile amount_total must be a Decimal or None")
                if not column.amount_total.is_finite():
                    raise ValueError("profile amount_total must be finite")
        if not names:
            raise ValueError("profile must contain one or more columns")
        if not isinstance(self._key_rows, tuple):
            raise TypeError("key rows must be a tuple of tuples")
        if len(self._key_rows) != self.profile.row_count:
            raise ValueError("key row count must match the profile row_count")
        for row in self._key_rows:
            if not isinstance(row, tuple):
                raise TypeError("key rows must be a tuple of tuples")
            if len(row) != len(self.profile.columns):
                raise ValueError("key row width must match the profile columns")
            for value in row:
                if value is not None:
                    if not isinstance(value, str):
                        raise TypeError("key observations must be strings or None")
                    if not value or value != value.strip():
                        raise ValueError("key observations must be nonblank canonical strings")


@dataclass(frozen=True, slots=True)
class KeyCompatibility:
    """Directional counts for an ordered candidate key mapping; no ranking."""

    key_pairs: tuple[tuple[str, str], ...]
    row_count_a: int
    row_count_b: int
    eligible_row_count_a: int
    eligible_row_count_b: int
    distinct_key_count_a: int
    distinct_key_count_b: int
    duplicate_excess_count_a: int
    duplicate_excess_count_b: int
    overlap_distinct_count: int
    overlap_row_count_a: int
    overlap_row_count_b: int
    only_a_distinct_count: int
    only_b_distinct_count: int


@dataclass(frozen=True, slots=True)
class AmountCompatibility:
    """Profile amount facts and an exact A-minus-B control difference."""

    column_a: str
    column_b: str
    row_count_a: int
    row_count_b: int
    parseable_count_a: int
    parseable_count_b: int
    blank_count_a: int
    blank_count_b: int
    total_a: Decimal | None
    total_b: Decimal | None
    control_difference: Decimal | None


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


def discover_csv(data: str | TextIO, *, source: Source) -> DiscoverySnapshot:
    """Discover decoded CSV text without selecting any key or amount columns.

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
    key_rows: list[tuple[str | None, ...]] = []
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
            observed_keys: list[str | None] = []
            for column, value in zip(columns, values, strict=True):
                key = value.strip() or None
                try:
                    amount = parse_amount(value)
                except AmountParseError:
                    amount = None
                column.add(value, key, amount)
                observed_keys.append(key)
            key_rows.append(tuple(observed_keys))
            row_count += 1
            source_row += 1
    except csv.Error as exc:
        raise IngestionError(source, f"invalid CSV: {exc}", source_row=source_row) from None
    except (OSError, UnicodeError) as exc:
        raise IngestionError(
            source, f"could not read CSV text: {exc}", source_row=source_row
        ) from None
    profile = TableProfile(
        source, None, row_count, tuple(column.finish(row_count) for column in columns)
    )
    return DiscoverySnapshot(profile, tuple(key_rows))


def profile_csv(data: str | TextIO, *, source: Source) -> TableProfile:
    """Return the profile from CSV discovery, preserving Stage 1 semantics."""

    return discover_csv(data, source=source).profile


def discover_xlsx(data: bytes, *, source: Source, worksheet: str) -> DiscoverySnapshot:
    """Discover one explicit standard XLSX worksheet using ingestion conversions.

    Stream actual worksheet XML, ignoring trailing wholly empty rows and rejecting
    interior empty rows or data beyond the headers. Evidence uses stored cell
    values, never displayed formatting or evaluated formulas. Formulas, errors and
    booleans are ineligible key/amount candidates. Other candidate failures are
    counted; failures to read or represent the source evidence remain input errors.
    """

    row_count = 0
    key_rows: list[tuple[str | None, ...]] = []
    with _open(data, source, worksheet) as workbook:
        rows = _iter_rows(_sheet(workbook, worksheet, source), source, worksheet)
        try:
            header = _header(rows, source, worksheet)
            columns = tuple(_ColumnAccumulator(name) for name in header)
            for source_row, cells in _data_rows(rows, source, worksheet, header):
                observed_keys: list[str | None] = []
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
                    observed_keys.append(key)
                key_rows.append(tuple(observed_keys))
                row_count += 1
        finally:
            rows.close()
    profile = TableProfile(
        source, worksheet, row_count, tuple(column.finish(row_count) for column in columns)
    )
    return DiscoverySnapshot(profile, tuple(key_rows))


def profile_xlsx(data: bytes, *, source: Source, worksheet: str) -> TableProfile:
    """Return the profile from XLSX discovery, preserving Stage 1 semantics."""

    return discover_xlsx(data, source=source, worksheet=worksheet).profile


def _validate_snapshot_direction(
    snapshot_a: DiscoverySnapshot, snapshot_b: DiscoverySnapshot
) -> None:
    for name, snapshot in (("snapshot_a", snapshot_a), ("snapshot_b", snapshot_b)):
        if not isinstance(snapshot, DiscoverySnapshot):
            raise TypeError(f"{name} must be a DiscoverySnapshot")
    if snapshot_a.profile.source is not Source.A or snapshot_b.profile.source is not Source.B:
        raise ValueError("snapshot_a must be Source.A and snapshot_b must be Source.B")


def _column_index(snapshot: DiscoverySnapshot, name: str) -> int:
    source = snapshot.profile.source
    if not isinstance(name, str):
        raise TypeError(f"File {source.value} column name must be a string")
    if not name.strip():
        raise ValueError(f"File {source.value} column name must be nonblank")
    for index, column in enumerate(snapshot.profile.columns):
        if column.name == name:
            return index
    raise ValueError(f"File {source.value} column {name!r} is missing")


def _key_counts(snapshot: DiscoverySnapshot, indices: tuple[int, ...]) -> Counter[tuple[str, ...]]:
    counts: Counter[tuple[str, ...]] = Counter()
    for row in snapshot._key_rows:
        components = tuple(row[index] for index in indices)
        if all(component is not None for component in components):
            counts[components] += 1
    return counts


def compare_key_mapping(
    snapshot_a: DiscoverySnapshot,
    snapshot_b: DiscoverySnapshot,
    *,
    key_pairs: Sequence[tuple[str, str]],
) -> KeyCompatibility:
    """Count complete baseline composite keys and physical-row overlap.

    Pair order defines component position; names are matched exactly. An eligible
    row requires every selected component. Repeated rows contribute individually
    to coverage counts even though distinct overlap uses one key per value.
    """

    _validate_snapshot_direction(snapshot_a, snapshot_b)
    if not isinstance(key_pairs, Sequence) or isinstance(key_pairs, str | bytes | bytearray):
        raise TypeError("key_pairs must be a sequence of column-name pairs")
    selections = tuple(key_pairs)
    if not selections:
        raise ValueError("select one or more key column pairs")
    pairs: list[tuple[str, str]] = []
    indices_a: list[int] = []
    indices_b: list[int] = []
    seen_a: set[str] = set()
    seen_b: set[str] = set()
    for pair in selections:
        if not isinstance(pair, Sequence) or isinstance(pair, str | bytes | bytearray):
            raise TypeError("each key pair must be a sequence of column names")
        if len(pair) != 2:
            raise ValueError("each key pair must contain exactly two column names")
        name_a, name_b = pair
        index_a = _column_index(snapshot_a, name_a)
        index_b = _column_index(snapshot_b, name_b)
        if name_a in seen_a:
            raise ValueError("selected File A key columns must be distinct")
        if name_b in seen_b:
            raise ValueError("selected File B key columns must be distinct")
        seen_a.add(name_a)
        seen_b.add(name_b)
        pairs.append((name_a, name_b))
        indices_a.append(index_a)
        indices_b.append(index_b)

    counts_a = _key_counts(snapshot_a, tuple(indices_a))
    counts_b = _key_counts(snapshot_b, tuple(indices_b))
    eligible_a = sum(counts_a.values())
    eligible_b = sum(counts_b.values())
    overlap = sum(key in counts_b for key in counts_a)
    return KeyCompatibility(
        key_pairs=tuple(pairs),
        row_count_a=snapshot_a.profile.row_count,
        row_count_b=snapshot_b.profile.row_count,
        eligible_row_count_a=eligible_a,
        eligible_row_count_b=eligible_b,
        distinct_key_count_a=len(counts_a),
        distinct_key_count_b=len(counts_b),
        duplicate_excess_count_a=eligible_a - len(counts_a),
        duplicate_excess_count_b=eligible_b - len(counts_b),
        overlap_distinct_count=overlap,
        overlap_row_count_a=sum(count for key, count in counts_a.items() if key in counts_b),
        overlap_row_count_b=sum(count for key, count in counts_b.items() if key in counts_a),
        only_a_distinct_count=len(counts_a) - overlap,
        only_b_distinct_count=len(counts_b) - overlap,
    )


def compare_amount_columns(
    snapshot_a: DiscoverySnapshot,
    snapshot_b: DiscoverySnapshot,
    *,
    column_a: str,
    column_b: str,
) -> AmountCompatibility:
    """Return profile amount facts, with an exact difference only for two totals."""

    _validate_snapshot_direction(snapshot_a, snapshot_b)
    amount_a = snapshot_a.profile.columns[_column_index(snapshot_a, column_a)]
    amount_b = snapshot_b.profile.columns[_column_index(snapshot_b, column_b)]
    total_a = amount_a.amount_total
    total_b = amount_b.amount_total
    difference = (
        sum_decimals((total_a, total_b.copy_negate()))
        if total_a is not None and total_b is not None
        else None
    )
    return AmountCompatibility(
        column_a=column_a,
        column_b=column_b,
        row_count_a=snapshot_a.profile.row_count,
        row_count_b=snapshot_b.profile.row_count,
        parseable_count_a=amount_a.amount_parseable_count,
        parseable_count_b=amount_b.amount_parseable_count,
        blank_count_a=amount_a.blank_count,
        blank_count_b=amount_b.blank_count,
        total_a=total_a,
        total_b=total_b,
        control_difference=difference,
    )
