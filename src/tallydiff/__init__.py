"""TallyDiff: traceable reconciliation for structured financial exports."""

from tallydiff.amounts import AmountParseError, parse_amount
from tallydiff.engine import ReconciliationIntegrityError, reconcile
from tallydiff.ingest import IngestionError, ingest_csv
from tallydiff.models import (
    FindingCategory,
    ReconciliationFinding,
    ReconciliationResult,
    Source,
    SourceRecord,
)

__all__ = [
    "AmountParseError",
    "FindingCategory",
    "IngestionError",
    "ReconciliationFinding",
    "ReconciliationIntegrityError",
    "ReconciliationResult",
    "Source",
    "SourceRecord",
    "ingest_csv",
    "parse_amount",
    "reconcile",
]
