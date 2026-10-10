"""Core data models for TallyDiff's reconciliation engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType

from tallydiff._decimal import sum_decimals
from tallydiff.configuration import ComparisonFieldMapping
from tallydiff.normalization_config import KeyNormalizationConfig

type CompositeKey = tuple[str, ...]


class Source(StrEnum):
    """Identifies which reconciliation input a record came from."""

    A = "A"
    B = "B"


class ReconciliationMode(StrEnum):
    """Compare unique rows, whole-key totals, or bounded exact one-to-many subsets."""

    UNIQUE = "unique"
    GROUPED_BY_KEY = "grouped_by_key"
    BOUNDED_ONE_TO_MANY = "bounded_one_to_many"


class FindingCategory(StrEnum):
    """Mutually exclusive outcome for one reconciliation key."""

    EXACT_MATCH = "exact_match"
    WITHIN_TOLERANCE = "within_tolerance"
    AMOUNT_MISMATCH = "amount_mismatch"
    A_ONLY = "a_only"
    B_ONLY = "b_only"
    DUPLICATE_AMBIGUOUS = "duplicate_ambiguous"


@dataclass(frozen=True, slots=True)
class OneToManyPolicy:
    """Immutable semantic and numerical snapshot, independent of engine enablement."""

    policy_id: str
    max_candidate_rows: int
    minimum_accepted_subset_rows: int
    max_planned_combinations_per_run: int
    max_ambiguity_witnesses: int
    exact_amounts_only: bool
    physical_row_uniqueness: bool
    singleton_rivals_count: bool

    def __post_init__(self) -> None:
        if type(self.policy_id) is not str:
            raise TypeError("policy_id must be a string")
        if not self.policy_id.strip():
            raise ValueError("policy_id must be nonblank")
        for name in (
            "max_candidate_rows",
            "minimum_accepted_subset_rows",
            "max_planned_combinations_per_run",
            "max_ambiguity_witnesses",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.minimum_accepted_subset_rows < 2:
            raise ValueError("minimum_accepted_subset_rows must be at least 2")
        if self.minimum_accepted_subset_rows > self.max_candidate_rows:
            raise ValueError("minimum_accepted_subset_rows must not exceed max_candidate_rows")
        if self.max_ambiguity_witnesses < 2:
            raise ValueError("max_ambiguity_witnesses must be at least 2")
        for name in ("exact_amounts_only", "physical_row_uniqueness", "singleton_rivals_count"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")

    @property
    def max_total_rows(self) -> int:
        return self.max_candidate_rows + 1

    @property
    def max_combinations_per_key(self) -> int:
        return (1 << self.max_candidate_rows) - 1


EXACT_UNIQUE_ONE_TO_MANY_POLICY = OneToManyPolicy(
    policy_id="exact_unique_v1",
    max_candidate_rows=12,
    minimum_accepted_subset_rows=2,
    max_planned_combinations_per_run=1_000_000,
    max_ambiguity_witnesses=2,
    exact_amounts_only=True,
    physical_row_uniqueness=True,
    singleton_rivals_count=True,
)


class CorrespondenceStatus(StrEnum):
    """Bounded analysis outcome, independent of the primary financial category."""

    UNIQUE_EXACT = "unique_exact"
    AMBIGUOUS = "ambiguous"
    NO_EXACT_SUBSET = "no_exact_subset"
    SINGLETON_ONLY = "singleton_only"
    BOUND_EXCEEDED = "bound_exceeded"
    NOT_ELIGIBLE = "not_eligible"


class CorrespondenceReason(StrEnum):
    """Structural or resource reason why correspondence was not searched."""

    BOTH_SIDES_MULTIPLE = "both_sides_multiple"
    MISSING_OPPOSITE_SIDE = "missing_opposite_side"
    CANDIDATE_ROW_LIMIT = "candidate_row_limit"
    RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"


class FieldComparisonStatus(StrEnum):
    """Secondary field agreement, independent of the primary finding category."""

    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """Sorted distinct ingested strings; a present blank is ("",), not ().

    Source records remain on the finding. NOT_COMPARABLE permits either equal
    or unequal summaries. Group structure determines comparability; even an inferred
    EXACT_MATCH retains NOT_COMPARABLE secondary evidence.
    """

    mapping: ComparisonFieldMapping
    values_a: tuple[str, ...]
    values_b: tuple[str, ...]
    status: FieldComparisonStatus

    def __post_init__(self) -> None:
        if not isinstance(self.mapping, ComparisonFieldMapping):
            raise TypeError("mapping must be a ComparisonFieldMapping")
        if not self.mapping.is_complete:
            raise ValueError("mapping must select nonblank columns for both files")
        for name in ("values_a", "values_b"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
                raise TypeError(f"{name} must be a tuple of strings")
            if any(left >= right for left, right in pairwise(values)):
                raise ValueError(f"{name} must be sorted and duplicate-free")
        if not isinstance(self.status, FieldComparisonStatus):
            raise TypeError("status must be a FieldComparisonStatus enum member")
        if self.status is FieldComparisonStatus.MATCH and self.values_a != self.values_b:
            raise ValueError("MATCH requires equal value tuples")
        if self.status is FieldComparisonStatus.MISMATCH and self.values_a == self.values_b:
            raise ValueError("MISMATCH requires unequal value tuples")


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One validated source row with an exact key and a finite Decimal amount.

    Keys are not normalized here. ``raw_fields`` is copied into a read-only
    mapping so the source evidence cannot change after construction.
    """

    source: Source
    source_row: int
    key: CompositeKey
    amount: Decimal
    raw_fields: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, Source):
            raise TypeError("source must be a Source enum member")
        if not isinstance(self.source_row, int) or isinstance(self.source_row, bool):
            raise TypeError("source_row must be an integer")
        if self.source_row < 1:
            raise ValueError("source_row must be a positive integer")
        if not isinstance(self.key, tuple) or any(
            not isinstance(component, str) for component in self.key
        ):
            raise TypeError("key must be a tuple of strings")
        if not self.key:
            raise ValueError("key must contain at least one component")
        if any(not component.strip() for component in self.key):
            raise ValueError("key components must not be blank")
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        if not self.amount.is_finite():
            raise ValueError("amount must be finite")
        if not isinstance(self.raw_fields, Mapping) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in self.raw_fields.items()
        ):
            raise TypeError("raw_fields must be a mapping of strings to strings")
        object.__setattr__(self, "raw_fields", MappingProxyType(dict(self.raw_fields)))


