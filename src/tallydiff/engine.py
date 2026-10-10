"""Deterministic reconciliation logic for already-parsed records."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal

from tallydiff._decimal import sum_decimals
from tallydiff.configuration import ComparisonFieldMapping
from tallydiff.models import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    CompositeKey,
    CorrespondenceAnalysis,
    CorrespondenceReason,
    CorrespondenceStatus,
    FieldComparison,
    FieldComparisonStatus,
    FindingCategory,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
)
from tallydiff.normalization import (
    NormalizationCollision,
    find_normalization_collisions,
    normalize_key,
)
from tallydiff.normalization_config import KeyNormalizationConfig
from tallydiff.subset_matching import analyze_bounded_one_to_many


class ReconciliationIntegrityError(RuntimeError):
    """Raised when an internal reconciliation invariant is violated."""


class NormalizationCollisionError(ValueError):
    """Reconciliation is blocked; both sources retain their full collision evidence."""

    def __init__(
        self,
        collisions_a: tuple[NormalizationCollision, ...],
        collisions_b: tuple[NormalizationCollision, ...],
    ) -> None:
        self.collisions_a = tuple(collisions_a)
        self.collisions_b = tuple(collisions_b)
        super().__init__(
            "Normalization collisions block reconciliation "
            f"(File A: {len(self.collisions_a)}, File B: {len(self.collisions_b)})."
        )


def reconcile(
    records_a: Iterable[SourceRecord],
    records_b: Iterable[SourceRecord],
    *,
    amount_tolerance: Decimal = Decimal("0"),
    mode: ReconciliationMode = ReconciliationMode.UNIQUE,
    key_normalization: KeyNormalizationConfig | None = None,
    comparison_fields: tuple[ComparisonFieldMapping, ...] = (),
) -> ReconciliationResult:
    """Reconcile validated records by matching keys, ordered by key and source row.

    Inputs are snapshotted once so validation cannot consume one-pass iterables.
    None for key_normalization preserves exact matching. An explicit configuration
    applies explicit rules to matching keys only; findings retain original records.
    Source integrity and all key arities are validated before normalization.
    Blank components fail with source/row/component context. Same-source collisions
    in either input block all modes, retaining evidence for both files in a
    NormalizationCollisionError before any findings or result are constructed.
    ``amount_tolerance`` must be a finite, nonnegative Decimal. Two-sided keys
    with a nonzero absolute delta at or below it are accepted as within tolerance
    when eligible under the mode; exact equality stays exact. All deltas retain
    their true values. ``mode`` must be a ReconciliationMode enum member.

    In UNIQUE mode, multiple rows on either side always produce a single
    ``DUPLICATE_AMBIGUOUS`` finding, regardless of tolerance or absent rows.
    GROUPED_BY_KEY compares the exact totals of all rows for each key, with
    presence taking precedence over amounts. Every source row is retained in
    all modes. Grouped results make no claim that individual rows correspond;
    rows are never paired heuristically.

    BOUNDED_ONE_TO_MANY uses the fixed canonical policy to find a physically unique
    exact subset against a sole anchor in either direction. Accepted inferences
    require review. Residual rows stay in the parent finding and always produce
    AMOUNT_MISMATCH; tolerance applies only to ordinary one-row-per-side groups.
    Every unresolved duplicate-shaped group remains DUPLICATE_AMBIGUOUS.

    Secondary fields compare distinct sets of original evidence strings, in
    configuration order. One-sided groups, UNIQUE duplicates, and all bounded
    correspondence analyses retain their summaries as NOT_COMPARABLE.
    Secondary status does not change primary categories or financial deltas.
    Any secondary MISMATCH additionally requires review.
    """

    if not isinstance(amount_tolerance, Decimal):
        raise TypeError("amount_tolerance must be a Decimal")
    if not amount_tolerance.is_finite():
        raise ValueError("amount_tolerance must be finite")
    if amount_tolerance < 0:
        raise ValueError("amount_tolerance must be nonnegative")
    if not isinstance(mode, ReconciliationMode):
        raise TypeError("mode must be a ReconciliationMode enum member")
    if key_normalization is not None and not isinstance(key_normalization, KeyNormalizationConfig):
        raise TypeError("key_normalization must be a KeyNormalizationConfig or None")
    _validate_comparison_fields(comparison_fields)

    records_a = tuple(records_a)
    records_b = tuple(records_b)
    _validate_source_records(records_a, Source.A)
    _validate_source_records(records_b, Source.B)

    if key_normalization is not None:
        for source, source_records in ((Source.A, records_a), (Source.B, records_b)):
            for record in source_records:
                if len(record.key) != len(key_normalization.component_rules):
                    raise ValueError(
                        f"File {source.value} source row {record.source_row}: "
                        "configuration length must equal key length"
                    )
        collisions_a = find_normalization_collisions(records_a, key_normalization, source=Source.A)
        collisions_b = find_normalization_collisions(records_b, key_normalization, source=Source.B)
        if collisions_a or collisions_b:
            raise NormalizationCollisionError(collisions_a, collisions_b)

    if comparison_fields:
        _validate_comparison_evidence(records_a, Source.A, comparison_fields)
        _validate_comparison_evidence(records_b, Source.B, comparison_fields)

    grouped_a = _group_by_key(records_a, key_normalization=key_normalization)
    grouped_b = _group_by_key(records_b, key_normalization=key_normalization)

    policy = (
        EXACT_UNIQUE_ONE_TO_MANY_POLICY if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY else None
    )
    remaining_planned_budget = policy.max_planned_combinations_per_run if policy else 0
    run_budget_exhausted = False
    findings: list[ReconciliationFinding] = []
    for key in sorted(grouped_a.keys() | grouped_b.keys()):
        rows_a = tuple(grouped_a.get(key, ()))
        rows_b = tuple(grouped_b.get(key, ()))
        amount_a = _sum_amounts(rows_a)
        amount_b = _sum_amounts(rows_b)
        analysis = None
        if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
            assert policy is not None
            analysis = analyze_bounded_one_to_many(
                rows_a,
                rows_b,
                policy=policy,
                remaining_planned_budget=remaining_planned_budget,
                run_budget_exhausted=run_budget_exhausted,
            )
            if analysis is not None:
                if analysis.reason is CorrespondenceReason.RUN_BUDGET_EXHAUSTED:
                    run_budget_exhausted = True
                else:
                    remaining_planned_budget -= analysis.reserved_combinations
                if remaining_planned_budget < 0:
                    raise ReconciliationIntegrityError("remaining planned budget became negative")
        category = _classify(
            rows_a, rows_b, amount_a, amount_b, amount_tolerance, mode, analysis=analysis
        )
        comparable = (
            analysis is None
            and bool(rows_a and rows_b)
            and (
                mode is ReconciliationMode.GROUPED_BY_KEY or (len(rows_a) == 1 and len(rows_b) == 1)
            )
        )

        findings.append(
            ReconciliationFinding(
                category=category,
                key=key,
                rows_a=rows_a,
                rows_b=rows_b,
                amount_a=amount_a,
                amount_b=amount_b,
                field_comparisons=_compare_fields(
                    rows_a, rows_b, comparison_fields, comparable=comparable
                ),
                correspondence_analysis=analysis,
            )
        )

    result = ReconciliationResult(
        total_a=_sum_amounts(records_a),
        total_b=_sum_amounts(records_b),
        findings=tuple(findings),
        amount_tolerance=amount_tolerance,
        mode=mode,
        key_normalization=key_normalization,
        comparison_fields=comparison_fields,
        one_to_many_policy=policy,
    )
    _assert_integrity(result, records_a, records_b)
    return result


def _validate_comparison_fields(comparison_fields: tuple[ComparisonFieldMapping, ...]) -> None:
    if not isinstance(comparison_fields, tuple) or any(
        not isinstance(mapping, ComparisonFieldMapping) for mapping in comparison_fields
    ):
        raise TypeError("comparison_fields must be a tuple of ComparisonFieldMapping objects")
    if any(not mapping.is_complete for mapping in comparison_fields):
        raise ValueError("comparison_fields must select nonblank columns for both files")
    for side, names in (
        ("A", tuple(mapping.file_a for mapping in comparison_fields)),
        ("B", tuple(mapping.file_b for mapping in comparison_fields)),
    ):
        if len(set(names)) != len(names):
            raise ValueError(f"each File {side} comparison column must be selected only once")


def _validate_comparison_evidence(
    records: Sequence[SourceRecord],
    source: Source,
    comparison_fields: tuple[ComparisonFieldMapping, ...],
) -> None:
    columns = tuple(
        mapping.file_a if source is Source.A else mapping.file_b for mapping in comparison_fields
    )
    for record in records:
        for column in columns:
            if column not in record.raw_fields:
                raise ValueError(
                    f"File {source.value} source row {record.source_row}: "
                    f"comparison column {column!r} is missing from raw_fields"
                )


def _compare_fields(
    rows_a: tuple[SourceRecord, ...],
    rows_b: tuple[SourceRecord, ...],
    comparison_fields: tuple[ComparisonFieldMapping, ...],
    *,
    comparable: bool,
) -> tuple[FieldComparison, ...]:
    if not comparison_fields:
        return ()
    comparisons = []
    for mapping in comparison_fields:
        values_a = tuple(sorted({record.raw_fields[mapping.file_a] for record in rows_a}))
        values_b = tuple(sorted({record.raw_fields[mapping.file_b] for record in rows_b}))
        if not comparable:
            status = FieldComparisonStatus.NOT_COMPARABLE
        elif values_a == values_b:
            status = FieldComparisonStatus.MATCH
        else:
            status = FieldComparisonStatus.MISMATCH
        comparisons.append(FieldComparison(mapping, values_a, values_b, status))
    return tuple(comparisons)


def _validate_source_records(records: Sequence[SourceRecord], expected_source: Source) -> None:
    seen_rows: set[int] = set()
    for record in records:
        if not isinstance(record, SourceRecord):
            raise TypeError("records must contain only SourceRecord objects")
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
    *,
    key_normalization: KeyNormalizationConfig | None = None,
) -> dict[CompositeKey, list[SourceRecord]]:
    grouped: dict[CompositeKey, list[SourceRecord]] = defaultdict(list)
    for record in records:
        key = (
            record.key
            if key_normalization is None
            else normalize_key(record.key, key_normalization)
        )
        grouped[key].append(record)
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
    *,
    analysis: CorrespondenceAnalysis | None = None,
) -> FindingCategory:
    if mode is ReconciliationMode.UNIQUE:
        if len(rows_a) > 1 or len(rows_b) > 1:
            return FindingCategory.DUPLICATE_AMBIGUOUS
        return _classify_amounts(rows_a, rows_b, amount_a, amount_b, amount_tolerance)
    if mode is ReconciliationMode.GROUPED_BY_KEY:
        return _classify_amounts(rows_a, rows_b, amount_a, amount_b, amount_tolerance)
    if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        if analysis is None:
            if len(rows_a) > 1 or len(rows_b) > 1:
                raise ReconciliationIntegrityError("bounded duplicate group requires analysis")
            return _classify_amounts(rows_a, rows_b, amount_a, amount_b, amount_tolerance)
        if analysis.status is CorrespondenceStatus.UNIQUE_EXACT:
            return (
                FindingCategory.AMOUNT_MISMATCH
                if analysis.unassigned_rows_a or analysis.unassigned_rows_b
                else FindingCategory.EXACT_MATCH
            )
        if analysis.status in (
            CorrespondenceStatus.AMBIGUOUS,
            CorrespondenceStatus.NO_EXACT_SUBSET,
            CorrespondenceStatus.SINGLETON_ONLY,
            CorrespondenceStatus.BOUND_EXCEEDED,
            CorrespondenceStatus.NOT_ELIGIBLE,
        ):
            return FindingCategory.DUPLICATE_AMBIGUOUS
        raise ReconciliationIntegrityError("unsupported bounded correspondence status")
    raise ReconciliationIntegrityError("unsupported reconciliation mode")


def _classify_amounts(
    rows_a: Sequence[SourceRecord],
    rows_b: Sequence[SourceRecord],
    amount_a: Decimal,
    amount_b: Decimal,
    amount_tolerance: Decimal,
) -> FindingCategory:
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

    review_and_tolerance_delta = sum_decimals(
        (sum_decimals(finding.delta for finding in result.exceptions), result.tolerated_delta_total)
    )
    if result.control_difference != review_and_tolerance_delta:
        raise ReconciliationIntegrityError(
            "review exceptions and tolerated deltas do not explain the control-total difference"
        )
    for finding in result.findings:
        needs_analysis = result.mode is ReconciliationMode.BOUNDED_ONE_TO_MANY and (
            len(finding.rows_a) > 1 or len(finding.rows_b) > 1
        )
        if (finding.correspondence_analysis is not None) != needs_analysis:
            raise ReconciliationIntegrityError(
                "correspondence analysis must appear exactly on bounded duplicate-shaped findings"
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
