"""Deterministic CSV summaries of reconciliation exceptions."""

import csv
from io import StringIO

from tallydiff.models import ReconciliationResult
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
    produces a header-only CSV. Exact matches and accepted within-tolerance
    findings are excluded. Record terminators are CRLF.

    Matching keys are not rewritten or spreadsheet-escaped. CSV quoting does
    not prevent spreadsheet formula evaluation; consumers must import untrusted
    report cells as text. See the README's spreadsheet-safety limitation.
    """

    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(_COLUMNS)
    for finding in result.exceptions:
        writer.writerow(
            (
                CATEGORY_LABELS[finding.category],
                " / ".join(finding.key),
                display_amount(finding.amount_a) if finding.rows_a else "",
                display_amount(finding.amount_b) if finding.rows_b else "",
                display_amount(finding.delta, signed=True),
                "; ".join(str(row.source_row) for row in finding.rows_a),
                "; ".join(str(row.source_row) for row in finding.rows_b),
            )
        )
    return output.getvalue().encode("utf-8")
