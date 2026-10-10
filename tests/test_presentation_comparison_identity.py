"""Secondary editing values participate in identity without changing legacy digests."""

import hashlib
import inspect
import json
from dataclasses import replace
from decimal import Decimal

import pytest

import tallydiff.presentation as presentation
from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    Source,
    SourceRecord,
    export_mapping_profile,
    load_mapping_profile,
    reconcile,
)
from tallydiff.presentation import configuration_id, review_exceptions

MAPPING = ColumnMapping((("id", "ref"),), "amount", "gross")
DEPARTMENT = ComparisonFieldMapping("Department", "Cost Center")
CURRENCY = ComparisonFieldMapping("Currency", "Currency Code")
ACTIVE = KeyNormalizationConfig((KeyNormalizationRules(casefold=True, remove_punctuation=True),))
EXACT = KeyNormalizationConfig((KeyNormalizationRules(),))


def _identity(mapping=MAPPING, normalization=None, **kwargs):
    options = {"name_a": "a.csv", "name_b": "b.csv", **kwargs}
    return configuration_id(b"a", b"b", mapping, key_normalization=normalization, **options)


@pytest.mark.parametrize(
    "normalization,expected",
    [
        (None, "21571386499163214f24658dc0194953102a80a80ae21faf91fd98d6ad4b1f34"),
        (EXACT, "21571386499163214f24658dc0194953102a80a80ae21faf91fd98d6ad4b1f34"),
        (ACTIVE, "22f3d90ad7111486433fb8ebd14ebf9df48870989b55415b390dad764a3fc0c8"),
    ],
)
def test_empty_comparisons_preserve_fixed_pre_v05_digests(normalization, expected):
    explicit = ColumnMapping(
        MAPPING.key_pairs, MAPPING.amount_a, MAPPING.amount_b, comparison_fields=()
    )
    assert _identity(MAPPING, normalization) == _identity(explicit, normalization) == expected


@pytest.mark.parametrize("normalization", [None, EXACT, ACTIVE])
def test_nonempty_identity_appends_exact_primitives_after_optional_normalization(
    monkeypatch, normalization
):
    mapping = replace(MAPPING, comparison_fields=(CURRENCY, DEPARTMENT))
    captured = []
    original_dumps = json.dumps

    def capture(value, **kwargs):
        captured.append((value, kwargs))
        return original_dumps(value, **kwargs)

    monkeypatch.setattr(presentation.json, "dumps", capture)
    digest = _identity(mapping, normalization)
    expected = [
        ["a.csv", hashlib.sha256(b"a").hexdigest(), None],
        ["b.csv", hashlib.sha256(b"b").hexdigest(), None],
        (("id", "ref"),),
        "amount",
        "gross",
        "0",
        "unique",
    ]
    if normalization is ACTIVE:
        expected.append(
            {
                "key_normalization": [
                    {
                        "casefold": True,
                        "collapse_whitespace": False,
                        "remove_punctuation": True,
                        "strip_leading_zeros": False,
                    }
                ]
            }
        )
    expected.append(
        {"comparison_fields": [["Currency", "Currency Code"], ["Department", "Cost Center"]]}
    )
    assert captured == [(expected, {"ensure_ascii": True})]
    assert (
        digest
        == hashlib.sha256(original_dumps(expected, ensure_ascii=True).encode("utf-8")).hexdigest()
    )


def test_every_secondary_edit_has_a_distinct_identity_without_canonicalizing_names():
    configurations = [
        (),
        (ComparisonFieldMapping(None, None),),
        (ComparisonFieldMapping("", None),),
        (ComparisonFieldMapping(None, ""),),
        (ComparisonFieldMapping("", ""),),
        (ComparisonFieldMapping(" ", ""),),
        (ComparisonFieldMapping("Department", None),),
        (ComparisonFieldMapping(None, "Cost Center"),),
        (DEPARTMENT,),
        (ComparisonFieldMapping("Division", "Cost Center"),),
        (ComparisonFieldMapping("Department", "Department Code"),),
        (ComparisonFieldMapping("department", "Cost Center"),),
        (ComparisonFieldMapping(" Department ", "Cost Center"),),
        (ComparisonFieldMapping("Department", " Cost Center "),),
        (ComparisonFieldMapping("é", "Cost Center"),),
        (ComparisonFieldMapping("e\u0301", "Cost Center"),),
        (ComparisonFieldMapping("Cost Center", "Department"),),
        (DEPARTMENT, CURRENCY),
        (CURRENCY, DEPARTMENT),
        (DEPARTMENT, DEPARTMENT),
    ]
    identities = [
        _identity(replace(MAPPING, comparison_fields=fields)) for fields in configurations
    ]
    assert len(set(identities)) == len(configurations)
    assert identities == [
        _identity(replace(MAPPING, comparison_fields=fields)) for fields in configurations
    ]