def _validate_correspondence_rows(
    rows: tuple[SourceRecord, ...], source: Source, name: str
) -> None:
    if not isinstance(rows, tuple) or any(not isinstance(row, SourceRecord) for row in rows):
        raise TypeError(f"{name} must be a tuple of SourceRecord objects")
    if any(row.source is not source for row in rows):
        raise ValueError(f"{name} must contain only File {source.value} records")
    if any(left.source_row >= right.source_row for left, right in pairwise(rows)):
        raise ValueError(f"{name} must be ordered by source_row without duplicates")


@dataclass(frozen=True, slots=True)
class SubsetSolution:
    """One exact solution retaining original rows, including singleton rivals.

    A one-row-per-side witness has no intrinsic anchor direction. The derived
    accessors use File A for that tie; the parent group determines search direction.
    """

    rows_a: tuple[SourceRecord, ...]
    rows_b: tuple[SourceRecord, ...]
    amount: Decimal

    def __post_init__(self) -> None:
        _validate_correspondence_rows(self.rows_a, Source.A, "rows_a")
        _validate_correspondence_rows(self.rows_b, Source.B, "rows_b")
        if not self.rows_a or not self.rows_b:
            raise ValueError("a solution must have records on both sides")
        if len(self.rows_a) != 1 and len(self.rows_b) != 1:
            raise ValueError("a solution must have one anchor row on at least one side")
        if not isinstance(self.amount, Decimal):
            raise TypeError("amount must be a Decimal")
        if not self.amount.is_finite():
            raise ValueError("amount must be finite")
        for name in ("rows_a", "rows_b"):
            if sum_decimals(row.amount for row in getattr(self, name)) != self.amount:
                raise ValueError(f"{name} amounts must sum exactly to amount")

    @property
    def anchor(self) -> SourceRecord:
        return self.rows_a[0] if len(self.rows_a) == 1 else self.rows_b[0]

    @property
    def anchor_source(self) -> Source:
        return self.anchor.source

    @property
    def candidate_rows(self) -> tuple[SourceRecord, ...]:
        return self.rows_b if self.anchor_source is Source.A else self.rows_a

    @property
    def candidate_source(self) -> Source:
        return Source.B if self.anchor_source is Source.A else Source.A

    @property
    def candidate_row_count(self) -> int:
        return len(self.candidate_rows)

    @property
    def is_multirow_solution(self) -> bool:
        return self.candidate_row_count > 1


