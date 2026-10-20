"""Deterministic, advisory file profiles independent of reconciliation mappings."""

import csv
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from io import StringIO
from itertools import combinations
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


def _key_compatibility_from_counts(
    snapshot_a: DiscoverySnapshot,
    snapshot_b: DiscoverySnapshot,
    key_pairs: tuple[tuple[str, str], ...],
    counts_a: Counter[tuple[str, ...]],
    counts_b: Counter[tuple[str, ...]],
) -> KeyCompatibility:
    eligible_a = sum(counts_a.values())
    eligible_b = sum(counts_b.values())
    overlap = sum(key in counts_b for key in counts_a)
    return KeyCompatibility(
        key_pairs=key_pairs,
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
    return _key_compatibility_from_counts(snapshot_a, snapshot_b, tuple(pairs), counts_a, counts_b)


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


@dataclass(frozen=True, slots=True)
class HeaderCompatibility:
    """Conservative lexical facts about two headers, independent of their values."""

    column_a: str
    column_b: str
    normalized_a: str
    normalized_b: str
    tokens_a: tuple[str, ...]
    tokens_b: tuple[str, ...]
    exact_normalized_match: bool
    shared_tokens: tuple[str, ...]


def _header_tokens(column: str) -> tuple[str, ...]:
    if not isinstance(column, str):
        raise TypeError("column names must be strings")
    if not column.strip():
        raise ValueError("column names must be nonblank")
    tokens: list[str] = []
    current: list[str] = []
    for character in column.casefold():
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def compare_headers(column_a: str, column_b: str) -> HeaderCompatibility:
    """Casefold headers and split on nonalphanumeric boundaries, without aliases.

    Source names and token order are preserved. Repeated tokens remain in each
    header's tokens; shared tokens occur once, in first-appearance File A order.
    No source value is normalized or compared by this header-only diagnostic.
    """

    tokens_a = _header_tokens(column_a)
    tokens_b = _header_tokens(column_b)
    normalized_a = " ".join(tokens_a)
    normalized_b = " ".join(tokens_b)
    present_b = set(tokens_b)
    seen: set[str] = set()
    shared: list[str] = []
    for token in tokens_a:
        if token in present_b and token not in seen:
            shared.append(token)
            seen.add(token)
    return HeaderCompatibility(
        column_a=column_a,
        column_b=column_b,
        normalized_a=normalized_a,
        normalized_b=normalized_b,
        tokens_a=tokens_a,
        tokens_b=tokens_b,
        exact_normalized_match=normalized_a == normalized_b,
        shared_tokens=tuple(shared),
    )


MAX_COMPOSITE_BASE_PAIRS = 8
MAX_COMPOSITE_EVALUATIONS = 64
MAX_SUGGESTIONS = 20


@dataclass(frozen=True, slots=True)
class KeyMappingSuggestion:
    """One advisory mapping with its unchanged factual diagnostics."""

    key_pairs: tuple[tuple[str, str], ...]
    compatibility: KeyCompatibility
    headers: tuple[HeaderCompatibility, ...]


@dataclass(frozen=True, slots=True)
class KeySuggestionResult:
    """Best-first candidates, preserving the complete leading evidence tie."""

    suggestions: tuple[KeyMappingSuggestion, ...]
    leading_count: int
    evaluated_single_count: int
    evaluated_composite_count: int
    composite_search_truncated: bool


@dataclass(frozen=True, slots=True)
class AmountMappingSuggestion:
    """A fully runnable advisory amount mapping, without accounting authority."""

    column_a: str
    column_b: str
    compatibility: AmountCompatibility
    header: HeaderCompatibility


@dataclass(frozen=True, slots=True)
class AmountSuggestionResult:
    """Runnable candidates, preserving the complete leading evidence tie."""

    suggestions: tuple[AmountMappingSuggestion, ...]
    leading_count: int
    evaluated_count: int


def _validate_max_suggestions(max_suggestions: int) -> None:
    if type(max_suggestions) is not int:
        raise TypeError("max_suggestions must be an integer")
    if not 1 <= max_suggestions <= MAX_SUGGESTIONS:
        raise ValueError(f"max_suggestions must be between 1 and {MAX_SUGGESTIONS}")


def _positive_exact_header(header: HeaderCompatibility) -> bool:
    return bool(header.normalized_a) and bool(header.normalized_b) and header.exact_normalized_match


def _evidence_ratio(numerator: int, denominator: int) -> Fraction:
    # All defined ratios are nonnegative; -1 is a bottom sentinel, not a percentage.
    return Fraction(numerator, denominator) if denominator else Fraction(-1)


def _key_evidence_rank(
    suggestion: KeyMappingSuggestion,
) -> tuple[bool, Fraction, Fraction, Fraction, int, int, int, int]:
    compatibility = suggestion.compatibility
    return (
        compatibility.overlap_distinct_count > 0,
        min(
            _evidence_ratio(compatibility.overlap_row_count_a, compatibility.eligible_row_count_a),
            _evidence_ratio(compatibility.overlap_row_count_b, compatibility.eligible_row_count_b),
        ),
        min(
            _evidence_ratio(compatibility.eligible_row_count_a, compatibility.row_count_a),
            _evidence_ratio(compatibility.eligible_row_count_b, compatibility.row_count_b),
        ),
        min(
            _evidence_ratio(compatibility.distinct_key_count_a, compatibility.eligible_row_count_a),
            _evidence_ratio(compatibility.distinct_key_count_b, compatibility.eligible_row_count_b),
        ),
        compatibility.overlap_distinct_count,
        sum(_positive_exact_header(header) for header in suggestion.headers),
        sum(len(header.shared_tokens) for header in suggestion.headers),
        -len(suggestion.key_pairs),
    )


def _amount_evidence_rank(suggestion: AmountMappingSuggestion) -> tuple[bool, int]:
    return _positive_exact_header(suggestion.header), len(suggestion.header.shared_tokens)


def _cached_key_counts(
    snapshot: DiscoverySnapshot,
    indices: tuple[int, ...],
    cache: dict[tuple[int, ...], Counter[tuple[str, ...]]],
) -> Counter[tuple[str, ...]]:
    if indices not in cache:
        cache[indices] = _key_counts(snapshot, indices)
    return cache[indices]


def suggest_key_mappings(
    snapshot_a: DiscoverySnapshot,
    snapshot_b: DiscoverySnapshot,
    *,
    max_suggestions: int = 8,
) -> KeySuggestionResult:
    """Rank all single pairs and bounded two-component mappings, without applying any.

    Descending evidence is: positive overlap; minimum overlap/eligible ratio;
    minimum eligible/table ratio; minimum distinct/eligible ratio; distinct overlap;
    nonempty exact-header component count; total shared-token count; lower key
    cardinality. Ratios are exact and undefined ratios rank below every defined
    ratio. Source-header ordinals only order equal evidence, never resolve a tie.

    The best eight single pairs seed composites. A tie crossing that bound is
    cut in source order and reported as truncated; ordinary exclusion of worse
    groups is not. At most 64 distinct-column composites are evaluated. Complete
    leading ties are returned even when larger than ``max_suggestions``.
    """

    _validate_snapshot_direction(snapshot_a, snapshot_b)
    _validate_max_suggestions(max_suggestions)
    ordinals_a = {column.name: index for index, column in enumerate(snapshot_a.profile.columns)}
    ordinals_b = {column.name: index for index, column in enumerate(snapshot_b.profile.columns)}
    cache_a: dict[tuple[int, ...], Counter[tuple[str, ...]]] = {}
    cache_b: dict[tuple[int, ...], Counter[tuple[str, ...]]] = {}
    headers: dict[tuple[str, str], HeaderCompatibility] = {}

    def display_order(key_pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[int, int], ...]:
        return tuple((ordinals_a[name_a], ordinals_b[name_b]) for name_a, name_b in key_pairs)

    def evaluate(key_pairs: tuple[tuple[str, str], ...]) -> KeyMappingSuggestion:
        indices_a = tuple(ordinals_a[name_a] for name_a, _ in key_pairs)
        indices_b = tuple(ordinals_b[name_b] for _, name_b in key_pairs)
        compatibility = _key_compatibility_from_counts(
            snapshot_a,
            snapshot_b,
            key_pairs,
            _cached_key_counts(snapshot_a, indices_a, cache_a),
            _cached_key_counts(snapshot_b, indices_b, cache_b),
        )
        return KeyMappingSuggestion(
            key_pairs, compatibility, tuple(headers[pair] for pair in key_pairs)
        )

    singles: list[KeyMappingSuggestion] = []
    for column_a in snapshot_a.profile.columns:
        for column_b in snapshot_b.profile.columns:
            pair = (column_a.name, column_b.name)
            headers[pair] = compare_headers(*pair)
            singles.append(evaluate((pair,)))
    # Stable evidence sorting preserves explicit source order within ties.
    singles.sort(key=lambda suggestion: display_order(suggestion.key_pairs))
    singles.sort(key=_key_evidence_rank, reverse=True)
    base = singles[:MAX_COMPOSITE_BASE_PAIRS]
    truncated = len(singles) > len(base) and _key_evidence_rank(singles[len(base) - 1]) == (
        _key_evidence_rank(singles[len(base)])
    )
    composite_pairs: list[tuple[tuple[str, str], ...]] = []
    for first, second in combinations(base, 2):
        pair_a = first.key_pairs[0]
        pair_b = second.key_pairs[0]
        if pair_a[0] != pair_b[0] and pair_a[1] != pair_b[1]:
            canonical = tuple(sorted((pair_a, pair_b), key=lambda pair: display_order((pair,))))
            composite_pairs.append(canonical)
    composite_pairs.sort(key=display_order)
    if len(composite_pairs) > MAX_COMPOSITE_EVALUATIONS:
        truncated = True
    evaluated = composite_pairs[:MAX_COMPOSITE_EVALUATIONS]
    candidates = singles + [evaluate(key_pairs) for key_pairs in evaluated]
    candidates.sort(key=lambda suggestion: display_order(suggestion.key_pairs))
    candidates.sort(key=_key_evidence_rank, reverse=True)
    leading_rank = _key_evidence_rank(candidates[0])
    leading_count = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if _key_evidence_rank(candidate) != leading_rank
        ),
        len(candidates),
    )
    return KeySuggestionResult(
        suggestions=tuple(candidates[: max(max_suggestions, leading_count)]),
        leading_count=leading_count,
        evaluated_single_count=len(singles),
        evaluated_composite_count=len(evaluated),
        composite_search_truncated=truncated,
    )