@pytest.mark.parametrize(
    "fields",
    [
        (ComparisonFieldMapping(None, None),),
        (ComparisonFieldMapping("Department", None),),
        (ComparisonFieldMapping(None, "Cost Center"),),
        (ComparisonFieldMapping("", " \t"),),
        (ComparisonFieldMapping("amount", "gross"),),
        (DEPARTMENT, DEPARTMENT),
    ],
)
def test_editing_identity_never_requires_mapping_completeness_or_problem_validation(
    monkeypatch, fields
):
    def forbidden(*args, **kwargs):
        pytest.fail("configuration_id must encode editable selections without validating readiness")

    monkeypatch.setattr(ColumnMapping, "problem", forbidden)
    mapping = replace(MAPPING, comparison_fields=fields)
    digest = _identity(mapping, ACTIVE)
    assert digest == _identity(
        replace(
            mapping,
            comparison_fields=tuple(
                ComparisonFieldMapping(field.file_a, field.file_b) for field in fields
            ),
        ),
        ACTIVE,
    )
    assert digest != _identity(MAPPING, ACTIVE)


@pytest.mark.parametrize("normalization", [None, EXACT, ACTIVE])
def test_equivalent_comparison_instances_produce_identical_identity(normalization):
    left = replace(MAPPING, comparison_fields=(DEPARTMENT, CURRENCY))
    right = replace(
        MAPPING,
        comparison_fields=(
            ComparisonFieldMapping("Department", "Cost Center"),
            ComparisonFieldMapping("Currency", "Currency Code"),
        ),
    )
    assert left.comparison_fields[0] is not right.comparison_fields[0]
    assert _identity(left, normalization) == _identity(right, normalization)


def test_comparison_identity_uses_no_display_labels_or_dataclass_repr(monkeypatch):
    mapping = replace(MAPPING, comparison_fields=(DEPARTMENT, CURRENCY))
    expected = _identity(mapping, ACTIVE, reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY)
    for labels in (
        presentation.CATEGORY_LABELS,
        presentation.MODE_LABELS,
        presentation.NORMALIZATION_LABELS,
        presentation.EXCEPTION_SORT_LABELS,
    ):
        for key in labels:
            monkeypatch.setitem(labels, key, "changed display label")

    def forbidden(*args, **kwargs):
        pytest.fail("identity must use primitive values, not repr")

    monkeypatch.setattr(ComparisonFieldMapping, "__repr__", forbidden)
    assert (
        _identity(mapping, ACTIVE, reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY)
        == expected
    )


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("normalization", [None, EXACT, ACTIVE])
def test_profile_manual_identity_matches_with_files_worksheets_and_json_formatting(
    mode, normalization
):
    mapping = replace(MAPPING, comparison_fields=(CURRENCY, DEPARTMENT))
    data = export_mapping_profile(
        mapping,
        amount_tolerance=Decimal("0.0100"),
        reconciliation_mode=mode,
        key_normalization=normalization,
    )
    documents = [
        data,
        json.dumps(json.loads(data), sort_keys=True, separators=(",", ":")),
        json.dumps(json.loads(data), indent=4),
    ]
    options = {
        "name_a": "source-a.xlsx",
        "name_b": "source-b.xlsx",
        "worksheet_a": "Ledger A",
        "worksheet_b": "Ledger B",
        "reconciliation_mode": mode,
        "amount_tolerance": Decimal("0.01"),
    }
    expected = _identity(mapping, normalization, **options)
    for document in documents:
        profile = load_mapping_profile(document)
        assert profile.mapping.comparison_fields == mapping.comparison_fields
        assert (
            _identity(
                profile.mapping,
                profile.key_normalization,
                **{**options, "amount_tolerance": profile.amount_tolerance},
            )
            == expected
        )
    assert _identity(mapping, normalization, **{**options, "worksheet_a": "Other"}) != expected
    assert (
        _identity(mapping, normalization, **{**options, "name_a": "changed-source.xlsx"})
        != expected
    )


def test_review_filters_remain_outside_configuration_identity_and_preserve_mapping():
    mapping = replace(MAPPING, comparison_fields=(DEPARTMENT,))
    a = (SourceRecord(Source.A, 2, ("INV",), Decimal("1"), {"Department": "Sales"}),)
    b = (SourceRecord(Source.B, 7, ("INV",), Decimal("1"), {"Cost Center": "Marketing"}),)
    result = reconcile(a, b, comparison_fields=mapping.comparison_fields)
    before = _identity(mapping)
    assert review_exceptions(result.findings, secondary_mismatches_only=True) == result.exceptions
    assert (
        review_exceptions(
            result.findings,
            query="absent",
            categories=(FindingCategory.EXACT_MATCH,),
            minimum_abs_delta=Decimal("0.01"),
            sort_order="absolute_delta_desc",
            secondary_mismatches_only=True,
        )
        == ()
    )
    assert _identity(mapping) == before
    assert result.comparison_fields is mapping.comparison_fields
    assert list(inspect.signature(configuration_id).parameters) == [
        "data_a",
        "data_b",
        "mapping",
        "name_a",
        "name_b",
        "amount_tolerance",
        "reconciliation_mode",
        "worksheet_a",
        "worksheet_b",
        "key_normalization",
    ]
