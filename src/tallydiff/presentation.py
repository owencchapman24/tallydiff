"""Small, UI-independent input and display helpers for the Streamlit app."""

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal

from tallydiff.configuration import ColumnMapping as ColumnMapping
from tallydiff.ingest import IngestionError
from tallydiff.models import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    CorrespondenceReason,
    CorrespondenceStatus,
    FindingCategory,
    ReconciliationFinding,
    ReconciliationMode,
    Source,
    SourceRecord,
)
from tallydiff.normalization_config import KeyNormalizationConfig

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

CORRESPONDENCE_STATUS_LABELS = {
    CorrespondenceStatus.UNIQUE_EXACT: "Unique exact subset",
    CorrespondenceStatus.AMBIGUOUS: "Ambiguous exact subsets",
    CorrespondenceStatus.NO_EXACT_SUBSET: "No exact subset",
    CorrespondenceStatus.SINGLETON_ONLY: "Singleton match only",
    CorrespondenceStatus.BOUND_EXCEEDED: "Search bound exceeded",
    CorrespondenceStatus.NOT_ELIGIBLE: "Not eligible for subset search",
}

CORRESPONDENCE_REASON_LABELS = {
    CorrespondenceReason.BOTH_SIDES_MULTIPLE: "Multiple records on both sides",
    CorrespondenceReason.MISSING_OPPOSITE_SIDE: "Missing records on the opposite side",
    CorrespondenceReason.CANDIDATE_ROW_LIMIT: "Candidate row limit exceeded",
    CorrespondenceReason.RUN_BUDGET_EXHAUSTED: "Run search budget exhausted",
}


NORMALIZATION_LABELS = {
    "casefold": "Ignore letter case",
    "collapse_whitespace": "Collapse whitespace",
    "remove_punctuation": "Ignore punctuation",
    "strip_leading_zeros": "Ignore leading zeros",
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
    key_normalization: KeyNormalizationConfig | None = None,
) -> str:
    """Identify files, ordered mappings, tolerance, mode, and canonical normalization.

    Exact/all-false rules preserve the existing exact identity. Enabled rule values
    and component order are included as primitive data, independently of UI labels.
    Nonempty secondary mappings append their exact directional editing values,
    including incomplete selections. Empty comparisons preserve existing digests.
    Bounded mode appends its fixed policy identifier after those optional components.
    """

    if not isinstance(reconciliation_mode, ReconciliationMode):
        raise TypeError("reconciliation_mode must be a ReconciliationMode enum member")
    if key_normalization is not None and not isinstance(key_normalization, KeyNormalizationConfig):
        raise TypeError("key_normalization must be a KeyNormalizationConfig or None")
    if key_normalization is not None and len(key_normalization.component_rules) != len(
        mapping.key_pairs
    ):
        raise ValueError("key_normalization length must equal key_pairs length")
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
    if key_normalization is not None:
        normalization = [
            {
                "casefold": rules.casefold,
                "collapse_whitespace": rules.collapse_whitespace,
                "remove_punctuation": rules.remove_punctuation,
                "strip_leading_zeros": rules.strip_leading_zeros,
            }
            for rules in key_normalization.component_rules
        ]
        if any(any(rules.values()) for rules in normalization):
            identity.append({"key_normalization": normalization})
    if mapping.comparison_fields:
        identity.append(
            {
                "comparison_fields": [
                    [field.file_a, field.file_b] for field in mapping.comparison_fields
                ]
            }
        )
    if reconciliation_mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        identity.append({"one_to_many_policy": EXACT_UNIQUE_ONE_TO_MANY_POLICY.policy_id})
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("utf-8")).hexdigest()


def display_amount(amount: Decimal, *, signed: bool = False) -> str:
    """Render every decimal place directly; positive deltas get an explicit plus."""

    return format(amount, "+f" if signed and amount > 0 else "f")


def finding_rows(findings: Sequence[ReconciliationFinding]) -> list[dict[str, str]]:
    """Summarize findings, appending metadata columns only when evidence is present."""

    rows = [
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
    if any(finding.field_comparisons for finding in findings):
        for row, finding in zip(rows, findings, strict=True):
            row["Secondary differences"] = "; ".join(
                f"{comparison.mapping.file_a} ↔ {comparison.mapping.file_b}"
                for comparison in finding.secondary_mismatches
            )
    if any(finding.correspondence_analysis is not None for finding in findings):
        for row, finding in zip(rows, findings, strict=True):
            row["Correspondence status"] = (
                CORRESPONDENCE_STATUS_LABELS[finding.correspondence_status]
                if finding.correspondence_analysis is not None
                else ""
            )
    return rows


EXCEPTION_CATEGORIES = (
    FindingCategory.AMOUNT_MISMATCH,
    FindingCategory.A_ONLY,
    FindingCategory.B_ONLY,
    FindingCategory.DUPLICATE_AMBIGUOUS,
)

REVIEW_CATEGORIES = tuple(FindingCategory)

EXCEPTION_SORT_LABELS = {
    "matching_key": "Matching key (ascending)",
    "absolute_delta_desc": "Absolute delta (largest first)",
    "absolute_delta_asc": "Absolute delta (smallest first)",
}


def review_exceptions(
    findings: Sequence[ReconciliationFinding],
    *,
    query: str = "",
    categories: Sequence[FindingCategory] = REVIEW_CATEGORIES,
    minimum_abs_delta: Decimal = Decimal("0"),
    sort_order: str = "matching_key",
    secondary_mismatches_only: bool = False,
) -> tuple[ReconciliationFinding, ...]:
    """Return a display-only view, preserving finding objects and all source evidence.

    Accepted findings are always excluded, even when their categories are selected.
    Search trims the query and compares casefolded substrings within each key
    component. The minimum absolute delta is inclusive: a zero-delta secondary
    exception is visible at zero and hidden by any positive minimum. The optional
    secondary filter retains only findings with a secondary mismatch. Delta sorts
    break ties by exact key, then retain input order for otherwise identical keys.
    """

    if not isinstance(minimum_abs_delta, Decimal):
        raise TypeError("minimum_abs_delta must be a Decimal")
    if not minimum_abs_delta.is_finite() or minimum_abs_delta < 0:
        raise ValueError("minimum_abs_delta must be finite and zero or greater")
    if sort_order not in EXCEPTION_SORT_LABELS:
        raise ValueError("Unsupported exception sort order")
    if any(not isinstance(category, FindingCategory) for category in categories):
        raise ValueError("categories must contain only FindingCategory enum members")
    if not isinstance(secondary_mismatches_only, bool):
        raise TypeError("secondary_mismatches_only must be a bool")
    selected = frozenset(categories)
    search = query.strip().casefold()
    matches = []
    for finding in findings:
        if not finding.is_exception:
            continue
        if secondary_mismatches_only and not finding.has_secondary_mismatch:
            continue
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
