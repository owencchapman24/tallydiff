"""Core data models for TallyDiff's reconciliation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Mapping, TypeAlias

CompositeKey: TypeAlias = tuple[str, ...]


class Source(str, Enum):
    """Identifies which reconciliation input a record came from."""

    A = "A"
    B = "B"


class FindingCategory(str, Enum):
    """Mutually exclusive outcome for one normalized reconciliation key."""

    EXACT_MATCH = "exact_match"
    AMOUNT_MISMATCH = "amount_mismatch"
    A_ONLY = "a_only"
    B_ONLY = "b_only"
    DUPLICATE_AMBIGUOUS = "duplicate_ambiguous"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One already-parsed source record ready for reconciliation."""

    source: Source
    source_row: int
    key: CompositeKey
    amount: Decimal
    raw_fields: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.source_row < 1:
            raise ValueError("source_row must be a positive integer")
        if not self.key:
            raise ValueError("key must contain at least one component")
        if any(component == "" for component in self.key):
            raise ValueError("key components must not be blank")


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """The reconciliation outcome for one normalized composite key."""

    category: FindingCategory
    key: CompositeKey
    rows_a: tuple[SourceRecord, ...]
    rows_b: tuple[SourceRecord, ...]
    amount_a: Decimal
    amount_b: Decimal

    @property
    def delta(self) -> Decimal:
        """Signed contribution to File A total minus File B total."""

        return self.amount_a - self.amount_b

    @property
    def is_exception(self) -> bool:
        return self.category is not FindingCategory.EXACT_MATCH


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Complete deterministic result for one reconciliation run."""

    total_a: Decimal
    total_b: Decimal
    findings: tuple[ReconciliationFinding, ...]

    @property
    def control_difference(self) -> Decimal:
        return self.total_a - self.total_b

    @property
    def finding_delta_sum(self) -> Decimal:
        return sum((finding.delta for finding in self.findings), Decimal("0"))

    @property
    def exceptions(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(finding for finding in self.findings if finding.is_exception)
