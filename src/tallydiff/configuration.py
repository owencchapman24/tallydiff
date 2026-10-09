"""Shared column configuration for manual mappings and portable profiles."""

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ComparisonFieldMapping:
    """One directional secondary field pair; None permits an editable selection."""

    file_a: str | None
    file_b: str | None

    def __post_init__(self) -> None:
        for name in ("file_a", "file_b"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a str or None")

    @property
    def is_complete(self) -> bool:
        """Whether both sides select nonblank column names, without trimming them."""

        return all(
            value is not None and bool(value.strip()) for value in (self.file_a, self.file_b)
        )


@dataclass(frozen=True)
class ColumnMapping:
    """Selections in displayed pair order; None means not yet selected."""

    key_pairs: tuple[tuple[str | None, str | None], ...]
    amount_a: str | None
    amount_b: str | None
    comparison_fields: tuple[ComparisonFieldMapping, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.comparison_fields, tuple) or any(
            not isinstance(mapping, ComparisonFieldMapping) for mapping in self.comparison_fields
        ):
            raise TypeError("comparison_fields must be a tuple of ComparisonFieldMapping objects")

    def problem(self, columns_a: Sequence[str], columns_b: Sequence[str]) -> str | None:
        if not self.key_pairs:
            return "Add at least one matching key field."
        for index, (left, right) in enumerate(self.key_pairs, start=1):
            if left not in columns_a or right not in columns_b:
                return f"Choose a File A and File B column for key field {index}."
        for side, index in (("A", 0), ("B", 1)):
            names = [pair[index] for pair in self.key_pairs]
            if len(set(names)) != len(names):
                return f"Each File {side} key column must be selected only once."
        if self.amount_a not in columns_a or self.amount_b not in columns_b:
            return "Choose an amount column for both files."
        for index, mapping in enumerate(self.comparison_fields, start=1):
            if (
                not mapping.is_complete
                or mapping.file_a not in columns_a
                or mapping.file_b not in columns_b
            ):
                return f"Choose a File A and File B column for comparison field {index}."
        for side, names, amount in (
            ("A", self.comparisons_a, self.amount_a),
            ("B", self.comparisons_b, self.amount_b),
        ):
            if len(set(names)) != len(names):
                return f"Each File {side} comparison column must be selected only once."
            if amount in names:
                return f"File {side} comparison fields must not use the amount column."
        return None

    @property
    def keys_a(self) -> tuple[str | None, ...]:
        return tuple(left for left, _ in self.key_pairs)

    @property
    def keys_b(self) -> tuple[str | None, ...]:
        return tuple(right for _, right in self.key_pairs)

    @property
    def comparisons_a(self) -> tuple[str | None, ...]:
        return tuple(mapping.file_a for mapping in self.comparison_fields)

    @property
    def comparisons_b(self) -> tuple[str | None, ...]:
        return tuple(mapping.file_b for mapping in self.comparison_fields)