@dataclass(frozen=True, slots=True)
class CorrespondenceAnalysis:
    """Stored analysis evidence; parent membership is validated by its finding.

    Witnesses are ordered by cardinality and source-row tuples. Their references
    are alternatives, never accounting allocations. Planned budget can be fully
    reserved even when two witnesses end examination early.
    """

    policy: OneToManyPolicy
    status: CorrespondenceStatus
    accepted_solution: SubsetSolution | None
    unassigned_rows_a: tuple[SourceRecord, ...]
    unassigned_rows_b: tuple[SourceRecord, ...]
    ambiguity_witnesses: tuple[SubsetSolution, ...]
    planned_combinations: int
    examined_combinations: int
    reserved_combinations: int
    search_complete: bool
    reason: CorrespondenceReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.policy, OneToManyPolicy):
            raise TypeError("policy must be a OneToManyPolicy")
        if not isinstance(self.status, CorrespondenceStatus):
            raise TypeError("status must be a CorrespondenceStatus enum member")
        if self.reason is not None and not isinstance(self.reason, CorrespondenceReason):
            raise TypeError("reason must be a CorrespondenceReason enum member or None")
        if self.accepted_solution is not None and not isinstance(
            self.accepted_solution, SubsetSolution
        ):
            raise TypeError("accepted_solution must be a SubsetSolution or None")
        _validate_correspondence_rows(self.unassigned_rows_a, Source.A, "unassigned_rows_a")
        _validate_correspondence_rows(self.unassigned_rows_b, Source.B, "unassigned_rows_b")
        if not isinstance(self.ambiguity_witnesses, tuple) or any(
            not isinstance(witness, SubsetSolution) for witness in self.ambiguity_witnesses
        ):
            raise TypeError("ambiguity_witnesses must be a tuple of SubsetSolution objects")
        witness_order = tuple(
            (
                len(witness.rows_a) + len(witness.rows_b),
                tuple(row.source_row for row in witness.rows_a),
                tuple(row.source_row for row in witness.rows_b),
            )
            for witness in self.ambiguity_witnesses
        )
        if any(left >= right for left, right in pairwise(witness_order)):
            raise ValueError("ambiguity_witnesses must be ordered and physically distinct")
        for name in ("planned_combinations", "examined_combinations", "reserved_combinations"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.examined_combinations > self.planned_combinations:
            raise ValueError("examined_combinations must not exceed planned_combinations")
        if type(self.search_complete) is not bool:
            raise TypeError("search_complete must be a boolean")

        if self.status is CorrespondenceStatus.UNIQUE_EXACT:
            if self.accepted_solution is None or not self.accepted_solution.is_multirow_solution:
                raise ValueError("UNIQUE_EXACT requires an accepted multirow solution")
        elif self.accepted_solution is not None:
            raise ValueError("unresolved statuses must not contain an accepted solution")

        if self.status is CorrespondenceStatus.AMBIGUOUS:
            if not 2 <= len(self.ambiguity_witnesses) <= self.policy.max_ambiguity_witnesses:
                raise ValueError("AMBIGUOUS requires two or more witnesses within the policy limit")
        elif self.status is CorrespondenceStatus.SINGLETON_ONLY:
            if (
                len(self.ambiguity_witnesses) != 1
                or self.ambiguity_witnesses[0].is_multirow_solution
            ):
                raise ValueError("SINGLETON_ONLY requires exactly one singleton witness")
        elif self.ambiguity_witnesses:
            raise ValueError("this status must not contain ambiguity witnesses")

        if (
            self.status
            in (
                CorrespondenceStatus.UNIQUE_EXACT,
                CorrespondenceStatus.NO_EXACT_SUBSET,
                CorrespondenceStatus.SINGLETON_ONLY,
            )
            and not self.search_complete
        ):
            raise ValueError("this status requires a complete search")
        if self.status is CorrespondenceStatus.BOUND_EXCEEDED:
            reasons = (
                CorrespondenceReason.CANDIDATE_ROW_LIMIT,
                CorrespondenceReason.RUN_BUDGET_EXHAUSTED,
            )
        elif self.status is CorrespondenceStatus.NOT_ELIGIBLE:
            reasons = (
                CorrespondenceReason.BOTH_SIDES_MULTIPLE,
                CorrespondenceReason.MISSING_OPPOSITE_SIDE,
            )
        else:
            reasons = (None,)
        if self.reason not in reasons:
            raise ValueError("reason is incompatible with correspondence status")
        if (
            self.status
            in (
                CorrespondenceStatus.BOUND_EXCEEDED,
                CorrespondenceStatus.NOT_ELIGIBLE,
            )
            and self.search_complete
        ):
            raise ValueError("unsearched statuses must not have a complete search")


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """Outcome for the matching key; retained source rows keep their original keys."""

    category: FindingCategory
    key: CompositeKey
    rows_a: tuple[SourceRecord, ...]
    rows_b: tuple[SourceRecord, ...]
    amount_a: Decimal
    amount_b: Decimal
    field_comparisons: tuple[FieldComparison, ...] = field(default=(), kw_only=True)
    correspondence_analysis: CorrespondenceAnalysis | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.field_comparisons, tuple) or any(
            not isinstance(comparison, FieldComparison) for comparison in self.field_comparisons
        ):
            raise TypeError("field_comparisons must be a tuple of FieldComparison objects")
        if self.correspondence_analysis is not None:
            if not isinstance(self.correspondence_analysis, CorrespondenceAnalysis):
                raise TypeError("correspondence_analysis must be a CorrespondenceAnalysis or None")
            self._validate_correspondence_analysis()

    def _validate_correspondence_analysis(self) -> None:
        analysis = self.correspondence_analysis
        assert analysis is not None
        for source, parent_rows, unassigned, name in (
            (Source.A, self.rows_a, analysis.unassigned_rows_a, "rows_a"),
            (Source.B, self.rows_b, analysis.unassigned_rows_b, "rows_b"),
        ):
            _validate_correspondence_rows(parent_rows, source, name)
            parent_ids = {id(row) for row in parent_rows}
            solutions = (
                (analysis.accepted_solution,) if analysis.accepted_solution is not None else ()
            ) + analysis.ambiguity_witnesses
            referenced = (
                *unassigned,
                *(row for solution in solutions for row in getattr(solution, name)),
            )
            if any(id(row) not in parent_ids for row in referenced):
                raise ValueError("analysis rows must be original parent records by object identity")
            selected = (
                getattr(analysis.accepted_solution, name)
                if analysis.accepted_solution is not None
                else ()
            )
            partition_ids = tuple(id(row) for row in (*selected, *unassigned))
            if len(partition_ids) != len(set(partition_ids)) or set(partition_ids) != parent_ids:
                raise ValueError(
                    "selected and unassigned rows must partition parent rows exactly once"
                )
            amount = self.amount_a if source is Source.A else self.amount_b
            if not isinstance(amount, Decimal):
                raise TypeError(f"amount_{source.value.lower()} must be a Decimal")
            if not amount.is_finite() or amount != sum_decimals(row.amount for row in parent_rows):
                raise ValueError("finding amounts must equal the complete parent row totals")

        if analysis.accepted_solution is not None:
            solution = analysis.accepted_solution
            anchor_rows = self.rows_a if solution.anchor_source is Source.A else self.rows_b
            candidate_rows = self.rows_b if solution.anchor_source is Source.A else self.rows_a
            if len(anchor_rows) != 1:
                raise ValueError("the anchor must be the sole row on its complete parent side")
            if not (
                analysis.policy.minimum_accepted_subset_rows
                <= solution.candidate_row_count
                <= len(candidate_rows)
                <= analysis.policy.max_candidate_rows
            ):
                raise ValueError("accepted candidate cardinality must satisfy the policy limits")
            expected_category = (
                FindingCategory.AMOUNT_MISMATCH
                if analysis.unassigned_rows_a or analysis.unassigned_rows_b
                else FindingCategory.EXACT_MATCH
            )
        else:
            expected_category = FindingCategory.DUPLICATE_AMBIGUOUS
        if self.category is not expected_category:
            raise ValueError("finding category is incompatible with correspondence analysis")

    @property
    def has_inferred_correspondence(self) -> bool:
        """Whether an accepted inference is recorded, independently of review membership."""

        return self.correspondence_status is CorrespondenceStatus.UNIQUE_EXACT

    @property
    def inferred_solution(self) -> SubsetSolution | None:
        return (
            self.correspondence_analysis.accepted_solution
            if self.correspondence_analysis is not None
            else None
        )

    @property
    def correspondence_status(self) -> CorrespondenceStatus | None:
        return (
            self.correspondence_analysis.status
            if self.correspondence_analysis is not None
            else None
        )

    @property
    def has_secondary_mismatch(self) -> bool:
        """Whether any configured secondary field reports a mismatch."""

        return any(
            comparison.status is FieldComparisonStatus.MISMATCH
            for comparison in self.field_comparisons
        )

    @property
    def secondary_mismatches(self) -> tuple[FieldComparison, ...]:
        """The ordered subset of secondary comparisons reporting a mismatch."""

        return tuple(
            comparison
            for comparison in self.field_comparisons
            if comparison.status is FieldComparisonStatus.MISMATCH
        )

    @property
    def delta(self) -> Decimal:
        """Signed contribution to File A total minus File B total."""

        return sum_decimals((self.amount_a, self.amount_b.copy_negate()))

    @property
    def is_exception(self) -> bool:
        """Whether a financial exception, secondary mismatch, or inference needs review."""

        primary_exception = self.category not in (
            FindingCategory.EXACT_MATCH,
            FindingCategory.WITHIN_TOLERANCE,
        )
        return primary_exception or self.has_secondary_mismatch or self.has_inferred_correspondence


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Complete result retaining explicit normalization, or None for exact matching.

    Bounded one-to-many results retain the canonical policy, including empty results.
    Legacy modes contain neither a policy nor correspondence analysis.
    """

    total_a: Decimal
    total_b: Decimal
    findings: tuple[ReconciliationFinding, ...]
    amount_tolerance: Decimal = Decimal("0")
    mode: ReconciliationMode = ReconciliationMode.UNIQUE
    key_normalization: KeyNormalizationConfig | None = None
    comparison_fields: tuple[ComparisonFieldMapping, ...] = field(default=(), kw_only=True)
    one_to_many_policy: OneToManyPolicy | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.one_to_many_policy is not None and not isinstance(
            self.one_to_many_policy, OneToManyPolicy
        ):
            raise TypeError("one_to_many_policy must be a OneToManyPolicy or None")
        if not isinstance(self.mode, ReconciliationMode):
            raise TypeError("mode must be a ReconciliationMode enum member")
        if self.mode in (ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY):
            if self.one_to_many_policy is not None:
                raise ValueError("legacy modes require one_to_many_policy to be None")
            if any(finding.correspondence_analysis is not None for finding in self.findings):
                raise ValueError("correspondence analysis requires BOUNDED_ONE_TO_MANY mode")
        elif self.mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
            if self.one_to_many_policy != EXACT_UNIQUE_ONE_TO_MANY_POLICY:
                raise ValueError("BOUNDED_ONE_TO_MANY requires the canonical one_to_many_policy")
        if self.key_normalization is not None and not isinstance(
            self.key_normalization, KeyNormalizationConfig
        ):
            raise TypeError("key_normalization must be a KeyNormalizationConfig or None")
        if not isinstance(self.comparison_fields, tuple) or any(
            not isinstance(mapping, ComparisonFieldMapping) for mapping in self.comparison_fields
        ):
            raise TypeError("comparison_fields must be a tuple of ComparisonFieldMapping objects")
        if any(not mapping.is_complete for mapping in self.comparison_fields):
            raise ValueError("comparison_fields must select nonblank columns for both files")
        for side, names in (
            ("A", tuple(mapping.file_a for mapping in self.comparison_fields)),
            ("B", tuple(mapping.file_b for mapping in self.comparison_fields)),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"each File {side} comparison column must be selected only once")
        for finding in self.findings:
            if finding.correspondence_analysis is not None:
                if self.one_to_many_policy is None:
                    raise ValueError("one_to_many_policy is required for correspondence analysis")
                if finding.correspondence_analysis.policy != self.one_to_many_policy:
                    raise ValueError("finding analysis policy must equal one_to_many_policy")
            if (
                tuple(comparison.mapping for comparison in finding.field_comparisons)
                != self.comparison_fields
            ):
                raise ValueError(
                    "finding field_comparisons must match comparison_fields in count and order"
                )

    @property
    def correspondence_analysis_findings(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.correspondence_analysis is not None
        )

    @property
    def inferred_correspondence_findings(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(finding for finding in self.findings if finding.has_inferred_correspondence)

    @property
    def control_difference(self) -> Decimal:
        return sum_decimals((self.total_a, self.total_b.copy_negate()))

    @property
    def finding_delta_sum(self) -> Decimal:
        return sum_decimals(finding.delta for finding in self.findings)

    @property
    def exceptions(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_exception)

    @property
    def secondary_mismatch_findings(self) -> tuple[ReconciliationFinding, ...]:
        """Findings with secondary differences, retaining result order."""

        return tuple(finding for finding in self.findings if finding.has_secondary_mismatch)

    @property
    def tolerated_findings(self) -> tuple[ReconciliationFinding, ...]:
        """Accepted nonzero differences, retained separately from review exceptions."""

        return tuple(
            finding
            for finding in self.findings
            if finding.category is FindingCategory.WITHIN_TOLERANCE and not finding.is_exception
        )

    @property
    def tolerated_delta_total(self) -> Decimal:
        """Signed net accepted variance; opposing tolerated deltas can cancel."""

        return sum_decimals(finding.delta for finding in self.tolerated_findings)
