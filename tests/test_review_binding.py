"""Exact completed-run identity and immutable complete review snapshots."""

import inspect
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, Inexact, InvalidOperation, Rounded, localcontext
from importlib.metadata import version
from pathlib import Path
from types import MappingProxyType

import pytest

import tallydiff
import tallydiff.review as domain
from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ColumnMapping,
    ComparisonFieldMapping,
    CorrespondenceReason,
    CorrespondenceStatus,
    FindingCategory,
    FindingReview,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    ReviewBinding,
    ReviewDisposition,
    ReviewSession,
    ReviewSessionError,
    Source,
    SourceRecord,
    bind_review_result,
    export_exceptions_csv,
    reconcile,
)

MAPPING = ColumnMapping((("id", "id"),), "amount", "amount")
COMPARISONS = (
    ComparisonFieldMapping("memo_a", "memo_b"),
    ComparisonFieldMapping("date_a", "date_b"),
)
POLICY = EXACT_UNIQUE_ONE_TO_MANY_POLICY
MODE = ReconciliationMode.BOUNDED_ONE_TO_MANY

# Frozen after inspecting primitive payloads and hashing an independent stdlib-only
# construction (no imports from the product fingerprint implementation).
GOLDENS = (
    (
        ReconciliationMode.UNIQUE,
        ("3",),
        ("2",),
        "K",
        "bdc483309be5bcd559de25f0811d5e0ec42e636bfaa86bf209bd29239ea1f35e",
        "2e58062b4d7831452f3f0fc27fc719dd8fdb7cd49a080b12d30c7d376f67aa53",
    ),
    (
        ReconciliationMode.GROUPED_BY_KEY,
        ("1", "2"),
        ("2",),
        "K",
        "97b760ad43d340f882e35647c17e56df95168fda98f9d329cf0ca3780f4ed4ce",
        "dfcf3642a1ae1dcddc07c9f302b36f34a92208e656de2cbdca6ae20c0c884400",
    ),
    (
        MODE,
        ("3",),
        ("1", "2"),
        "K",
        "d1a666fdfc06693a9ba1bae00f2c6768e578cce498ba349d971545470ee3c6e6",
        "adc88eb7b86de416f53ef27a54cf5c569216b19a4d3b8c028451e2cc6c65791f",
    ),
    (
        ReconciliationMode.UNIQUE,
        ("3",),
        ("2",),
        "\u00e9",
        "d283fd978e0707f27099f634a097b18c15691273bac9c79b280595be61310227",
        "040282424b02b07888a6f362d896053fc83bb3a7275d4e3bf3270d45e54ca7b9",
    ),
)


def _record(source, ordinal, amount, key=("K",), raw_fields=None):
    if raw_fields is None:
        raw_fields = {"id": key[0], "amount": amount}
    return SourceRecord(source, ordinal, key, Decimal(amount), raw_fields)


def _result(amounts_a=("3",), amounts_b=("2",), **kwargs):
    return reconcile(
        tuple(_record(Source.A, index, amount) for index, amount in enumerate(amounts_a, 2)),
        tuple(_record(Source.B, index, amount) for index, amount in enumerate(amounts_b, 2)),
        **kwargs,
    )


def _bind(result=None, **kwargs):
    options = dict(
        data_a=b"input A", data_b=b"input B", mapping=MAPPING, format_a="csv", format_b="csv"
    )
    options.update(kwargs)
    return bind_review_result(_result() if result is None else result, **options)


def _fingerprint(result=None, **kwargs):
    return _bind(result, **kwargs).result_fingerprint


def _comparison_result(
    *, memo_a="Sales", memo_b="Purchasing", amount_b="3", comparisons=COMPARISONS
):
    left = _record(
        Source.A,
        2,
        "3",
        raw_fields={
            "memo_a": memo_a,
            "date_a": "2026-01-01",
            "memo_b": memo_a,
            "date_b": "2026-01-01",
        },
    )
    right = _record(
        Source.B,
        2,
        amount_b,
        raw_fields={
            "memo_b": memo_b,
            "date_b": "2026-01-02",
            "memo_a": memo_b,
            "date_a": "2026-01-02",
        },
    )
    result = reconcile(
        (left,), (right,), amount_tolerance=Decimal("1"), comparison_fields=comparisons
    )
    return result, replace(MAPPING, comparison_fields=comparisons)


def _multi_result():
    return reconcile(
        (
            _record(Source.A, 2, "3", ("A",)),
            _record(Source.A, 3, "5", ("B",)),
            _record(Source.A, 4, "7", ("C",)),
            _record(Source.A, 5, "2", ("clean",)),
            _record(Source.A, 6, "2", ("tolerated",)),
        ),
        (
            _record(Source.B, 2, "1", ("A",)),
            _record(Source.B, 3, "3", ("B",)),
            _record(Source.B, 4, "4", ("C",)),
            _record(Source.B, 5, "2", ("clean",)),
            _record(Source.B, 6, "1", ("tolerated",)),
        ),
        amount_tolerance=Decimal("1"),
    )


def _replace_finding(result, finding, index=0):
    findings = result.findings[:index] + (finding,) + result.findings[index + 1 :]
    return replace(result, findings=findings)


def _corrupt(value, **changes):
    for name, changed in changes.items():
        object.__setattr__(value, name, changed)
    return value


