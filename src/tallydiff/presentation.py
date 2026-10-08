"""Small, UI-independent input and display helpers for the Streamlit app."""

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal

from tallydiff.configuration import ColumnMapping as ColumnMapping
from tallydiff.ingest import IngestionError
from tallydiff.models import FindingCategory, ReconciliationFinding, Source, SourceRecord

CATEGORY_LABELS = {
    FindingCategory.EXACT_MATCH: "Exact match",
    FindingCategory.WITHIN_TOLERANCE: "Within tolerance",
    FindingCategory.AMOUNT_MISMATCH: "Amount mismatch",
    FindingCategory.A_ONLY: "File A only",
    FindingCategory.B_ONLY: "File B only",
    FindingCategory.DUPLICATE_AMBIGUOUS: "Duplicate / ambiguous",
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
    worksheet_a: str | None = None,
    worksheet_b: str | None = None,
) -> str:
    """Identify the files, ordered selections, and tolerance behind a displayed result."""

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
