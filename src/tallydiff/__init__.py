"""TallyDiff: traceable reconciliation for structured financial exports."""

from tallydiff.amounts import AmountParseError, parse_amount
from tallydiff.configuration import ColumnMapping, ComparisonFieldMapping
from tallydiff.engine import NormalizationCollisionError, ReconciliationIntegrityError, reconcile
from tallydiff.export import export_exceptions_csv
from tallydiff.ingest import IngestionError, ingest_csv, inspect_csv_columns
from tallydiff.models import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    CorrespondenceAnalysis,
    CorrespondenceReason,
    CorrespondenceStatus,
    FieldComparison,
    FieldComparisonStatus,
    FindingCategory,
    OneToManyPolicy,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    SubsetSolution,
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
    "EXACT_UNIQUE_ONE_TO_MANY_POLICY",
    "AmountParseError",
    "ColumnMapping",
    "ComparisonFieldMapping",
    "CorrespondenceAnalysis",
    "CorrespondenceReason",
    "CorrespondenceStatus",
    "FieldComparison",
    "FieldComparisonStatus",
    "FindingCategory",
    "IngestionError",
    "KeyNormalizationConfig",
    "KeyNormalizationError",
    "KeyNormalizationRules",
    "MappingProfile",
    "NormalizationCollision",
    "NormalizationCollisionError",
    "OriginalKeyEvidence",
    "OneToManyPolicy",
    "ProfileError",
    "ReconciliationFinding",
    "ReconciliationIntegrityError",
    "ReconciliationMode",
    "ReconciliationResult",
    "Source",
    "SourceRecord",
    "SubsetSolution",
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