@pytest.mark.parametrize(
    "mode,amounts_a,amounts_b,key,result_digest,fingerprint",
    GOLDENS,
    ids=["unique", "grouped", "bounded", "unicode"],
)
def test_frozen_independent_result_and_binding_digests(
    mode, amounts_a, amounts_b, key, result_digest, fingerprint
):
    records = []
    inputs = []
    for source, amounts in ((Source.A, amounts_a), (Source.B, amounts_b)):
        rows = []
        for ordinal, amount in enumerate(amounts, 2):
            raw = {"id": key, "amount": amount}
            if key != "K":
                raw["memo"] = "e\u0301\r\n\U0001f600"
            rows.append(_record(source, ordinal, amount, (key,), raw))
        records.append(tuple(rows))
        inputs.append(
            ("id,amount\n" + "".join(f"{key},{amount}\n" for amount in amounts)).encode("utf-8")
        )
    result = reconcile(*records, mode=mode)
    assert domain._digest(domain._RESULT_PREFIX, domain._result_payload(result, 1)) == result_digest
    assert _fingerprint(result, data_a=inputs[0], data_b=inputs[1]) == fingerprint
    with localcontext() as context:
        context.prec = 1
        context.Emin = -1
        context.Emax = 1
        context.traps[Inexact] = context.traps[Rounded] = context.traps[InvalidOperation] = True
        context.clear_flags()
        assert _fingerprint(result, data_a=inputs[0], data_b=inputs[1]) == fingerprint
        assert not any(context.flags.values())


def test_semantics_identifier_and_domain_separation_are_fixed(monkeypatch):
    assert domain._RESULT_PREFIX == b"tallydiff-review-result-v1\0"
    assert domain._BINDING_PREFIX == b"tallydiff-review-binding-v1\0"
    assert domain._RECONCILIATION_SEMANTICS == "tallydiff-reconciliation-v1"
    original = _fingerprint()
    monkeypatch.setattr(domain, "_RECONCILIATION_SEMANTICS", "future-semantics")
    assert _fingerprint() != original


@pytest.mark.parametrize(
    "amounts_a,amounts_b,mode,category",
    [
        (("3",), ("2",), ReconciliationMode.UNIQUE, FindingCategory.AMOUNT_MISMATCH),
        (("1", "2"), ("2",), ReconciliationMode.GROUPED_BY_KEY, FindingCategory.AMOUNT_MISMATCH),
        (("3",), ("1", "2"), MODE, FindingCategory.EXACT_MATCH),
        (("3",), ("1", "2", "4"), MODE, FindingCategory.AMOUNT_MISMATCH),
        (("3",), ("1", "2", "1", "2"), MODE, FindingCategory.DUPLICATE_AMBIGUOUS),
        (("3",), ("1", "1"), MODE, FindingCategory.DUPLICATE_AMBIGUOUS),
        (("3",), ("3", "9"), MODE, FindingCategory.DUPLICATE_AMBIGUOUS),
        (("3", "2"), ("1", "2"), MODE, FindingCategory.DUPLICATE_AMBIGUOUS),
        (("3",), ("1",) * 13, MODE, FindingCategory.DUPLICATE_AMBIGUOUS),
        (("0",), ("1", "-1"), MODE, FindingCategory.EXACT_MATCH),
        (("1", "2"), ("3",), MODE, FindingCategory.EXACT_MATCH),
    ],
)
def test_binding_covers_all_modes_and_correspondence_shapes(amounts_a, amounts_b, mode, category):
    result = _result(amounts_a, amounts_b, mode=mode)
    binding = _bind(result)
    assert binding.result is result
    assert result.findings[0].category is category
    assert (
        binding.exception_keys == tuple(finding.key for finding in result.exceptions) == (("K",),)
    )
    assert len(binding.result_fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in binding.result_fingerprint)


@pytest.mark.parametrize(
    "amount_b,category",
    [("3", FindingCategory.EXACT_MATCH), ("2", FindingCategory.WITHIN_TOLERANCE)],
)
def test_secondary_only_exceptions_remain_reviewable(amount_b, category):
    result, mapping = _comparison_result(amount_b=amount_b)
    assert result.findings[0].category is category
    assert _bind(result, mapping=mapping).exception_keys == (("K",),)


@pytest.mark.parametrize(
    "result",
    [
        _result(("3",), ("3",)),
        _result((), ()),
        _result(("3",), ("2",), amount_tolerance=Decimal("1")),
    ],
)
def test_zero_exception_bindings_and_sessions(result):
    binding = _bind(result)
    session = ReviewSession.unreviewed(binding)
    assert binding.exception_keys == session.reviews == ()
    assert (
        session.total,
        session.unreviewed_count,
        session.needs_follow_up_count,
        session.complete_count,
        session.reviewed_count,
        session.open_count,
    ) == (0,) * 6


@pytest.mark.parametrize(
    "format_a,format_b", [("csv", "csv"), ("xlsx", "xlsx"), ("csv", "xlsx"), ("xlsx", "csv")]
)
def test_explicit_input_format_metadata(format_a, format_b):
    binding = _bind(
        format_a=format_a,
        format_b=format_b,
        worksheet_a=" A \u00e9 " if format_a == "xlsx" else None,
        worksheet_b="B" if format_b == "xlsx" else None,
    )
    assert binding.result is not None


def test_signature_excludes_filenames_and_repeated_binding_is_identical():
    assert tuple(inspect.signature(bind_review_result).parameters) == (
        "result",
        "data_a",
        "data_b",
        "mapping",
        "format_a",
        "format_b",
        "worksheet_a",
        "worksheet_b",
    )
    assert _bind() == _bind()
    assert _fingerprint() == _fingerprint()
    with pytest.raises(TypeError, match="unexpected keyword"):
        _bind(name_a="renamed.csv")


@pytest.mark.parametrize(
    "changes",
    [
        {"data_a": b"input a"},
        {"data_b": b"input b"},
        {"data_a": b"\xef\xbb\xbfinput A"},
        {"data_a": b"input B", "data_b": b"input A"},
        {"format_a": "xlsx", "worksheet_a": "Sheet"},
        {"format_b": "xlsx", "worksheet_b": "Sheet"},
    ],
)
def test_every_input_identity_component_changes_binding(changes):
    assert _fingerprint(**changes) != _fingerprint()