def suggest_amount_mappings(
    snapshot_a: DiscoverySnapshot,
    snapshot_b: DiscoverySnapshot,
    *,
    max_suggestions: int = 8,
) -> AmountSuggestionResult:
    """Evaluate all pairs and suggest only columns with a total on both nonempty tables.

    Descending evidence is only nonempty exact normalized-header equality, then
    shared-token count. Totals and every value statistic are absent from ranking.
    Source-header ordinals order equal evidence without resolving ambiguity. The
    complete leading tie is retained even when larger than ``max_suggestions``.
    """

    _validate_snapshot_direction(snapshot_a, snapshot_b)
    _validate_max_suggestions(max_suggestions)
    candidates: list[AmountMappingSuggestion] = []
    evaluated_count = 0
    # Enumeration supplies explicit A-then-B source order for the stable sort.
    for column_a in snapshot_a.profile.columns:
        for column_b in snapshot_b.profile.columns:
            compatibility = compare_amount_columns(
                snapshot_a, snapshot_b, column_a=column_a.name, column_b=column_b.name
            )
            evaluated_count += 1
            if (
                compatibility.row_count_a > 0
                and compatibility.row_count_b > 0
                and compatibility.total_a is not None
                and compatibility.total_b is not None
            ):
                candidates.append(
                    AmountMappingSuggestion(
                        column_a.name,
                        column_b.name,
                        compatibility,
                        compare_headers(column_a.name, column_b.name),
                    )
                )
    candidates.sort(key=_amount_evidence_rank, reverse=True)
    if not candidates:
        return AmountSuggestionResult((), 0, evaluated_count)
    leading_rank = _amount_evidence_rank(candidates[0])
    leading_count = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if _amount_evidence_rank(candidate) != leading_rank
        ),
        len(candidates),
    )
    return AmountSuggestionResult(
        tuple(candidates[: max(max_suggestions, leading_count)]), leading_count, evaluated_count
    )


