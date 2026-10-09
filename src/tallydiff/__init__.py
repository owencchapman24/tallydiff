"""TallyDiff: traceable reconciliation for structured financial exports."""

from tallydiff.amounts import AmountParseError, parse_amount
from tallydiff.configuration import ColumnMapping
from tallydiff.engine import NormalizationCollisionError, ReconciliationIntegrityError, reconcile
from tallydiff.export import export_exceptions_csv
from tallydiff.ingest import IngestionError, ingest_csv, inspect_csv_columns
from tallydiff.models import (
    FindingCategory,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
)
from tallydiff.normalization import (
    KeyNormalizationError,
    NormalizationCollision,
    OriginalKeyEvidence,
)
from tallydiff.normalization_config import KeyNormalizationConfig, KeyNormalizationRules
from tallydiff.profiles import (
    MappingProfile,
    ProfileError,
    export_mapping_profile,
    load_mapping_profile,
)
from tallydiff.xlsx import ingest_xlsx, inspect_xlsx_columns, inspect_xlsx_sheets

__all__ = [
    "AmountParseError",
    "ColumnMapping",
    "FindingCategory",
    "IngestionError",
    "KeyNormalizationConfig",
    "KeyNormalizationError",
    "KeyNormalizationRules",
    "MappingProfile",
    "NormalizationCollision",
    "NormalizationCollisionError",
    "OriginalKeyEvidence",
    "ProfileError",
    "ReconciliationFinding",
    "ReconciliationIntegrityError",
    "ReconciliationMode",
    "ReconciliationResult",
    "Source",
    "SourceRecord",
    "export_exceptions_csv",
    "export_mapping_profile",
    "ingest_csv",
    "ingest_xlsx",
    "inspect_csv_columns",
    "inspect_xlsx_columns",
    "inspect_xlsx_sheets",
    "load_mapping_profile",
    "parse_amount",
    "reconcile",
]
