"""Shared column configuration for manual mappings and portable profiles."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnMapping:
    """Selections in displayed pair order; None means not yet selected."""

    key_pairs: tuple[tuple[str | None, str | None], ...]
    amount_a: str | None
    amount_b: str | None

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
        return None

    @property
    def keys_a(self) -> tuple[str | None, ...]:
        return tuple(left for left, _ in self.key_pairs)

    @property
    def keys_b(self) -> tuple[str | None, ...]:
        return tuple(right for _, right in self.key_pairs)