def test_worksheet_names_are_exact_and_directional():
    options = dict(format_a="xlsx", format_b="xlsx", worksheet_a="Sheet A", worksheet_b="Sheet B")
    original = _fingerprint(**options)
    for changed in (
        {"worksheet_a": "Sheet B"},
        {"worksheet_b": "Sheet A"},
        {"worksheet_a": " Sheet A "},
        {"worksheet_a": "She\u0301et A"},
    ):
        assert _fingerprint(**(options | changed)) != original


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("value", [None, "", " \t", 1, "\ud800"])
def test_xlsx_requires_a_nonblank_utf8_worksheet(side, value):
    with pytest.raises(ReviewSessionError, match="worksheet"):
        _bind(**{f"format_{side}": "xlsx", f"worksheet_{side}": value})


@pytest.mark.parametrize("side", ["a", "b"])
def test_csv_rejects_a_selected_worksheet(side):
    with pytest.raises(ReviewSessionError, match="CSV worksheet"):
        _bind(**{f"worksheet_{side}": "Sheet"})


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("value", [None, True, "CSV", "xls", "", 1])
def test_formats_are_exact_supported_strings(side, value):
    with pytest.raises(ReviewSessionError, match="format"):
        _bind(**{f"format_{side}": value})


@pytest.mark.parametrize(
    "name,value",
    [
        ("result", "result"),
        ("data_a", bytearray(b"a")),
        ("data_b", memoryview(b"b")),
        ("mapping", None),
    ],
)
def test_programmer_argument_types_raise_type_error(name, value):
    options = dict(data_a=b"a", data_b=b"b", mapping=MAPPING, format_a="csv", format_b="csv")
    result = _result()
    if name == "result":
        result = value
    else:
        options[name] = value
    with pytest.raises(TypeError):
        bind_review_result(result, **options)


def test_bytes_subclasses_are_not_accepted():
    class BytesSubclass(bytes):
        pass

    with pytest.raises(TypeError, match="exact bytes"):
        _bind(data_a=BytesSubclass(b"a"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("key_pairs", (("other", "id"),)),
        ("key_pairs", (("id", "other"),)),
        ("amount_a", "other"),
        ("amount_b", "other"),
        ("amount_a", " amount "),
    ],
)
def test_mapping_names_affect_identity(field, value):
    assert _fingerprint(mapping=replace(MAPPING, **{field: value})) != _fingerprint()


def test_key_mapping_order_is_significant():
    result = reconcile(
        (_record(Source.A, 2, "3", ("K", "V")),), (_record(Source.B, 2, "2", ("K", "V")),)
    )
    mapping = ColumnMapping((("id", "id"), ("vendor", "supplier")), "amount", "amount")
    assert _fingerprint(result, mapping=mapping) != _fingerprint(
        result, mapping=replace(mapping, key_pairs=mapping.key_pairs[::-1])
    )


@pytest.mark.parametrize(
    "comparisons",
    [
        COMPARISONS[::-1],
        tuple(ComparisonFieldMapping(item.file_b, item.file_a) for item in COMPARISONS),
    ],
)
def test_secondary_mapping_order_and_direction_affect_identity(comparisons):
    result, mapping = _comparison_result()
    changed, changed_mapping = _comparison_result(comparisons=comparisons)
    assert _fingerprint(changed, mapping=changed_mapping) != _fingerprint(result, mapping=mapping)


def test_mode_is_part_of_identity_even_with_the_same_financial_outcome():
    fingerprints = {_fingerprint(_result(mode=mode)) for mode in ReconciliationMode}
    assert len(fingerprints) == 3


@pytest.mark.parametrize(
    "values", [("0", "0.0", "0.00", "-0.00"), ("1.23", "1.2300"), ("1000", "1E+3")]
)
def test_numerically_equivalent_tolerances_bind_identically(values):
    fingerprints = {_fingerprint(_result(amount_tolerance=Decimal(value))) for value in values}
    assert len(fingerprints) == 1


def test_different_numeric_tolerance_changes_identity():
    assert _fingerprint(_result(amount_tolerance=Decimal("0.1"))) != _fingerprint(
        _result(amount_tolerance=Decimal("0.2"))
    )


def test_none_and_all_false_normalization_bind_identically():
    assert _fingerprint(_result()) == _fingerprint(
        _result(key_normalization=KeyNormalizationConfig())
    )


@pytest.mark.parametrize("component", [0, 1])
@pytest.mark.parametrize(
    "flag", ["casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros"]
)
def test_every_normalization_rule_and_component_affects_identity(component, flag):
    records = ((_record(Source.A, 2, "3", ("K", "V")),), (_record(Source.B, 2, "2", ("K", "V")),))
    mapping = ColumnMapping((("id", "id"), ("vendor", "vendor")), "amount", "amount")
    rules = [KeyNormalizationRules(), KeyNormalizationRules()]
    rules[component] = replace(rules[component], **{flag: True})
    changed = reconcile(*records, key_normalization=KeyNormalizationConfig(tuple(rules)))
    assert _fingerprint(changed, mapping=mapping) != _fingerprint(
        reconcile(*records), mapping=mapping
    )


def test_category_is_stored_evidence_not_reclassified_by_binder():
    result = _result()
    changed = _replace_finding(
        result, replace(result.findings[0], category=FindingCategory.DUPLICATE_AMBIGUOUS)
    )
    assert _fingerprint(changed) != _fingerprint(result)


