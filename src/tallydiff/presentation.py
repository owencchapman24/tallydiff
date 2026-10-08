"""Small, UI-independent input and display helpers for the Streamlit app."""

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal

from tallydiff.configuration import ColumnMapping as ColumnMapping
from tallydiff.ingest import IngestionError
from tallydiff.models import (
    FindingCategory,
    ReconciliationFinding,
    ReconciliationMode,
    Source,
    SourceRecord,
)

CATEGORY_LABELS = {
    FindingCategory.EXACT_MATCH: "Exact match",
    FindingCategory.WITHIN_TOLERANCE: "Within tolerance",
    FindingCategory.AMOUNT_MISMATCH: "Amount mismatch",
    FindingCategory.A_ONLY: "File A only",
    FindingCategory.B_ONLY: "File B only",
    FindingCategory.DUPLICATE_AMBIGUOUS: "Duplicate / ambiguous",
}


MODE_LABELS = {
    ReconciliationMode.UNIQUE: "Unique records",
    ReconciliationMode.GROUPED_BY_KEY: "Group by matching key",
}


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
    amount_tolerance: Decimal = Decimal("0"),
    reconciliation_mode: ReconciliationMode = ReconciliationMode.UNIQUE,
    worksheet_a: str | None = None,
    worksheet_b: str | None = None,
) -> str:
    """Identify the files, ordered selections, tolerance, and mode behind a displayed result."""

    if not isinstance(reconciliation_mode, ReconciliationMode):
        raise TypeError("reconciliation_mode must be a ReconciliationMode enum member")
    tolerance_identity = format(amount_tolerance, "f")
    if "." in tolerance_identity:
        tolerance_identity = tolerance_identity.rstrip("0").rstrip(".")
    if amount_tolerance.is_zero():
        tolerance_identity = "0"
    identity = [
        [name_a, hashlib.sha256(data_a).hexdigest(), worksheet_a],
        [name_b, hashlib.sha256(data_b).hexdigest(), worksheet_b],
        mapping.key_pairs,
        mapping.amount_a,
        mapping.amount_b,
        tolerance_identity,
        reconciliation_mode.value,
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


EXCEPTION_CATEGORIES = (
    FindingCategory.AMOUNT_MISMATCH,
    FindingCategory.A_ONLY,
    FindingCategory.B_ONLY,
    FindingCategory.DUPLICATE_AMBIGUOUS,
)

EXCEPTION_SORT_LABELS = {
    "matching_key": "Matching key (ascending)",
    "absolute_delta_desc": "Absolute delta (largest first)",
    "absolute_delta_asc": "Absolute delta (smallest first)",
}


def review_exceptions(
    findings: Sequence[ReconciliationFinding],
    *,
    query: str = "",
    categories: Sequence[FindingCategory] = EXCEPTION_CATEGORIES,
    minimum_abs_delta: Decimal = Decimal("0"),
    sort_order: str = "matching_key",
) -> tuple[ReconciliationFinding, ...]:
    """Return a display-only view, preserving finding objects and all source evidence.

    Search trims the query and compares casefolded substrings within each key
    component. The minimum absolute delta is inclusive. Delta sorts break ties
    by exact key, then retain input order for otherwise identical sort keys.
    """

    if not isinstance(minimum_abs_delta, Decimal):
        raise TypeError("minimum_abs_delta must be a Decimal")
    if not minimum_abs_delta.is_finite() or minimum_abs_delta < 0:
        raise ValueError("minimum_abs_delta must be finite and zero or greater")
    if sort_order not in EXCEPTION_SORT_LABELS:
        raise ValueError("Unsupported exception sort order")
    if any(
        not isinstance(category, FindingCategory) or category not in EXCEPTION_CATEGORIES
        for category in categories
    ):
        raise ValueError("categories must contain only exception category enum members")
    selected = frozenset(categories)
    search = query.strip().casefold()
    matches = []
    for finding in findings:
        if finding.category not in selected:
            continue
        if search and not any(search in component.casefold() for component in finding.key):
            continue
        absolute_delta = finding.delta.copy_abs()
        if absolute_delta >= minimum_abs_delta:
            matches.append((finding, absolute_delta))
    if sort_order == "matching_key":
        matches.sort(key=lambda item: item[0].key)
    elif sort_order == "absolute_delta_desc":
        matches.sort(key=lambda item: (item[1].copy_negate(), item[0].key))
    else:
        matches.sort(key=lambda item: (item[1], item[0].key))
    return tuple(finding for finding, _ in matches)


def evidence_rows(record: SourceRecord) -> list[dict[str, str]]:
    return [{"Field": field, "Original value": value} for field, value in record.raw_fields.items()]
