"""Core data models for TallyDiff's reconciliation engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from tallydiff._decimal import sum_decimals

type CompositeKey = tuple[str, ...]


class Source(StrEnum):
    """Identifies which reconciliation input a record came from."""

    A = "A"
    B = "B"


class FindingCategory(StrEnum):
    """Mutually exclusive outcome for one exact reconciliation key."""

    EXACT_MATCH = "exact_match"
    WITHIN_TOLERANCE = "within_tolerance"
    AMOUNT_MISMATCH = "amount_mismatch"
    A_ONLY = "a_only"
    B_ONLY = "b_only"
    DUPLICATE_AMBIGUOUS = "duplicate_ambiguous"


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
    """The reconciliation outcome for one exact composite key."""

    category: FindingCategory
    key: CompositeKey
    rows_a: tuple[SourceRecord, ...]
    rows_b: tuple[SourceRecord, ...]
    amount_a: Decimal
    amount_b: Decimal

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
    """Complete deterministic result for one reconciliation run."""

    total_a: Decimal
    total_b: Decimal
    findings: tuple[ReconciliationFinding, ...]
    amount_tolerance: Decimal = Decimal("0")

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