@pytest.mark.parametrize(
    "original,changed", [("1.23", "1.2300"), ("0.00", "-0.00"), ("1E+3", "1000.00")]
)
def test_source_decimal_scale_and_signed_zero_are_evidence(original, changed):
    left = _record(Source.A, 2, original, raw_fields={})
    altered = replace(left, amount=Decimal(changed))
    right = _record(Source.B, 2, "0", raw_fields={})
    assert _fingerprint(reconcile((left,), (right,))) != _fingerprint(
        reconcile((altered,), (right,))
    )


@pytest.mark.parametrize("field", ["amount_a", "amount_b"])
def test_finding_amount_scale_changes_identity(field):
    result = _result()
    finding = result.findings[0]
    altered = replace(finding, **{field: Decimal(format(getattr(finding, field), "f") + ".00")})
    assert _fingerprint(_replace_finding(result, altered)) != _fingerprint(result)


@pytest.mark.parametrize("field", ["total_a", "total_b"])
def test_total_scale_changes_identity(field):
    result = _result()
    assert _fingerprint(
        replace(result, **{field: Decimal(format(getattr(result, field), "f") + ".00")})
    ) != _fingerprint(result)


@pytest.mark.parametrize("change", ["value", "order", "ordinal", "original_key"])
def test_original_source_evidence_changes_identity(change):
    result = _result()
    finding = result.findings[0]
    row = finding.rows_a[0]
    if change == "value":
        altered = replace(row, raw_fields={"id": "K", "amount": "changed original text"})
        assert altered == row  # Raw evidence is deliberately excluded from SourceRecord equality.
    elif change == "order":
        altered = replace(row, raw_fields=dict(reversed(tuple(row.raw_fields.items()))))
        assert altered == row
    elif change == "ordinal":
        altered = replace(row, source_row=7)
    else:
        altered = replace(row, key=("Original K",))
    changed = _replace_finding(result, replace(finding, rows_a=(altered,)))
    assert _fingerprint(changed) != _fingerprint(result)


def test_secondary_values_and_status_are_bound_without_recomparison():
    result, mapping = _comparison_result()
    changed, _ = _comparison_result(memo_b="Sales")
    assert _fingerprint(changed, mapping=mapping) != _fingerprint(result, mapping=mapping)
    comparison = result.findings[0].field_comparisons[0]
    altered = replace(comparison, status=domain.FieldComparisonStatus.NOT_COMPARABLE)
    finding = replace(
        result.findings[0], field_comparisons=(altered, result.findings[0].field_comparisons[1])
    )
    assert _fingerprint(_replace_finding(result, finding), mapping=mapping) != _fingerprint(
        result, mapping=mapping
    )


@pytest.mark.parametrize("key", ["clean", "tolerated"])
def test_nonreviewable_findings_are_included_in_identity(key):
    result = _multi_result()
    index = next(index for index, finding in enumerate(result.findings) if finding.key == (key,))
    finding = result.findings[index]
    assert not finding.is_exception
    altered = replace(finding.rows_a[0], raw_fields={"retained": "changed"})
    changed = _replace_finding(result, replace(finding, rows_a=(altered,)), index)
    assert _fingerprint(changed) != _fingerprint(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("planned_combinations", 8),
        ("examined_combinations", 4),
        ("reserved_combinations", 6),
        ("search_complete", True),
    ],
)
def test_every_search_counter_and_completion_flag_affects_identity(field, value):
    result = _result(("3",), ("3", "1", "2"), mode=MODE)
    finding = result.findings[0]
    analysis = finding.correspondence_analysis
    assert analysis.status is CorrespondenceStatus.AMBIGUOUS
    changed = _replace_finding(
        result, replace(finding, correspondence_analysis=replace(analysis, **{field: value}))
    )
    assert _fingerprint(changed) != _fingerprint(result)


def test_correspondence_status_and_reason_affect_identity():
    result = _result(("3",), ("1", "1"), mode=MODE)
    finding = result.findings[0]
    analysis = finding.correspondence_analysis
    changed = replace(
        analysis,
        status=CorrespondenceStatus.BOUND_EXCEEDED,
        reason=CorrespondenceReason.RUN_BUDGET_EXHAUSTED,
        search_complete=False,
    )
    assert _fingerprint(
        _replace_finding(result, replace(finding, correspondence_analysis=changed))
    ) != _fingerprint(result)
    other_reason = replace(changed, reason=CorrespondenceReason.CANDIDATE_ROW_LIMIT)
    assert _fingerprint(
        _replace_finding(result, replace(finding, correspondence_analysis=other_reason))
    ) != _fingerprint(_replace_finding(result, replace(finding, correspondence_analysis=changed)))


