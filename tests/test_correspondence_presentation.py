"""Conditional correspondence summaries and policy-aware configuration identity."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from inspect import signature

import pytest

import tallydiff.presentation as presentation
from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ColumnMapping,
    ComparisonFieldMapping,
    CorrespondenceReason,
    CorrespondenceStatus,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    Source,
    SourceRecord,
    reconcile,
)
from tallydiff.presentation import (
    CORRESPONDENCE_REASON_LABELS,
    CORRESPONDENCE_STATUS_LABELS,
    configuration_id,
    finding_rows,
)

MODE = ReconciliationMode.BOUNDED_ONE_TO_MANY
MAPPING = ColumnMapping((("id", "ref"), ("invoice", "document")), "amount", "gross")
COMPARISONS = (
    ComparisonFieldMapping("department", "cost_center"),
    ComparisonFieldMapping("currency", "currency_code"),
)
ACTIVE = KeyNormalizationConfig(
    (KeyNormalizationRules(casefold=True), KeyNormalizationRules(strip_leading_zeros=True))
)


def _identity(mode, mapping=MAPPING, normalization=None):
    return configuration_id(
        b"a",
        b"b",
        mapping,
        name_a="a.csv",
        name_b="b.csv",
        reconciliation_mode=mode,
        key_normalization=normalization,
    )


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("normalization", [None, ACTIVE])
@pytest.mark.parametrize("comparisons", [(), COMPARISONS])
def test_policy_component_follows_existing_optional_components_only_for_bounded(
    monkeypatch, mode, normalization, comparisons
):
    captured = []
    original = json.dumps

    def capture(value, **kwargs):
        captured.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(presentation.json, "dumps", capture)
    mapping = replace(MAPPING, comparison_fields=comparisons)
    digest = _identity(mode, mapping, normalization)
    assert digest == _identity(mode, mapping, normalization)
    identity = captured[0]
    assert identity[:7] == [
        ["a.csv", hashlib.sha256(b"a").hexdigest(), None],
        ["b.csv", hashlib.sha256(b"b").hexdigest(), None],
        MAPPING.key_pairs,
        "amount",
        "gross",
        "0",
        mode.value,
    ]
    optional_names = [next(iter(component)) for component in identity[7:]]
    assert optional_names == (
        (["key_normalization"] if normalization else [])
        + (["comparison_fields"] if comparisons else [])
        + (["one_to_many_policy"] if mode is MODE else [])
    )
    if mode is MODE:
        assert identity[-1] == {"one_to_many_policy": "exact_unique_v1"}
    assert digest == hashlib.sha256(original(identity, ensure_ascii=True).encode()).hexdigest()
    assert "one_to_many_policy" not in signature(configuration_id).parameters


def test_bounded_identity_differs_from_legacy_and_preserves_configuration_order():
    mapping = replace(MAPPING, comparison_fields=COMPARISONS)
    bounded = _identity(MODE, mapping, ACTIVE)
    assert bounded == _identity(MODE, mapping, ACTIVE)
    assert len({_identity(mode, mapping, ACTIVE) for mode in ReconciliationMode}) == 3
    assert bounded != _identity(MODE, replace(mapping, comparison_fields=COMPARISONS[::-1]), ACTIVE)
    assert bounded != _identity(MODE, mapping, KeyNormalizationConfig(ACTIVE.component_rules[::-1]))
    assert bounded != _identity(MODE, replace(mapping, key_pairs=MAPPING.key_pairs[::-1]), ACTIVE)


def test_policy_identifier_changes_only_bounded_identity(monkeypatch):
    before = {mode: _identity(mode) for mode in ReconciliationMode}
    monkeypatch.setattr(
        presentation,
        "EXACT_UNIQUE_ONE_TO_MANY_POLICY",
        replace(EXACT_UNIQUE_ONE_TO_MANY_POLICY, policy_id="future_policy"),
    )
    after = {mode: _identity(mode) for mode in ReconciliationMode}
    for mode in (ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY):
        assert before[mode] == after[mode]
    assert before[MODE] != after[MODE]


def test_correspondence_labels_cover_every_status_and_reason_deterministically():
    assert CORRESPONDENCE_STATUS_LABELS == {
        CorrespondenceStatus.UNIQUE_EXACT: "Unique exact subset",
        CorrespondenceStatus.AMBIGUOUS: "Ambiguous exact subsets",
        CorrespondenceStatus.NO_EXACT_SUBSET: "No exact subset",
        CorrespondenceStatus.SINGLETON_ONLY: "Singleton match only",
        CorrespondenceStatus.BOUND_EXCEEDED: "Search bound exceeded",
        CorrespondenceStatus.NOT_ELIGIBLE: "Not eligible for subset search",
    }
    assert CORRESPONDENCE_REASON_LABELS == {
        CorrespondenceReason.BOTH_SIDES_MULTIPLE: "Multiple records on both sides",
        CorrespondenceReason.MISSING_OPPOSITE_SIDE: "Missing records on the opposite side",
        CorrespondenceReason.CANDIDATE_ROW_LIMIT: "Candidate row limit exceeded",
        CorrespondenceReason.RUN_BUDGET_EXHAUSTED: "Run search budget exhausted",
    }
    assert set(CORRESPONDENCE_STATUS_LABELS) == set(CorrespondenceStatus)
    assert set(CORRESPONDENCE_REASON_LABELS) == set(CorrespondenceReason)


@pytest.mark.parametrize("comparisons", [(), (COMPARISONS[0],)])
def test_finding_rows_adds_only_status_after_secondary_columns_for_mixed_analysis(comparisons):
    rows_a = (
        SourceRecord(Source.A, 2, ("inferred",), Decimal("300"), {"department": "Sales"}),
        SourceRecord(Source.A, 3, ("ordinary",), Decimal("10"), {"department": "Sales"}),
    )
    rows_b = (
        SourceRecord(Source.B, 2, ("inferred",), Decimal("100"), {"cost_center": "Sales"}),
        SourceRecord(Source.B, 3, ("inferred",), Decimal("200"), {"cost_center": "Sales"}),
        SourceRecord(Source.B, 4, ("ordinary",), Decimal("11"), {"cost_center": "Finance"}),
    )
    findings = reconcile(rows_a, rows_b, mode=MODE, comparison_fields=comparisons).findings
    ordinary = finding_rows(findings[1:])[0]
    assert ordinary == {
        "Category": "Amount mismatch",
        "Matching key": "ordinary",
        "File A amount": "10",
        "File B amount": "11",
        "Delta (A - B)": "-1",
        "File A records": "3",
        "File B records": "4",
        **({"Secondary differences": "department ↔ cost_center"} if comparisons else {}),
    }
    summaries = finding_rows(findings)
    assert summaries[1] == {**ordinary, "Correspondence status": ""}
    assert summaries[0] == {
        "Category": "Exact match",
        "Matching key": "inferred",
        "File A amount": "300",
        "File B amount": "300",
        "Delta (A - B)": "0",
        "File A records": "2",
        "File B records": "2, 3",
        **({"Secondary differences": ""} if comparisons else {}),
        "Correspondence status": "Unique exact subset",
    }
    assert list(summaries[0])[-1] == "Correspondence status"
    assert all(list(summary) == list(summaries[0]) for summary in summaries)
    assert finding_rows(()) == []
