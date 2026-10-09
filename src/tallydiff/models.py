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
    """Whether matching keys require unique rows or compare all source-row totals."""

    UNIQUE = "unique"
    GROUPED_BY_KEY = "grouped_by_key"


class FindingCategory(StrEnum):
    """Mutually exclusive outcome for one reconciliation key."""

    EXACT_MATCH = "exact_match"
    WITHIN_TOLERANCE = "within_tolerance"
    AMOUNT_MISMATCH = "amount_mismatch"
    A_ONLY = "a_only"
    B_ONLY = "b_only"
    DUPLICATE_AMBIGUOUS = "duplicate_ambiguous"


class FieldComparisonStatus(StrEnum):
    """Secondary field agreement, independent of the primary finding category."""

    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """Sorted distinct ingested strings; a present blank is ("",), not ().

    Source records remain on the finding. NOT_COMPARABLE permits either equal
    or unequal summaries; the primary category explains why comparison is unavailable.
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

    def __post_init__(self) -> None:
        if not isinstance(self.field_comparisons, tuple) or any(
            not isinstance(comparison, FieldComparison) for comparison in self.field_comparisons
        ):
            raise TypeError("field_comparisons must be a tuple of FieldComparison objects")

    @property
    def has_secondary_mismatch(self) -> bool:
        """Whether any secondary field differs, independently of exception membership."""

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
        """Whether this group requires review rather than representing accepted variance."""

        return self.category not in (
            FindingCategory.EXACT_MATCH,
            FindingCategory.WITHIN_TOLERANCE,
        )


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Complete result retaining explicit normalization, or None for exact matching."""

    total_a: Decimal
    total_b: Decimal
    findings: tuple[ReconciliationFinding, ...]
    amount_tolerance: Decimal = Decimal("0")
    mode: ReconciliationMode = ReconciliationMode.UNIQUE
    key_normalization: KeyNormalizationConfig | None = None
    comparison_fields: tuple[ComparisonFieldMapping, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ReconciliationMode):
            raise TypeError("mode must be a ReconciliationMode enum member")
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
            if (
                tuple(comparison.mapping for comparison in finding.field_comparisons)
                != self.comparison_fields
            ):
                raise ValueError(
                    "finding field_comparisons must match comparison_fields in count and order"
                )

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
    def tolerated_findings(self) -> tuple[ReconciliationFinding, ...]:
        """Accepted nonzero differences, retained separately from review exceptions."""

        return tuple(
            finding
            for finding in self.findings
            if finding.category is FindingCategory.WITHIN_TOLERANCE
        )

    @property
    def tolerated_delta_total(self) -> Decimal:
        """Signed net accepted variance; opposing tolerated deltas can cancel."""

        return sum_decimals(finding.delta for finding in self.tolerated_findings)