def test_accepted_solution_residuals_and_witness_references_are_bound():
    results = [
        _result(("3",), ("1", "2"), mode=MODE),
        _result(("3",), ("1", "2", "4"), mode=MODE),
        _result(("3",), ("1", "2", "1", "2"), mode=MODE),
    ]
    for result in results:
        finding = result.findings[0]
        analysis = finding.correspondence_analysis
        changed_row = replace(finding.rows_b[0], source_row=1)
        rows_b = (changed_row,) + finding.rows_b[1:]

        def change_references(rows, original=finding.rows_b[0], replacement=changed_row):
            return tuple(replacement if row is original else row for row in rows)

        def change_solution(solution):
            return replace(solution, rows_b=change_references(solution.rows_b))

        altered_analysis = replace(
            analysis,
            accepted_solution=change_solution(analysis.accepted_solution)
            if analysis.accepted_solution
            else None,
            unassigned_rows_b=change_references(analysis.unassigned_rows_b),
            ambiguity_witnesses=tuple(
                change_solution(witness) for witness in analysis.ambiguity_witnesses
            ),
        )
        altered = replace(finding, rows_b=rows_b, correspondence_analysis=altered_analysis)
        assert _fingerprint(_replace_finding(result, altered)) != _fingerprint(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_id", "another_policy"),
        ("max_candidate_rows", 13),
        ("minimum_accepted_subset_rows", 3),
        ("max_planned_combinations_per_run", 999_999),
        ("max_ambiguity_witnesses", 3),
        ("exact_amounts_only", False),
        ("physical_row_uniqueness", False),
        ("singleton_rivals_count", False),
    ],
)
def test_complete_policy_snapshot_affects_digest_and_noncanonical_binding_is_rejected(field, value):
    result = _result(("3",), ("1", "2"), mode=MODE)
    original = domain._result_payload(result, 1)
    changed_policy = replace(POLICY, **{field: value})
    changed = domain._result_payload(result, 1)
    changed["one_to_many_policy"] = domain._policy_payload(changed_policy)
    assert domain._digest(domain._RESULT_PREFIX, original) != domain._digest(
        domain._RESULT_PREFIX, changed
    )
    _corrupt(result, one_to_many_policy=changed_policy)
    with pytest.raises(ReviewSessionError, match="canonical"):
        _bind(result)


def test_other_keys_run_budget_metadata_changes_whole_binding():
    result = reconcile(
        (_record(Source.A, 2, "3", ("A",)), _record(Source.A, 3, "4", ("B",))),
        (
            _record(Source.B, 2, "1", ("A",)),
            _record(Source.B, 3, "2", ("A",)),
            _record(Source.B, 4, "4", ("B",)),
            _record(Source.B, 5, "1", ("B",)),
            _record(Source.B, 6, "3", ("B",)),
        ),
        mode=MODE,
    )
    finding = result.findings[1]
    changed = _replace_finding(
        result,
        replace(
            finding,
            correspondence_analysis=replace(
                finding.correspondence_analysis, examined_combinations=4
            ),
        ),
        1,
    )
    assert changed.findings[0] is result.findings[0]
    assert _fingerprint(changed) != _fingerprint(result)


def test_correspondence_reference_payloads_include_residuals_and_witnesses():
    residual = _result(("3",), ("1", "2", "4"), mode=MODE)
    payload = domain._analysis_payload(residual.findings[0].correspondence_analysis)
    assert payload["accepted_solution"] == {
        "amount": "3",
        "rows_a": [["A", 2]],
        "rows_b": [["B", 2], ["B", 3]],
    }
    assert payload["unassigned_rows_a"] == []
    assert payload["unassigned_rows_b"] == [["B", 4]]
    ambiguous = _result(("3",), ("3", "1", "2"), mode=MODE)
    payload = domain._analysis_payload(ambiguous.findings[0].correspondence_analysis)
    assert payload["accepted_solution"] is None
    assert payload["ambiguity_witnesses"] == [
        {"amount": "3", "rows_a": [["A", 2]], "rows_b": [["B", 2]]},
        {"amount": "3", "rows_a": [["A", 2]], "rows_b": [["B", 3], ["B", 4]]},
    ]


def test_subset_solution_amount_scale_changes_identity():
    result = _result(("3",), ("1", "2"), mode=MODE)
    finding = result.findings[0]
    analysis = finding.correspondence_analysis
    altered = replace(analysis.accepted_solution, amount=Decimal("3.00"))
    changed = _replace_finding(
        result,
        replace(finding, correspondence_analysis=replace(analysis, accepted_solution=altered)),
    )
    assert _fingerprint(changed) != _fingerprint(result)


def test_source_control_characters_are_evidence_not_analyst_notes():
    key = ("K\x00\x7f\x80",)
    row = _record(Source.A, 2, "3", key, {"column\x00": "\x01\x02\x7f\x80\x9f"})
    binding = _bind(reconcile((row,), ()))
    assert binding.exception_keys == (key,)
    assert ReviewSession.unreviewed(binding).reviews[0].matching_key is key


def test_unicode_evidence_is_not_normalized():
    left = _record(Source.A, 2, "3", ("\u00e9",), {"\u00e9": "e\u0301"})
    changed = replace(left, raw_fields={"\u00e9": "\u00e9"})
    assert _fingerprint(reconcile((left,), ())) != _fingerprint(reconcile((changed,), ()))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_hostile_decimal_context_does_not_change_fingerprints_or_flags(mode):
    result = _result(
        ("12345678901234567890.2300",),
        ("12345678901234567889.0000",),
        mode=mode,
        amount_tolerance=Decimal("1.2300"),
    )
    expected = _fingerprint(result)
    with localcontext() as context:
        context.prec = 1
        context.Emin = -1
        context.Emax = 1
        context.traps[Inexact] = context.traps[Rounded] = context.traps[InvalidOperation] = True
        context.clear_flags()
        assert _fingerprint(result) == expected
        assert not any(context.flags.values())


