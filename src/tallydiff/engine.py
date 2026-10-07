"""Deterministic reconciliation logic for already-parsed records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal

from tallydiff.models import (
    CompositeKey,
    FindingCategory,
    ReconciliationFinding,
    ReconciliationResult,
    Source,
    SourceRecord,
)


class ReconciliationIntegrityError(RuntimeError):
    """Raised when an internal reconciliation invariant is violated."""


def reconcile(
    records_a: Sequence[SourceRecord],
    records_b: Sequence[SourceRecord],
) -> ReconciliationResult:
    """Reconcile two sets of validated records by their normalized exact keys.

    Duplicate keys are never paired heuristically. If either side contains more
    than one row for a key, every row for that key is retained in a single
    ``DUPLICATE_AMBIGUOUS`` finding.
    """

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
        category = _classify(rows_a, rows_b, amount_a, amount_b)

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
                f"File {expected_source.value} contains duplicate source row "
                f"{record.source_row}"
            )
        seen_rows.add(record.source_row)


def _group_by_key(
    records: Iterable[SourceRecord],
) -> dict[CompositeKey, list[SourceRecord]]:
    grouped: dict[CompositeKey, list[SourceRecord]] = defaultdict(list)
    for record in records:
        grouped[record.key].append(record)
    return dict(grouped)


def _sum_amounts(records: Iterable[SourceRecord]) -> Decimal:
    return sum((record.amount for record in records), Decimal("0"))


def _classify(
    rows_a: tuple[SourceRecord, ...],
    rows_b: tuple[SourceRecord, ...],
    amount_a: Decimal,
    amount_b: Decimal,
) -> FindingCategory:
    if len(rows_a) > 1 or len(rows_b) > 1:
        return FindingCategory.DUPLICATE_AMBIGUOUS
    if rows_a and not rows_b:
        return FindingCategory.A_ONLY
    if rows_b and not rows_a:
        return FindingCategory.B_ONLY
    if amount_a == amount_b:
        return FindingCategory.EXACT_MATCH
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

    expected_rows = {
        (record.source, record.source_row) for record in (*records_a, *records_b)
    }
    accounted_rows = {
        (record.source, record.source_row)
        for finding in result.findings
        for record in (*finding.rows_a, *finding.rows_b)
    }
    if expected_rows != accounted_rows:
        raise ReconciliationIntegrityError("not every source row was accounted for exactly once")