MAX_KEY_DIFFERENCE_ROWS = 25_000
MAX_KEY_DIFFERENCE_SIGNATURE_ENTRIES = 100_000
MAX_KEY_DIFFERENCE_CANDIDATES = 20


@dataclass(frozen=True, slots=True)
class PossibleKeyDifference:
    """Two original baseline keys requiring investigation, never established matches."""

    key_a: tuple[str, ...]
    key_b: tuple[str, ...]
    component_index: int
    column_a: str
    column_b: str


@dataclass(frozen=True, slots=True)
class PossibleKeyDifferenceResult:
    """Bounded advisory evidence; a skipped search has no exhaustive candidate count."""

    key_pairs: tuple[tuple[str, str], ...]
    candidates: tuple[PossibleKeyDifference, ...]
    total_candidate_count: int
    display_truncated: bool
    evaluated: bool
    skip_reason: str | None
    ambiguity_suppressed: bool
    signature_entry_count: int | None


def _difference_key_selection(snapshot_a, snapshot_b, key_pairs):
    """Validate selection with the same order and errors as compare_key_mapping."""

    _validate_snapshot_direction(snapshot_a, snapshot_b)
    if not isinstance(key_pairs, Sequence) or isinstance(key_pairs, str | bytes | bytearray):
        raise TypeError("key_pairs must be a sequence of column-name pairs")
    selections = tuple(key_pairs)
    if not selections:
        raise ValueError("select one or more key column pairs")
    ordinals_a = {column.name: index for index, column in enumerate(snapshot_a.profile.columns)}
    ordinals_b = {column.name: index for index, column in enumerate(snapshot_b.profile.columns)}
    pairs = []
    indices_a = []
    indices_b = []
    seen_a = set()
    seen_b = set()
    for pair in selections:
        if not isinstance(pair, Sequence) or isinstance(pair, str | bytes | bytearray):
            raise TypeError("each key pair must be a sequence of column names")
        if len(pair) != 2:
            raise ValueError("each key pair must contain exactly two column names")
        name_a, name_b = pair
        index_a = ordinals_a.get(name_a) if isinstance(name_a, str) else None
        if index_a is None:
            index_a = _column_index(snapshot_a, name_a)
        index_b = ordinals_b.get(name_b) if isinstance(name_b, str) else None
        if index_b is None:
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
    return tuple(pairs), tuple(indices_a), tuple(indices_b)