def test_binding_does_not_repeat_ingestion_normalization_reconciliation_or_search(monkeypatch):
    import tallydiff.engine as engine
    import tallydiff.ingest as ingest
    import tallydiff.normalization as normalization
    import tallydiff.subset_matching as subsets
    import tallydiff.xlsx as xlsx

    result = _result(("3",), ("1", "2"), mode=MODE)

    def forbidden(*args, **kwargs):
        pytest.fail("binding must use completed evidence")

    monkeypatch.setattr(engine, "reconcile", forbidden)
    monkeypatch.setattr(ingest, "ingest_csv", forbidden)
    monkeypatch.setattr(xlsx, "ingest_xlsx", forbidden)
    monkeypatch.setattr(normalization, "normalize_key", forbidden)
    monkeypatch.setattr(subsets, "analyze_bounded_one_to_many", forbidden)
    _bind(result)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_keys",
        "unordered",
        "arity",
        "findings_list",
        "empty_parent",
        "wrong_source",
        "duplicate_ordinal",
        "unordered_rows",
        "ordinal_type",
        "nonfinite_row",
        "row_amount_type",
        "finding_amount",
        "finding_amount_type",
        "result_total",
        "total_type",
        "category_type",
        "key_surrogate",
        "raw_surrogate",
        "raw_name_surrogate",
        "raw_mutable",
        "raw_nonstring",
        "original_key_surrogate",
        "original_key_arity",
        "tolerance_type",
        "tolerance_negative",
        "tolerance_nan",
        "mode_type",
        "normalization_arity",
        "normalization_type",
    ],
)
def test_structurally_corrupted_results_are_rejected(change):
    result = _result(("1", "2"), ("2",), mode=ReconciliationMode.GROUPED_BY_KEY)
    finding = result.findings[0]
    row = finding.rows_a[0]
    if change == "duplicate_keys":
        _corrupt(result, findings=(finding, finding))
    elif change == "unordered":
        result = _multi_result()
        _corrupt(result, findings=result.findings[::-1])
    elif change == "arity":
        _corrupt(finding, key=("K", "extra"))
    elif change == "findings_list":
        _corrupt(result, findings=list(result.findings))
    elif change == "empty_parent":
        _corrupt(finding, rows_a=(), rows_b=())
    elif change == "wrong_source":
        _corrupt(row, source=Source.B)
    elif change == "duplicate_ordinal":
        _corrupt(finding.rows_a[1], source_row=row.source_row)
    elif change == "unordered_rows":
        _corrupt(finding, rows_a=finding.rows_a[::-1])
    elif change == "ordinal_type":
        _corrupt(row, source_row=True)
    elif change == "nonfinite_row":
        _corrupt(row, amount=Decimal("NaN"))
    elif change == "row_amount_type":
        _corrupt(row, amount=1)
    elif change == "finding_amount":
        _corrupt(finding, amount_a=Decimal("4"))
    elif change == "finding_amount_type":
        _corrupt(finding, amount_a=3)
    elif change == "result_total":
        _corrupt(result, total_a=Decimal("4"))
    elif change == "total_type":
        _corrupt(result, total_a=3)
    elif change == "category_type":
        _corrupt(finding, category="amount_mismatch")
    elif change == "key_surrogate":
        _corrupt(finding, key=("\ud800",))
    elif change == "raw_surrogate":
        _corrupt(row, raw_fields=MappingProxyType({"column": "\ud800"}))
    elif change == "raw_name_surrogate":
        _corrupt(row, raw_fields=MappingProxyType({"\ud800": "value"}))
    elif change == "raw_mutable":
        _corrupt(row, raw_fields={"column": "value"})
    elif change == "raw_nonstring":
        _corrupt(row, raw_fields=MappingProxyType({"column": 1}))
    elif change == "original_key_surrogate":
        _corrupt(row, key=("\ud800",))
    elif change == "original_key_arity":
        _corrupt(row, key=("K", "extra"))
    elif change == "tolerance_type":
        _corrupt(result, amount_tolerance="0")
    elif change == "tolerance_negative":
        _corrupt(result, amount_tolerance=Decimal("-1"))
    elif change == "tolerance_nan":
        _corrupt(result, amount_tolerance=Decimal("NaN"))
    elif change == "mode_type":
        _corrupt(result, mode="grouped_by_key")
    elif change == "normalization_arity":
        _corrupt(result, key_normalization=KeyNormalizationConfig((KeyNormalizationRules(),) * 2))
    else:
        _corrupt(result, key_normalization=[])
    with pytest.raises(ReviewSessionError):
        _bind(result)


def test_duplicate_source_identity_across_findings_is_rejected():
    result = _multi_result()
    _corrupt(result.findings[1].rows_a[0], source_row=result.findings[0].rows_a[0].source_row)
    with pytest.raises(ReviewSessionError, match="duplicate source-row identity"):
        _bind(result)


@pytest.mark.parametrize("change", ["count", "order", "mapping", "value", "status", "result_order"])
def test_corrupt_comparison_configuration_and_evidence_are_rejected(change):
    result, mapping = _comparison_result()
    finding = result.findings[0]
    if change == "count":
        _corrupt(finding, field_comparisons=())
    elif change == "order":
        _corrupt(finding, field_comparisons=finding.field_comparisons[::-1])
    elif change == "mapping":
        mapping = MAPPING
    elif change == "value":
        _corrupt(finding.field_comparisons[0], values_a=("\ud800",))
    elif change == "status":
        _corrupt(finding.field_comparisons[0], status="mismatch")
    else:
        _corrupt(result, comparison_fields=result.comparison_fields[::-1])
    with pytest.raises(ReviewSessionError):
        _bind(result, mapping=mapping)


