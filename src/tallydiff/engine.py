"""Deterministic reconciliation logic for already-parsed records."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal

from tallydiff._decimal import sum_decimals
from tallydiff.models import (
    CompositeKey,
    FindingCategory,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
)


class ReconciliationIntegrityError(RuntimeError):
    """Raised when an internal reconciliation invariant is violated."""


def reconcile(
    records_a: Iterable[SourceRecord],
    records_b: Iterable[SourceRecord],
    *,
    amount_tolerance: Decimal = Decimal("0"),
    mode: ReconciliationMode = ReconciliationMode.UNIQUE,
) -> ReconciliationResult:
    """Reconcile validated records by exact keys, ordered by key and source row.

    Inputs are snapshotted once so validation cannot consume one-pass iterables.
    ``amount_tolerance`` must be a finite, nonnegative Decimal. Two-sided keys
    with a nonzero absolute delta at or below it are accepted as within tolerance
    when eligible under the mode; exact equality stays exact. All deltas retain
    their true values. ``mode`` must be a ReconciliationMode enum member.

    In UNIQUE mode, multiple rows on either side always produce a single
    ``DUPLICATE_AMBIGUOUS`` finding, regardless of tolerance or absent rows.
    GROUPED_BY_KEY compares the exact totals of all rows for each key, with
    presence taking precedence over amounts. Every source row is retained in
    both modes. Grouped results make no claim that individual rows correspond;
    rows are never paired heuristically.
    """

    if not isinstance(amount_tolerance, Decimal):
        raise TypeError("amount_tolerance must be a Decimal")
    if not amount_tolerance.is_finite():
        raise ValueError("amount_tolerance must be finite")
    if amount_tolerance < 0:
        raise ValueError("amount_tolerance must be nonnegative")
    if not isinstance(mode, ReconciliationMode):
        raise TypeError("mode must be a ReconciliationMode enum member")

    records_a = tuple(records_a)
    records_b = tuple(records_b)
    _validate_source_records(records_a, Source.A)
    _validate_source_records(records_b, Source.B)

    grouped_a = _group_by_key(records_a)
    grouped_b = _group_by_key(records_b)

    findings: list[ReconciliationFinding] = []
    for key in sorted(grouped_a.keys() | grouped_b.keys()):
        rows_a = tuple(grouped_a.get(key, ()))
        rows_b = tuple(grouped_b.get(key, ()))
        amount_a = _sum_amounts(rows_a)
        amount_b = _sum_amounts(rows_b)
        category = _classify(rows_a, rows_b, amount_a, amount_b, amount_tolerance, mode)

        findings.append(
            ReconciliationFinding(
                category=category,
                key=key,
                rows_a=rows_a,
                rows_b=rows_b,
                amount_a=amount_a,
                amount_b=amount_b,
            )
        )

    result = ReconciliationResult(
        total_a=_sum_amounts(records_a),
        total_b=_sum_amounts(records_b),
        findings=tuple(findings),
        amount_tolerance=amount_tolerance,
        mode=mode,
    )
    _assert_integrity(result, records_a, records_b)
    return result


def _validate_source_records(records: Sequence[SourceRecord], expected_source: Source) -> None:
    seen_rows: set[int] = set()
    for record in records:
        if record.source is not expected_source:
            raise ValueError(
                f"record at source row {record.source_row} belongs to File {record.source.value}, "
                f"not File {expected_source.value}"
            )
        if record.source_row in seen_rows:
            raise ValueError(
                f"File {expected_source.value} contains duplicate source row {record.source_row}"
            )
        seen_rows.add(record.source_row)


def _group_by_key(
    records: Iterable[SourceRecord],
) -> dict[CompositeKey, list[SourceRecord]]:
    grouped: dict[CompositeKey, list[SourceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.key].append(record)
    for rows in grouped.values():
        rows.sort(key=lambda record: record.source_row)
    return dict(grouped)


def _sum_amounts(records: Iterable[SourceRecord]) -> Decimal:
    return sum_decimals(record.amount for record in records)


def _classify(
    rows_a: tuple[SourceRecord, ...],
    rows_b: tuple[SourceRecord, ...],
    amount_a: Decimal,
    amount_b: Decimal,
    amount_tolerance: Decimal,
    mode: ReconciliationMode,
) -> FindingCategory:
    if mode is ReconciliationMode.UNIQUE and (len(rows_a) > 1 or len(rows_b) > 1):
        return FindingCategory.DUPLICATE_AMBIGUOUS
    if rows_a and not rows_b:
        return FindingCategory.A_ONLY
    if rows_b and not rows_a:
        return FindingCategory.B_ONLY
    if amount_a == amount_b:
        return FindingCategory.EXACT_MATCH
    delta = sum_decimals((amount_a, amount_b.copy_negate()))
    if delta.copy_abs() <= amount_tolerance:
        return FindingCategory.WITHIN_TOLERANCE
    return FindingCategory.AMOUNT_MISMATCH


def _assert_integrity(
    result: ReconciliationResult,
    records_a: Sequence[SourceRecord],
    records_b: Sequence[SourceRecord],
) -> None:
    if result.control_difference != result.finding_delta_sum:
        raise ReconciliationIntegrityError(
            "finding deltas do not explain the control-total difference"
        )

    expected_rows = Counter(
        (record.source, record.source_row) for record in (*records_a, *records_b)
    )
    accounted_rows = Counter(
        (record.source, record.source_row)
        for finding in result.findings
        for record in (*finding.rows_a, *finding.rows_b)
    )
    if expected_rows != accounted_rows:
        raise ReconciliationIntegrityError("not every source row was accounted for exactly once")
