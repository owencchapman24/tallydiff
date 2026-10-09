"""Deterministic CSV summaries of reconciliation exceptions."""

import csv
import json
from io import StringIO

from tallydiff.models import FieldComparisonStatus, ReconciliationResult
from tallydiff.presentation import CATEGORY_LABELS, display_amount

_COLUMNS = (
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
)


def export_exceptions_csv(result: ReconciliationResult) -> bytes:
    """Return one CSV row per exception group, encoded as UTF-8 without a BOM.

    Columns and finding order are deterministic. Missing sides have blank
    amounts; present zero amounts retain their exact Decimal representation.
    All source record ordinals are retained, separated by ``; ``. CSV quoting
    preserves commas, quotes, and embedded newlines. An empty exception set
    produces a header-only CSV. Only review-required findings are included.
    Configured comparisons append mismatch labels and every field's structured
    values/status, including MATCH and NOT_COMPARABLE. Value tuples are serialized
    as Unicode JSON arrays without altering evidence. Record terminators are CRLF.

    Matching keys and evidence are not rewritten or spreadsheet-escaped. JSON
    serialization and CSV quoting do not prevent spreadsheet formula evaluation;
    consumers must import untrusted report cells as text. See the README's
    spreadsheet-safety limitation.
    """

    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    columns = list(_COLUMNS)
    if result.comparison_fields:
        columns.append("Secondary differences")
        for index, mapping in enumerate(result.comparison_fields, start=1):
            columns.extend(
                (
                    f"Comparison {index} File A values — {mapping.file_a}",
                    f"Comparison {index} File B values — {mapping.file_b}",
                    f"Comparison {index} status",
                )
            )
    writer.writerow(columns)
    for finding in result.exceptions:
        row = [
            CATEGORY_LABELS[finding.category],
            " / ".join(finding.key),
            display_amount(finding.amount_a) if finding.rows_a else "",
            display_amount(finding.amount_b) if finding.rows_b else "",
            display_amount(finding.delta, signed=True),
            "; ".join(str(row.source_row) for row in finding.rows_a),
            "; ".join(str(row.source_row) for row in finding.rows_b),
        ]
        if result.comparison_fields:
            row.append(
                "; ".join(
                    f"Comparison {index}: {comparison.mapping.file_a} ↔ {comparison.mapping.file_b}"
                    for index, comparison in enumerate(finding.field_comparisons, start=1)
                    if comparison.status is FieldComparisonStatus.MISMATCH
                )
            )
            for comparison in finding.field_comparisons:
                row.extend(
                    (
                        json.dumps(comparison.values_a, ensure_ascii=False),
                        json.dumps(comparison.values_b, ensure_ascii=False),
                        comparison.status.value,
                    )
                )
        writer.writerow(row)
    return output.getvalue().encode("utf-8")