def _key_difference_signature_index(keys, prefixes, suffixes):
    """Index exact omitted-position signatures using bounded prefix/suffix tries.

    Intern IDs are shared across both files. Each transition includes the exact
    component string, so equality does not rely on a digest or approximate hash.
    Signatures retain three integers rather than a copy of every remaining key
    component. All retained nodes and entries are linear in the signature budget.
    """

    index = {}
    for key in keys:
        before = []
        current = 0
        for value in key:
            before.append(current)
            transition = (current, value)
            if transition not in prefixes:
                prefixes[transition] = len(prefixes) + 1
            current = prefixes[transition]
        after = [0] * len(key)
        current = 0
        for component in range(len(key) - 1, -1, -1):
            after[component] = current
            transition = (current, key[component])
            if transition not in suffixes:
                suffixes[transition] = len(suffixes) + 1
            current = suffixes[transition]
        for component in range(len(key)):
            signature = (component, before[component], after[component])
            index.setdefault(signature, []).append(key)
    return index


def find_possible_key_differences(
    snapshot_a: DiscoverySnapshot,
    snapshot_b: DiscoverySnapshot,
    *,
    key_pairs: Sequence[tuple[str, str]],
) -> PossibleKeyDifferenceResult:
    """Find conservative one-component differences in unmatched baseline keys.

    Only occurrence-one keys can be displayed. All unmatched distinct keys,
    including repeated keys, remain ambiguity evidence. A cross-file signature
    with multiple distinct keys on either side suppresses every participating
    endpoint. Multiple partners through different omitted components also
    suppress an endpoint, even if one of those relationships was already
    ambiguous or involved a repeated key.

    Row limits precede key counting. The exact unmatched-distinct-key count times
    cardinality bounds signature construction before either index is built.
    Results sort by zero-based component index and complete original tuples;
    only display is truncated, never the qualifying count. Nothing is applied.
    """

    pairs, indices_a, indices_b = _difference_key_selection(snapshot_a, snapshot_b, key_pairs)

    def skipped(reason, entries=None):
        return PossibleKeyDifferenceResult(pairs, (), 0, False, False, reason, False, entries)

    if len(pairs) < 2:
        return skipped("requires_composite_key")
    if (
        snapshot_a.profile.row_count > MAX_KEY_DIFFERENCE_ROWS
        or snapshot_b.profile.row_count > MAX_KEY_DIFFERENCE_ROWS
    ):
        return skipped("source_row_limit")
    counts_a = _key_counts(snapshot_a, indices_a)
    counts_b = _key_counts(snapshot_b, indices_b)
    unmatched_a = tuple(key for key in counts_a if key not in counts_b)
    unmatched_b = tuple(key for key in counts_b if key not in counts_a)
    entries = (len(unmatched_a) + len(unmatched_b)) * len(pairs)
    if entries > MAX_KEY_DIFFERENCE_SIGNATURE_ENTRIES:
        return skipped("signature_entry_limit", entries)
    prefixes = {}
    suffixes = {}
    index_a = _key_difference_signature_index(unmatched_a, prefixes, suffixes)
    index_b = _key_difference_signature_index(unmatched_b, prefixes, suffixes)
    ambiguous_a = set()
    ambiguous_b = set()
    partners_a = {}
    partners_b = {}
    provisional = []
    for signature, keys_a in index_a.items():
        keys_b = index_b.get(signature)
        if keys_b is None:
            continue
        if len(keys_a) != 1 or len(keys_b) != 1:
            ambiguous_a.update(keys_a)
            ambiguous_b.update(keys_b)
            continue
        key_a, key_b = keys_a[0], keys_b[0]
        partners_a.setdefault(key_a, set()).add(key_b)
        partners_b.setdefault(key_b, set()).add(key_a)
        provisional.append((signature[0], key_a, key_b))
    ambiguous_a.update(key for key, partners in partners_a.items() if len(partners) > 1)
    ambiguous_b.update(key for key, partners in partners_b.items() if len(partners) > 1)
    qualifying = sorted(
        (component, key_a, key_b)
        for component, key_a, key_b in provisional
        if key_a not in ambiguous_a
        and key_b not in ambiguous_b
        and counts_a[key_a] == 1
        and counts_b[key_b] == 1
    )
    candidates = tuple(
        PossibleKeyDifference(key_a, key_b, component, *pairs[component])
        for component, key_a, key_b in qualifying[:MAX_KEY_DIFFERENCE_CANDIDATES]
    )
    return PossibleKeyDifferenceResult(
        pairs,
        candidates,
        len(qualifying),
        len(qualifying) > MAX_KEY_DIFFERENCE_CANDIDATES,
        True,
        None,
        bool(ambiguous_a or ambiguous_b),
        entries,
    )