@pytest.mark.parametrize(
    "change",
    [
        "foreign_reference",
        "equal_cloned_reference",
        "invalid_reference_ordinal",
        "wrong_policy",
        "legacy_mode",
        "missing_policy",
        "missing_partition",
        "negative_counter",
        "invalid_status",
        "invalid_solution_amount",
    ],
)
def test_corrupt_correspondence_is_rejected(change):
    result = _result(("3",), ("1", "2", "4"), mode=MODE)
    finding = result.findings[0]
    analysis = finding.correspondence_analysis
    solution = analysis.accepted_solution
    if change == "foreign_reference":
        _corrupt(solution, rows_b=(_record(Source.B, 99, "1"), finding.rows_b[1]))
    elif change == "equal_cloned_reference":
        _corrupt(
            solution,
            rows_b=(
                replace(finding.rows_b[0], raw_fields={"other": "evidence"}),
                finding.rows_b[1],
            ),
        )
    elif change == "invalid_reference_ordinal":
        clone = replace(finding.rows_b[0])
        _corrupt(clone, source_row=True)
        _corrupt(solution, rows_b=(clone, finding.rows_b[1]))
    elif change == "wrong_policy":
        _corrupt(analysis, policy=replace(POLICY, max_candidate_rows=13))
    elif change == "legacy_mode":
        _corrupt(result, mode=ReconciliationMode.UNIQUE, one_to_many_policy=None)
    elif change == "missing_policy":
        _corrupt(result, one_to_many_policy=None)
    elif change == "missing_partition":
        _corrupt(analysis, unassigned_rows_b=())
    elif change == "negative_counter":
        _corrupt(analysis, reserved_combinations=-1)
    elif change == "invalid_status":
        _corrupt(analysis, status="unique_exact")
    else:
        _corrupt(solution, amount=Decimal("4"))
    with pytest.raises(ReviewSessionError):
        _bind(result)


@pytest.mark.parametrize(
    "name",
    [
        "control_difference",
        "finding_delta_sum",
        "tolerated_delta_total",
        "exceptions",
        "tolerated_findings",
    ],
)
def test_inconsistent_result_derived_contracts_are_rejected(name):
    changed_type = type(
        "CorruptedResult",
        (ReconciliationResult,),
        {
            name: property(
                lambda self: (
                    ()
                    if name == "exceptions"
                    else self.findings
                    if name == "tolerated_findings"
                    else Decimal("999")
                )
            )
        },
    )
    result = _multi_result()
    corrupted = changed_type(
        **{field.name: getattr(result, field.name) for field in fields(ReconciliationResult)}
    )
    with pytest.raises(ReviewSessionError):
        _bind(corrupted)


def test_inconsistent_finding_exception_membership_is_rejected():
    class CorruptedFinding(ReconciliationFinding):
        @property
        def is_exception(self):
            return False

    result = _result()
    finding = result.findings[0]
    changed = CorruptedFinding(
        **{field.name: getattr(finding, field.name) for field in fields(ReconciliationFinding)}
    )
    with pytest.raises(ReviewSessionError, match="exception membership"):
        _bind(_replace_finding(result, changed))


@pytest.mark.parametrize(
    "mapping",
    [
        ColumnMapping((), "amount", "amount"),
        ColumnMapping(((None, "id"),), "amount", "amount"),
        ColumnMapping((("id", "id"),), None, "amount"),
        ColumnMapping((("\ud800", "id"),), "amount", "amount"),
        ColumnMapping((("id", "id"), ("id", "other")), "amount", "amount"),
    ],
)
def test_incomplete_or_invalid_mapping_is_rejected(mapping):
    with pytest.raises(ReviewSessionError, match="mapping"):
        _bind(mapping=mapping)


def test_validation_errors_do_not_echo_financial_evidence():
    secret = "confidential-client-key"
    result = reconcile((_record(Source.A, 2, "3", (secret,)),), ())
    _corrupt(result.findings[0], amount_a=Decimal("4"))
    with pytest.raises(ReviewSessionError) as error:
        _bind(result)
    assert "finding 1" in str(error.value)
    assert secret not in str(error.value)


@pytest.mark.parametrize("value", ["", "A" * 64, "g" * 64, "0" * 63, "0" * 65, None])
def test_binding_rejects_invalid_fingerprint_shape(value):
    binding = _bind()
    with pytest.raises(ReviewSessionError, match="64 lowercase"):
        replace(binding, result_fingerprint=value)


@pytest.mark.parametrize("keys", [[], (), (("other",),), (("K",), ("K",)), (["K"],)])
def test_binding_requires_exact_exception_key_population(keys):
    with pytest.raises(ReviewSessionError):
        replace(_bind(), exception_keys=keys)


def test_default_session_is_full_ordered_and_bound():
    binding = _bind(_multi_result())
    session = ReviewSession.unreviewed(binding)
    assert [field.name for field in fields(ReviewBinding)] == [
        "result",
        "result_fingerprint",
        "exception_keys",
    ]
    assert [field.name for field in fields(ReviewSession)] == ["binding", "reviews"]
    assert session.binding is binding
    assert (
        tuple(review.matching_key for review in session.reviews)
        == binding.exception_keys
        == (("A",), ("B",), ("C",))
    )
    assert all(
        review.disposition is ReviewDisposition.UNREVIEWED and review.note == ""
        for review in session.reviews
    )


@pytest.mark.parametrize("disposition", list(ReviewDisposition))
def test_functional_replacement_preserves_other_objects_and_binding(disposition):
    session = ReviewSession.unreviewed(_bind(_multi_result()))
    annotation = FindingReview(("B",), disposition, " \tUnicode \u00e9\r\n=1+2 ")
    changed = session.with_review(annotation)
    assert changed is not session
    assert changed.binding is session.binding
    assert changed.reviews[1] is annotation
    assert changed.reviews[0] is session.reviews[0]
    assert changed.reviews[2] is session.reviews[2]
    assert session.reviews[1].disposition is ReviewDisposition.UNREVIEWED
    assert session.reviews[1].note == ""


@pytest.mark.parametrize("key", [("unknown",), ("clean",), ("tolerated",)])
def test_functional_replacement_rejects_nonexception_keys(key):
    session = ReviewSession.unreviewed(_bind(_multi_result()))
    with pytest.raises(ReviewSessionError):
        session.with_review(FindingReview(key, ReviewDisposition.REVIEW_COMPLETE))


