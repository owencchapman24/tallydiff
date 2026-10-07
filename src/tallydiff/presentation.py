"""Small, UI-independent input and display helpers for the Streamlit app."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tallydiff.ingest import IngestionError
from tallydiff.models import FindingCategory, ReconciliationFinding, Source, SourceRecord

CATEGORY_LABELS = {
    FindingCategory.EXACT_MATCH: "Exact match",
    FindingCategory.AMOUNT_MISMATCH: "Amount mismatch",
    FindingCategory.A_ONLY: "File A only",
    FindingCategory.B_ONLY: "File B only",
    FindingCategory.DUPLICATE_AMBIGUOUS: "Duplicate / ambiguous",
}


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


def decode_upload(data: bytes, *, source: Source) -> str:
    """Decode UTF-8 with an optional BOM, with no replacement or guessing."""

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestionError(
            source,
            f"CSV must be UTF-8 (with or without BOM); invalid encoding at byte {exc.start}.",
        ) from None


def configuration_id(
    data_a: bytes,
    data_b: bytes,
    mapping: ColumnMapping,
    *,
    name_a: str,
    name_b: str,
) -> str:
    """Identify the exact files and ordered selections behind a displayed result."""

    identity = [
        [name_a, hashlib.sha256(data_a).hexdigest()],
        [name_b, hashlib.sha256(data_b).hexdigest()],
        mapping.key_pairs,
        mapping.amount_a,
        mapping.amount_b,
    ]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("utf-8")).hexdigest()


def display_amount(amount: Decimal, *, signed: bool = False) -> str:
    """Render every decimal place directly; positive deltas get an explicit plus."""

    return format(amount, "+f" if signed and amount > 0 else "f")


def finding_rows(findings: Sequence[ReconciliationFinding]) -> list[dict[str, str]]:
    return [
        {
            "Category": CATEGORY_LABELS[finding.category],
            "Matching key": " / ".join(finding.key),
            "File A amount": display_amount(finding.amount_a) if finding.rows_a else "—",
            "File B amount": display_amount(finding.amount_b) if finding.rows_b else "—",
            "Delta (A - B)": display_amount(finding.delta, signed=True),
            "File A records": ", ".join(str(row.source_row) for row in finding.rows_a),
            "File B records": ", ".join(str(row.source_row) for row in finding.rows_b),
        }
        for finding in findings
    ]


def evidence_rows(record: SourceRecord) -> list[dict[str, str]]:
    return [{"Field": field, "Original value": value} for field, value in record.raw_fields.items()]
