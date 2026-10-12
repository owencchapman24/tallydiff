"""Deterministic CSV summaries of reconciliation exceptions."""

import csv
import json
from io import StringIO

from tallydiff.models import (
    FieldComparisonStatus,
    ReconciliationFinding,
    ReconciliationResult,
    SourceRecord,
    SubsetSolution,
)
from tallydiff.presentation import CATEGORY_LABELS, display_amount
from tallydiff.review import ReviewBinding, ReviewDisposition, ReviewSession, ReviewSessionError

_COLUMNS = (
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
)
_REVIEW_DISPOSITION_LABELS = {
    ReviewDisposition.UNREVIEWED: "Unreviewed",
    ReviewDisposition.NEEDS_FOLLOW_UP: "Needs follow-up",
    ReviewDisposition.REVIEW_COMPLETE: "Review complete",
}


def _row_reference(row: SourceRecord) -> dict[str, object]:
    return {
        "source": row.source.value,
        "source_row": row.source_row,
        "amount": display_amount(row.amount),
        "original_key": list(row.key),
    }


def _solution_evidence(solution: SubsetSolution) -> dict[str, object]:
    return {
        "amount": display_amount(solution.amount),
        "rows_a": [_row_reference(row) for row in solution.rows_a],
        "rows_b": [_row_reference(row) for row in solution.rows_b],
    }


def _correspondence_evidence(finding: ReconciliationFinding) -> str:
    """Serialize stored evidence, deriving search direction from the complete parent."""

    analysis = finding.correspondence_analysis
    policy = analysis.policy
    anchor = None
    if len(finding.rows_a) == 1 and len(finding.rows_b) > 1:
        anchor = finding.rows_a[0]
    elif len(finding.rows_b) == 1 and len(finding.rows_a) > 1:
        anchor = finding.rows_b[0]
    evidence = {
        "schema_version": 1,
        "policy": {
            "policy_id": policy.policy_id,
            "max_candidate_rows": policy.max_candidate_rows,
            "minimum_accepted_subset_rows": policy.minimum_accepted_subset_rows,
            "max_planned_combinations_per_run": policy.max_planned_combinations_per_run,
            "max_ambiguity_witnesses": policy.max_ambiguity_witnesses,
            "exact_amounts_only": policy.exact_amounts_only,
            "physical_row_uniqueness": policy.physical_row_uniqueness,
            "singleton_rivals_count": policy.singleton_rivals_count,
        },
        "matching_key": list(finding.key),
        "status": analysis.status.value,
        "reason": analysis.reason.value if analysis.reason is not None else None,
        "anchor": _row_reference(anchor) if anchor is not None else None,
        "accepted_solution": (
            _solution_evidence(analysis.accepted_solution)
            if analysis.accepted_solution is not None
            else None
        ),
        "unassigned_rows_a": [_row_reference(row) for row in analysis.unassigned_rows_a],
        "unassigned_rows_b": [_row_reference(row) for row in analysis.unassigned_rows_b],
        "ambiguity_witnesses": [
            _solution_evidence(witness) for witness in analysis.ambiguity_witnesses
        ],
        "planned_combinations": analysis.planned_combinations,
        "examined_combinations": analysis.examined_combinations,
        "reserved_combinations": analysis.reserved_combinations,
        "search_complete": analysis.search_complete,
    }
    return json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))


def _exception_columns(result: ReconciliationResult, *, has_correspondence: bool) -> list[str]:
    """Construct the original report schema from stored configuration/evidence."""

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
    if has_correspondence:
        columns.extend(("Correspondence status", "Correspondence evidence"))
    return columns


def _exception_row(
    result: ReconciliationResult,
    finding: ReconciliationFinding,
    *,
    has_correspondence: bool,
) -> list[str]:
    """Construct original cells without recomputing or rewriting evidence."""

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
    if has_correspondence:
        analysis = finding.correspondence_analysis
        row.extend(
            (analysis.status.value, _correspondence_evidence(finding))
            if analysis is not None
            else ("", "")
        )
    return row


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
    Stored correspondence analysis appends status and versioned JSON evidence only
    when present among the exported exceptions, after all comparison columns.

    Matching keys and evidence are not rewritten or spreadsheet-escaped. JSON
    serialization and CSV quoting do not prevent spreadsheet formula evaluation;
    consumers must import untrusted report cells as text. See the README's
    spreadsheet-safety limitation.
    """

    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    exceptions = result.exceptions
    has_correspondence = any(finding.correspondence_analysis is not None for finding in exceptions)
    writer.writerow(_exception_columns(result, has_correspondence=has_correspondence))
    for finding in exceptions:
        writer.writerow(_exception_row(result, finding, has_correspondence=has_correspondence))
    return output.getvalue().encode("utf-8")


def export_reviewed_exceptions_csv(binding: ReviewBinding, session: ReviewSession) -> bytes:
    """Append analyst annotations to the complete, unchanged exception evidence.

    A session must match the binding's fingerprint and canonical exception keys
    in order; independently reconstructed equivalent bindings are supported.
    Mismatches raise ReviewSessionError before any output is constructed.
    Review dispositions never filter exceptions or change reconciliation truth.

    The UTF-8 CSV has no BOM and uses CRLF record terminators. Every nonempty
    analyst note is rendered as ``"Note: " + note`` without trimming; saved notes
    and original evidence cells remain unchanged. CSV quoting preserves embedded
    delimiters/newlines. Only the new note column receives this display prefix.
    """

    if not isinstance(binding, ReviewBinding):
        raise TypeError("binding must be a ReviewBinding")
    if not isinstance(session, ReviewSession):
        raise TypeError("session must be a ReviewSession")
    if session.binding.result_fingerprint != binding.result_fingerprint:
        raise ReviewSessionError("session fingerprint does not match the review binding")
    if session.binding.exception_keys != binding.exception_keys:
        raise ReviewSessionError("session binding exception keys do not match the review binding")
    if tuple(review.matching_key for review in session.reviews) != binding.exception_keys:
        raise ReviewSessionError("reviews must cover exactly the binding exception keys in order")

    result = binding.result
    exceptions = result.exceptions
    has_correspondence = any(finding.correspondence_analysis is not None for finding in exceptions)
    columns = _exception_columns(result, has_correspondence=has_correspondence)
    columns.extend(("Review disposition", "Review note"))
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(columns)
    for finding, review in zip(exceptions, session.reviews, strict=True):
        row = _exception_row(result, finding, has_correspondence=has_correspondence)
        row.extend(
            (
                _REVIEW_DISPOSITION_LABELS[review.disposition],
                "Note: " + review.note if review.note != "" else "",
            )
        )
        writer.writerow(row)
    return output.getvalue().encode("utf-8")
