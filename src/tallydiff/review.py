"""Analyst annotations kept separate from reconciliation truth."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from tallydiff._decimal import sum_decimals
from tallydiff.configuration import ColumnMapping, ComparisonFieldMapping
from tallydiff.models import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    CorrespondenceAnalysis,
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
from tallydiff.normalization_config import KeyNormalizationConfig, KeyNormalizationRules

_MAX_NOTE_BYTES = 16_384
_RESULT_PREFIX = b"tallydiff-review-result-v1\0"
_BINDING_PREFIX = b"tallydiff-review-binding-v1\0"
_RECONCILIATION_SEMANTICS = "tallydiff-reconciliation-v1"
_RULE_FIELDS = ("casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros")
_POLICY_FIELDS = (
    "policy_id",
    "max_candidate_rows",
    "minimum_accepted_subset_rows",
    "max_planned_combinations_per_run",
    "max_ambiguity_witnesses",
    "exact_amounts_only",
    "physical_row_uniqueness",
    "singleton_rivals_count",
)


class ReviewDisposition(StrEnum):
    """Analyst workflow state, without asserting accounting correctness."""

    UNREVIEWED = "unreviewed"
    NEEDS_FOLLOW_UP = "needs_follow_up"
    REVIEW_COMPLETE = "review_complete"

    @property
    def is_reviewed(self) -> bool:
        """Whether the analyst has committed a workflow decision."""

        return self is not ReviewDisposition.UNREVIEWED

    @property
    def is_open(self) -> bool:
        """Whether initial review or follow-up work remains."""

        return self is not ReviewDisposition.REVIEW_COMPLETE


class ReviewSessionError(ValueError):
    """An expected review binding or persisted-session validation failure."""


def _validate_note(note: str) -> None:
    if not isinstance(note, str):
        raise TypeError("note must be a str")
    try:
        size = len(note.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("note must be UTF-8 encodable, without Unicode surrogates") from None
    if size > _MAX_NOTE_BYTES:
        raise ValueError(f"note must not exceed {_MAX_NOTE_BYTES:,} UTF-8 bytes")
    for character in note:
        codepoint = ord(character)
        if (codepoint < 0x20 and character not in "\t\n\r") or 0x7F <= codepoint <= 0x9F:
            raise ValueError(f"note must not contain control character U+{codepoint:04X}")


@dataclass(frozen=True, slots=True)
class FindingReview:
    """One immutable annotation; keys and valid plain-text notes are retained exactly."""

    matching_key: tuple[str, ...]
    disposition: ReviewDisposition
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.matching_key, tuple) or any(
            not isinstance(component, str) for component in self.matching_key
        ):
            raise TypeError("matching_key must be a tuple of strings")
        if not self.matching_key:
            raise ValueError("matching_key must contain at least one component")
        if any(not component.strip() for component in self.matching_key):
            raise ValueError("matching_key components must not be blank")
        if not isinstance(self.disposition, ReviewDisposition):
            raise TypeError("disposition must be a ReviewDisposition enum member")
        _validate_note(self.note)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReviewSessionError(message)


def _text(value: object, label: str, *, nonblank: bool = False) -> None:
    _require(isinstance(value, str), f"{label} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ReviewSessionError(f"{label} must be UTF-8 encodable") from None
    if nonblank:
        _require(bool(value.strip()), f"{label} must be nonblank")


def _key(value: object, label: str, arity: int | None = None) -> None:
    _require(isinstance(value, tuple) and bool(value), f"{label} must be a nonempty tuple")
    if arity is not None:
        _require(len(value) == arity, f"{label} has inconsistent key arity")
    for component in value:
        _text(component, f"{label} component", nonblank=True)


def _decimal(value: object, label: str) -> None:
    _require(isinstance(value, Decimal), f"{label} must be a Decimal")
    _require(value.is_finite(), f"{label} must be finite")


def _model_invariants(value: object, model: type, label: str) -> None:
    """Validate stored model invariants without copying or modifying the model."""

    try:
        model.__post_init__(value)
    except (TypeError, ValueError, AttributeError):
        raise ReviewSessionError(f"{label} violates {model.__name__} invariants") from None


def _comparison_mappings(value: object, label: str) -> list[dict[str, str]]:
    _require(isinstance(value, tuple), f"{label} must be a tuple")
    payload = []
    for index, mapping in enumerate(value, start=1):
        _require(isinstance(mapping, ComparisonFieldMapping), f"{label} {index} must be a mapping")
        _text(mapping.file_a, f"{label} {index} File A", nonblank=True)
        _text(mapping.file_b, f"{label} {index} File B", nonblank=True)
        payload.append({"file_a": mapping.file_a, "file_b": mapping.file_b})
    for side in ("file_a", "file_b"):
        names = [mapping[side] for mapping in payload]
        _require(len(set(names)) == len(names), f"{label} has duplicate directional columns")
    return payload


def _mapping_payload(mapping: ColumnMapping) -> dict[str, object]:
    _require(
        isinstance(mapping.key_pairs, tuple) and bool(mapping.key_pairs),
        "mapping key_pairs must be a nonempty tuple",
    )
    pairs = []
    for index, pair in enumerate(mapping.key_pairs, start=1):
        _require(isinstance(pair, tuple) and len(pair) == 2, f"mapping key pair {index} is invalid")
        for column in pair:
            _text(column, f"mapping key pair {index} column", nonblank=True)
        pairs.append({"file_a": pair[0], "file_b": pair[1]})
    for side in ("file_a", "file_b"):
        names = [pair[side] for pair in pairs]
        _require(len(set(names)) == len(names), "mapping key columns must be unique on each side")
    _text(mapping.amount_a, "mapping amount_a", nonblank=True)
    _text(mapping.amount_b, "mapping amount_b", nonblank=True)
    comparisons = _comparison_mappings(mapping.comparison_fields, "mapping comparison_fields")
    for side, amount in (("file_a", mapping.amount_a), ("file_b", mapping.amount_b)):
        _require(
            all(field[side] != amount for field in comparisons),
            "mapping comparisons must not use the amount column",
        )
    return {
        "key_pairs": pairs,
        "amount_columns": {"file_a": mapping.amount_a, "file_b": mapping.amount_b},
        "comparison_fields": comparisons,
    }


def _normalization_payload(
    config: KeyNormalizationConfig | None, arity: int | None
) -> list[dict[str, bool]]:
    if config is None:
        return [{name: False for name in _RULE_FIELDS} for _ in range(arity or 0)]
    _require(isinstance(config, KeyNormalizationConfig), "result normalization must be a config")
    _model_invariants(config, KeyNormalizationConfig, "result normalization")
    if arity is not None:
        _require(
            len(config.component_rules) == arity, "result normalization has inconsistent arity"
        )
    for index, rules in enumerate(config.component_rules, start=1):
        _model_invariants(rules, KeyNormalizationRules, f"normalization component {index}")
    return [
        {name: getattr(rules, name) for name in _RULE_FIELDS} for rules in config.component_rules
    ]


def _policy_payload(policy: OneToManyPolicy | None) -> dict[str, object] | None:
    if policy is None:
        return None
    _require(isinstance(policy, OneToManyPolicy), "one-to-many policy must be a policy or None")
    _model_invariants(policy, OneToManyPolicy, "one-to-many policy")
    _text(policy.policy_id, "one-to-many policy identifier", nonblank=True)
    return {name: getattr(policy, name) for name in _POLICY_FIELDS}


def _validate_rows(rows: object, source: Source, arity: int, seen: set[int], label: str) -> None:
    _require(isinstance(rows, tuple), f"{label} must be a tuple")
    previous = 0
    for row in rows:
        _require(isinstance(row, SourceRecord), f"{label} must contain SourceRecord objects")
        _require(row.source is source, f"{label} contains a record on the wrong source side")
        _require(
            type(row.source_row) is int and row.source_row > 0, f"{label} has an invalid ordinal"
        )
        location = f"File {source.value} source row {row.source_row}"
        _require(row.source_row > previous, f"{label} source-row order must be strictly increasing")
        _require(row.source_row not in seen, f"{location} has a duplicate source-row identity")
        previous = row.source_row
        seen.add(row.source_row)
        _key(row.key, f"{location} original key", arity)
        _decimal(row.amount, f"{location} amount")
        _require(
            isinstance(row.raw_fields, MappingProxyType), f"{location} raw_fields must be immutable"
        )
        for name, value in row.raw_fields.items():
            _text(name, f"{location} evidence column")
            _text(value, f"{location} evidence value")


def _validate_references(rows: object, parents: tuple[SourceRecord, ...], label: str) -> None:
    _require(isinstance(rows, tuple), f"{label} must be a tuple")
    parent_index = {row.source_row: row for row in parents}
    previous = 0
    for row in rows:
        _require(isinstance(row, SourceRecord), f"{label} must contain source records")
        _require(
            type(row.source_row) is int and row.source_row > previous,
            f"{label} has invalid ordinals",
        )
        _require(
            parent_index.get(row.source_row) is row,
            f"{label} must reference original parent records",
        )
        previous = row.source_row


def _validate_analysis(finding: ReconciliationFinding, label: str) -> None:
    analysis = finding.correspondence_analysis
    if analysis is None:
        return
    _require(
        isinstance(analysis, CorrespondenceAnalysis), f"{label} analysis must be stored analysis"
    )
    _policy_payload(analysis.policy)
    _require(isinstance(analysis.ambiguity_witnesses, tuple), f"{label} witnesses must be a tuple")
    solutions = analysis.ambiguity_witnesses
    if analysis.accepted_solution is not None:
        solutions = (analysis.accepted_solution, *solutions)
    for index, solution in enumerate(solutions, start=1):
        location = f"{label} solution {index}"
        _require(isinstance(solution, SubsetSolution), f"{location} must be a solution")
        for side in ("a", "b"):
            _validate_references(
                getattr(solution, f"rows_{side}"), getattr(finding, f"rows_{side}"), location
            )
        _decimal(solution.amount, f"{location} amount")
        _model_invariants(solution, SubsetSolution, location)
    for side in ("a", "b"):
        _validate_references(
            getattr(analysis, f"unassigned_rows_{side}"),
            getattr(finding, f"rows_{side}"),
            f"{label} unassigned File {side.upper()} references",
        )
    _model_invariants(analysis, CorrespondenceAnalysis, f"{label} analysis")


def _same_population(actual: object, expected: tuple, label: str) -> None:
    _require(
        isinstance(actual, tuple)
        and len(actual) == len(expected)
        and all(left is right for left, right in zip(actual, expected, strict=True)),
        f"{label} is inconsistent with the completed findings",
    )


def _validate_result(result: ReconciliationResult, arity: int | None = None) -> None:
    _require(isinstance(result.findings, tuple), "result findings must be a tuple")
    _require(
        isinstance(result.mode, ReconciliationMode), "result mode must be a ReconciliationMode"
    )
    _decimal(result.amount_tolerance, "result amount tolerance")
    _require(result.amount_tolerance >= 0, "result amount tolerance must be nonnegative")
    comparisons = _comparison_mappings(result.comparison_fields, "result comparison_fields")
    policy = _policy_payload(result.one_to_many_policy)
    if result.mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        _require(
            policy == _policy_payload(EXACT_UNIQUE_ONE_TO_MANY_POLICY),
            "bounded result requires the complete canonical one-to-many policy",
        )
    else:
        _require(policy is None, "legacy result must not contain a one-to-many policy")
    previous_key = None
    seen_a: set[int] = set()
    seen_b: set[int] = set()
    for index, finding in enumerate(result.findings, start=1):
        label = f"finding {index}"
        _require(isinstance(finding, ReconciliationFinding), f"{label} must be a finding")
        _key(finding.key, f"{label} matching key", arity)
        if arity is None:
            arity = len(finding.key)
        _require(
            previous_key is None or previous_key < finding.key,
            f"{label} keys must be strictly ordered",
        )
        previous_key = finding.key
        _require(
            isinstance(finding.category, FindingCategory),
            f"{label} category must be a FindingCategory",
        )
        _validate_rows(finding.rows_a, Source.A, arity, seen_a, f"{label} File A")
        _validate_rows(finding.rows_b, Source.B, arity, seen_b, f"{label} File B")
        _require(bool(finding.rows_a or finding.rows_b), f"{label} must retain source evidence")
        for side in ("a", "b"):
            amount = getattr(finding, f"amount_{side}")
            _decimal(amount, f"{label} amount_{side}")
            _require(
                amount == sum_decimals(row.amount for row in getattr(finding, f"rows_{side}")),
                f"{label} amount_{side} does not equal its complete source rows",
            )
        _require(
            isinstance(finding.field_comparisons, tuple), f"{label} comparisons must be a tuple"
        )
        _require(
            len(finding.field_comparisons) == len(comparisons),
            f"{label} comparison count is inconsistent",
        )
        for number, (comparison, mapping) in enumerate(
            zip(finding.field_comparisons, comparisons, strict=True), start=1
        ):
            location = f"{label} comparison {number}"
            _require(
                isinstance(comparison, FieldComparison), f"{location} must be a FieldComparison"
            )
            _require(
                _comparison_mappings((comparison.mapping,), location)[0] == mapping,
                f"{location} mapping/order is inconsistent",
            )
            _model_invariants(comparison, FieldComparison, location)
            for value in (*comparison.values_a, *comparison.values_b):
                _text(value, f"{location} evidence")
        _validate_analysis(finding, label)
        _model_invariants(finding, ReconciliationFinding, label)
        _decimal(finding.delta, f"{label} delta")
        _require(
            finding.delta == sum_decimals((finding.amount_a, finding.amount_b.copy_negate())),
            f"{label} delta is inconsistent",
        )
        expected_exception = (
            finding.category not in (FindingCategory.EXACT_MATCH, FindingCategory.WITHIN_TOLERANCE)
            or any(
                item.status is FieldComparisonStatus.MISMATCH for item in finding.field_comparisons
            )
            or (
                finding.correspondence_analysis is not None
                and finding.correspondence_analysis.status is CorrespondenceStatus.UNIQUE_EXACT
            )
        )
        _require(
            finding.is_exception is expected_exception,
            f"{label} exception membership is inconsistent",
        )
    _normalization_payload(result.key_normalization, arity)
    _model_invariants(result, ReconciliationResult, "result")
    for side in ("a", "b"):
        total = getattr(result, f"total_{side}")
        _decimal(total, f"result total_{side}")
        _require(
            total
            == sum_decimals(getattr(finding, f"amount_{side}") for finding in result.findings),
            f"result total_{side} is inconsistent with findings",
        )
    exceptions = tuple(finding for finding in result.findings if finding.is_exception)
    tolerated = tuple(
        finding
        for finding in result.findings
        if finding.category is FindingCategory.WITHIN_TOLERANCE and not finding.is_exception
    )
    _same_population(result.exceptions, exceptions, "result exception population")
    _same_population(result.tolerated_findings, tolerated, "result tolerated population")
    control = sum_decimals((result.total_a, result.total_b.copy_negate()))
    delta_sum = sum_decimals(finding.delta for finding in result.findings)
    _require(control == delta_sum, "result accounting identity is inconsistent")
    for name, expected in (
        ("control_difference", control),
        ("finding_delta_sum", delta_sum),
        ("tolerated_delta_total", sum_decimals(finding.delta for finding in tolerated)),
    ):
        value = getattr(result, name)
        _decimal(value, f"result {name}")
        _require(value == expected, f"result {name} is inconsistent")


def _numeric_tolerance(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _configuration_payload(result: ReconciliationResult, arity: int) -> dict[str, object]:
    return {
        "reconciliation_mode": result.mode.value,
        "amount_tolerance": _numeric_tolerance(result.amount_tolerance),
        "key_normalization": _normalization_payload(result.key_normalization, arity),
        "comparison_fields": _comparison_mappings(
            result.comparison_fields, "result comparison_fields"
        ),
        "one_to_many_policy": _policy_payload(result.one_to_many_policy),
    }


def _row_payload(row: SourceRecord) -> dict[str, object]:
    return {
        "source": row.source.value,
        "source_row": row.source_row,
        "original_key": list(row.key),
        "amount": format(row.amount, "f"),
        "raw_fields": [[name, value] for name, value in row.raw_fields.items()],
    }


def _references(rows: tuple[SourceRecord, ...]) -> list[list[str | int]]:
    return [[row.source.value, row.source_row] for row in rows]


def _solution_payload(solution: SubsetSolution) -> dict[str, object]:
    return {
        "rows_a": _references(solution.rows_a),
        "rows_b": _references(solution.rows_b),
        "amount": format(solution.amount, "f"),
    }


def _analysis_payload(analysis: CorrespondenceAnalysis | None) -> dict[str, object] | None:
    if analysis is None:
        return None
    return {
        "policy": _policy_payload(analysis.policy),
        "status": analysis.status.value,
        "reason": analysis.reason.value if analysis.reason is not None else None,
        "accepted_solution": _solution_payload(analysis.accepted_solution)
        if analysis.accepted_solution is not None
        else None,
        "unassigned_rows_a": _references(analysis.unassigned_rows_a),
        "unassigned_rows_b": _references(analysis.unassigned_rows_b),
        "ambiguity_witnesses": [
            _solution_payload(witness) for witness in analysis.ambiguity_witnesses
        ],
        "planned_combinations": analysis.planned_combinations,
        "examined_combinations": analysis.examined_combinations,
        "reserved_combinations": analysis.reserved_combinations,
        "search_complete": analysis.search_complete,
    }


def _result_payload(result: ReconciliationResult, arity: int) -> dict[str, object]:
    return {
        **_configuration_payload(result, arity),
        "total_a": format(result.total_a, "f"),
        "total_b": format(result.total_b, "f"),
        "control_difference": format(result.control_difference, "f"),
        "finding_delta_sum": format(result.finding_delta_sum, "f"),
        "tolerated_delta_total": format(result.tolerated_delta_total, "f"),
        "findings": [
            {
                "matching_key": list(finding.key),
                "category": finding.category.value,
                "amount_a": format(finding.amount_a, "f"),
                "amount_b": format(finding.amount_b, "f"),
                "delta": format(finding.delta, "f"),
                "is_exception": finding.is_exception,
                "is_tolerated": finding.category is FindingCategory.WITHIN_TOLERANCE
                and not finding.is_exception,
                "rows_a": [_row_payload(row) for row in finding.rows_a],
                "rows_b": [_row_payload(row) for row in finding.rows_b],
                "field_comparisons": [
                    {
                        "mapping": {
                            "file_a": comparison.mapping.file_a,
                            "file_b": comparison.mapping.file_b,
                        },
                        "values_a": list(comparison.values_a),
                        "values_b": list(comparison.values_b),
                        "status": comparison.status.value,
                    }
                    for comparison in finding.field_comparisons
                ],
                "correspondence_analysis": _analysis_payload(finding.correspondence_analysis),
            }
            for finding in result.findings
        ],
    }


def _digest(prefix: bytes, payload: dict[str, object]) -> str:
    data = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(prefix + data).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewBinding:
    """A completed result and its exact-input fingerprint, without retaining input bytes."""

    result: ReconciliationResult
    result_fingerprint: str
    exception_keys: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.result, ReconciliationResult):
            raise TypeError("result must be a ReconciliationResult")
        _validate_result(self.result)
        _require(
            type(self.result_fingerprint) is str
            and len(self.result_fingerprint) == 64
            and all(character in "0123456789abcdef" for character in self.result_fingerprint),
            "result_fingerprint must be 64 lowercase hexadecimal characters",
        )
        _require(isinstance(self.exception_keys, tuple), "exception_keys must be a tuple")
        for key in self.exception_keys:
            _key(key, "exception key")
        _require(
            self.exception_keys == tuple(finding.key for finding in self.result.exceptions),
            "exception_keys must match the complete result exception population in order",
        )


def bind_review_result(
    result: ReconciliationResult,
    *,
    data_a: bytes,
    data_b: bytes,
    mapping: ColumnMapping,
    format_a: Literal["csv", "xlsx"],
    format_b: Literal["csv", "xlsx"],
    worksheet_a: str | None = None,
    worksheet_b: str | None = None,
) -> ReviewBinding:
    """Bind a successful run's inputs/configuration to its complete stored evidence.

    The caller supplies bytes and mappings from that same run. No ingestion,
    normalization, reconciliation, or correspondence search is repeated.
    Filenames are deliberately absent from this persistent identity contract.
    """

    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be a ReconciliationResult")
    if type(data_a) is not bytes or type(data_b) is not bytes:
        raise TypeError("data_a and data_b must be exact bytes objects")
    if not isinstance(mapping, ColumnMapping):
        raise TypeError("mapping must be a ColumnMapping")
    sources = {}
    for side, data, input_format, worksheet in (
        ("file_a", data_a, format_a, worksheet_a),
        ("file_b", data_b, format_b, worksheet_b),
    ):
        _require(
            type(input_format) is str and input_format in ("csv", "xlsx"),
            f"{side} format must be csv or xlsx",
        )
        if input_format == "csv":
            _require(worksheet is None, f"{side} CSV worksheet must be None")
        else:
            _text(worksheet, f"{side} XLSX worksheet", nonblank=True)
        sources[side] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "format": input_format,
            "worksheet": worksheet,
        }
    mapping_payload = _mapping_payload(mapping)
    arity = len(mapping.key_pairs)
    _validate_result(result, arity)
    _require(
        mapping_payload["comparison_fields"]
        == _comparison_mappings(result.comparison_fields, "result comparison_fields"),
        "mapping comparison_fields are inconsistent with result comparison_fields",
    )
    payload = {
        "reconciliation_semantics": _RECONCILIATION_SEMANTICS,
        "sources": sources,
        "mapping": mapping_payload,
        "configuration": _configuration_payload(result, arity),
        "result_digest": _digest(_RESULT_PREFIX, _result_payload(result, arity)),
    }
    return ReviewBinding(
        result,
        _digest(_BINDING_PREFIX, payload),
        tuple(finding.key for finding in result.exceptions),
    )


@dataclass(frozen=True, slots=True)
class ReviewSession:
    """A complete immutable annotation snapshot in its binding's exception order."""

    binding: ReviewBinding
    reviews: tuple[FindingReview, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ReviewBinding):
            raise TypeError("binding must be a ReviewBinding")
        if not isinstance(self.reviews, tuple) or any(
            not isinstance(review, FindingReview) for review in self.reviews
        ):
            raise TypeError("reviews must be a tuple of FindingReview objects")
        for index, review in enumerate(self.reviews, start=1):
            _model_invariants(review, FindingReview, f"review {index}")
        _require(
            tuple(review.matching_key for review in self.reviews) == self.binding.exception_keys,
            "reviews must cover exactly the exception keys in binding order",
        )

    @classmethod
    def unreviewed(cls, binding: ReviewBinding) -> "ReviewSession":
        """Create every default annotation, including a valid empty population."""

        if not isinstance(binding, ReviewBinding):
            raise TypeError("binding must be a ReviewBinding")
        return cls(
            binding,
            tuple(
                FindingReview(key, ReviewDisposition.UNREVIEWED) for key in binding.exception_keys
            ),
        )

    def with_review(self, review: FindingReview) -> "ReviewSession":
        """Return a new ordered snapshot, retaining all unaffected annotation objects."""

        if not isinstance(review, FindingReview):
            raise TypeError("review must be a FindingReview")
        for index, current in enumerate(self.reviews):
            if current.matching_key == review.matching_key:
                return ReviewSession(
                    self.binding, self.reviews[:index] + (review,) + self.reviews[index + 1 :]
                )
        raise ReviewSessionError("review matching_key does not belong to this binding's exceptions")

    @property
    def total(self) -> int:
        return len(self.reviews)

    @property
    def unreviewed_count(self) -> int:
        return sum(review.disposition is ReviewDisposition.UNREVIEWED for review in self.reviews)

    @property
    def needs_follow_up_count(self) -> int:
        return sum(
            review.disposition is ReviewDisposition.NEEDS_FOLLOW_UP for review in self.reviews
        )

    @property
    def complete_count(self) -> int:
        return sum(
            review.disposition is ReviewDisposition.REVIEW_COMPLETE for review in self.reviews
        )

    @property
    def reviewed_count(self) -> int:
        return self.total - self.unreviewed_count

    @property
    def open_count(self) -> int:
        return self.total - self.complete_count