@pytest.mark.parametrize("change", ["missing", "duplicate", "reordered", "extra", "other_binding"])
def test_session_rejects_invalid_full_population_without_sorting(change):
    binding = _bind(_multi_result())
    reviews = ReviewSession.unreviewed(binding).reviews
    if change == "missing":
        reviews = reviews[:-1]
    elif change == "duplicate":
        reviews = (reviews[0], reviews[0], reviews[2])
    elif change == "reordered":
        reviews = reviews[::-1]
    elif change == "extra":
        reviews = reviews + (FindingReview(("clean",), ReviewDisposition.UNREVIEWED),)
    else:
        binding = _bind()
    with pytest.raises(ReviewSessionError, match="binding order"):
        ReviewSession(binding, reviews)


@pytest.mark.parametrize(
    "value", [[], [FindingReview(("K",), ReviewDisposition.UNREVIEWED)], (None,)]
)
def test_session_requires_tuple_of_finding_reviews(value):
    with pytest.raises(TypeError):
        ReviewSession(_bind(), value)


def test_session_programmer_types_and_corrupted_annotation_are_rejected():
    with pytest.raises(TypeError):
        ReviewSession(None, ())
    with pytest.raises(TypeError):
        ReviewSession.unreviewed(None)
    session = ReviewSession.unreviewed(_bind())
    with pytest.raises(TypeError):
        session.with_review("annotation")
    annotation = FindingReview(("K",), ReviewDisposition.UNREVIEWED)
    _corrupt(annotation, note="\x00")
    with pytest.raises(ReviewSessionError):
        session.with_review(annotation)


def test_progress_counts_have_exact_locked_meanings():
    session = ReviewSession.unreviewed(_bind(_multi_result()))
    session = session.with_review(FindingReview(("A",), ReviewDisposition.NEEDS_FOLLOW_UP))
    session = session.with_review(FindingReview(("B",), ReviewDisposition.REVIEW_COMPLETE))
    session = session.with_review(
        FindingReview(("C",), ReviewDisposition.UNREVIEWED, "A note alone is not a decision")
    )
    assert (
        session.total,
        session.unreviewed_count,
        session.needs_follow_up_count,
        session.complete_count,
        session.reviewed_count,
        session.open_count,
    ) == (3, 1, 1, 1, 2, 2)
    assert (
        session.total
        == session.unreviewed_count + session.needs_follow_up_count + session.complete_count
    )
    assert (
        session.reviewed_count
        == session.needs_follow_up_count + session.complete_count
        == session.total - session.unreviewed_count
    )
    assert (
        session.open_count
        == session.unreviewed_count + session.needs_follow_up_count
        == session.total - session.complete_count
    )


def test_binding_and_session_are_frozen_slotted_and_hashable():
    binding = _bind()
    session = ReviewSession.unreviewed(binding)
    assert binding == _bind()
    assert session == ReviewSession.unreviewed(_bind())
    assert hash(binding) == hash(_bind())
    assert hash(session) == hash(ReviewSession.unreviewed(_bind()))
    for value, field in ((binding, "result_fingerprint"), (session, "reviews")):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)
        with pytest.raises(AttributeError):
            object.__setattr__(value, "unexpected", [])


def test_display_key_collisions_stay_distinct_in_a_complete_session():
    keys = (("A", "B / C"), ("A / B", "C"))
    result = reconcile(
        tuple(_record(Source.A, index, "3", key) for index, key in enumerate(keys, 2)), ()
    )
    mapping = ColumnMapping((("id", "id"), ("other", "other")), "amount", "amount")
    session = ReviewSession.unreviewed(_bind(result, mapping=mapping))
    changed = session.with_review(FindingReview(keys[1], ReviewDisposition.REVIEW_COMPLETE))
    assert changed.reviews[0] is session.reviews[0]
    assert changed.reviews[1].matching_key == keys[1]


@pytest.mark.parametrize("kind", ["mixed", "secondary", "inferred", "residual", "ambiguous"])
def test_review_changes_leave_all_reconciliation_evidence_and_original_csv_unchanged(kind):
    mapping = MAPPING
    if kind == "mixed":
        result = _multi_result()
    elif kind == "secondary":
        result, mapping = _comparison_result(amount_b="2")
    else:
        amounts = {
            "inferred": ("1", "2"),
            "residual": ("1", "2", "4"),
            "ambiguous": ("1", "2", "1", "2"),
        }[kind]
        result = _result(("3",), amounts, mode=MODE)
    before = domain._digest(domain._RESULT_PREFIX, domain._result_payload(result, 1))
    csv = export_exceptions_csv(result)
    finding_objects = result.findings
    evidence = tuple(
        row.raw_fields for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)
    )
    binding = _bind(result, mapping=mapping)
    session = ReviewSession.unreviewed(binding)
    for disposition in ReviewDisposition:
        for key in binding.exception_keys:
            session = session.with_review(FindingReview(key, disposition, " explanation\r\n=1+2 "))
        assert result.findings is finding_objects
        assert domain._digest(domain._RESULT_PREFIX, domain._result_payload(result, 1)) == before
        assert export_exceptions_csv(result) == csv
        assert _fingerprint(result, mapping=mapping) == binding.result_fingerprint
        assert all(
            row.raw_fields is original
            for row, original in zip(
                (row for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)),
                evidence,
                strict=True,
            )
        )


def test_stage_two_public_api_is_additive_and_package_release_version_matches():
    for name in (
        "ReviewDisposition",
        "FindingReview",
        "ReviewSessionError",
        "ReviewBinding",
        "ReviewSession",
        "bind_review_result",
    ):
        assert name in tallydiff.__all__
        assert getattr(tallydiff, name) is getattr(domain, name)
    assert version("tallydiff") == "0.7.0"
    assert (
        'version = "0.7.0"' in (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
