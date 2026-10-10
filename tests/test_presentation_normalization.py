"""Normalization identity remains independent of Streamlit and display labels."""

import json
from decimal import Decimal

import pytest

from tallydiff import (
    ColumnMapping,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    export_mapping_profile,
    load_mapping_profile,
)
from tallydiff.presentation import NORMALIZATION_LABELS, configuration_id

MAPPING = ColumnMapping((("vendor", "supplier"), ("invoice", "ref")), "amount", "gross")
FIELDS = ("casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros")


def _identity(config=None, **kwargs):
    return configuration_id(
        b"a", b"b", MAPPING, name_a="a.csv", name_b="b.csv", key_normalization=config, **kwargs
    )


def test_exact_identity_preserves_previous_digest_and_canonical_all_false() -> None:
    previous_digest = "94542dd48c1f6cb87e499b3f220a446c5b9f00489c72cc460be1df7fe896049a"
    exact = KeyNormalizationConfig((KeyNormalizationRules(), KeyNormalizationRules()))
    assert _identity() == _identity(None) == _identity(exact) == previous_digest
    assert configuration_id(b"a", b"b", MAPPING, name_a="a.csv", name_b="b.csv") == previous_digest


@pytest.mark.parametrize("field", FIELDS)
def test_each_rule_and_owning_component_affect_identity(field: str) -> None:
    active = KeyNormalizationRules(**{field: True})
    left = KeyNormalizationConfig((active, KeyNormalizationRules()))
    right = KeyNormalizationConfig((KeyNormalizationRules(), active))
    equivalent = KeyNormalizationConfig(
        (KeyNormalizationRules(**{field: True}), KeyNormalizationRules())
    )
    assert _identity(left) == _identity(equivalent)
    assert len({_identity(), _identity(left), _identity(right)}) == 3


@pytest.mark.parametrize("field", FIELDS)
def test_changing_one_rule_with_other_rules_active_changes_identity(field: str) -> None:
    enabled = dict.fromkeys(FIELDS, True)
    changed = {**enabled, field: False}
    baseline = KeyNormalizationConfig((KeyNormalizationRules(**enabled), KeyNormalizationRules()))
    updated = KeyNormalizationConfig((KeyNormalizationRules(**changed), KeyNormalizationRules()))
    assert _identity(updated) != _identity(baseline)


def test_active_components_keep_order_and_exact_slots() -> None:
    a = KeyNormalizationRules(casefold=True, remove_punctuation=True)
    b = KeyNormalizationRules(strip_leading_zeros=True)
    assert _identity(KeyNormalizationConfig((a, b))) != _identity(KeyNormalizationConfig((b, a)))
    assert _identity(KeyNormalizationConfig((a, b))) != _identity(
        KeyNormalizationConfig((a, KeyNormalizationRules()))
    )


def test_identity_uses_primitive_rules_independent_of_labels(monkeypatch) -> None:
    config = KeyNormalizationConfig((KeyNormalizationRules(casefold=True), KeyNormalizationRules()))
    expected = _identity(config)
    for field in FIELDS:
        monkeypatch.setitem(NORMALIZATION_LABELS, field, f"Different display label: {field}")
    assert _identity(config) == expected


@pytest.mark.parametrize("invalid", [(), {}, "casefold", True, 1, KeyNormalizationRules()])
def test_identity_rejects_invalid_config_type(invalid) -> None:
    with pytest.raises(TypeError, match="KeyNormalizationConfig"):
        _identity(invalid)


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("active", [False, True])
def test_identity_rejects_wrong_component_count_even_if_exact(count: int, active: bool) -> None:
    config = KeyNormalizationConfig(
        tuple(KeyNormalizationRules(casefold=active) for _ in range(count))
    )
    with pytest.raises(ValueError, match="key_pairs"):
        _identity(config)


def test_profile_round_trip_and_manual_configuration_share_identity() -> None:
    config = KeyNormalizationConfig(
        (KeyNormalizationRules(casefold=True), KeyNormalizationRules(strip_leading_zeros=True))
    )
    profile_data = export_mapping_profile(
        MAPPING,
        key_normalization=config,
        amount_tolerance=Decimal("0.0100"),
        reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY,
    )
    profile = load_mapping_profile(profile_data)
    assert json.loads(profile_data)["version"] == 5
    assert json.loads(profile_data)["one_to_many_policy"] is None
    assert _identity(
        config,
        amount_tolerance=Decimal("0.01"),
        reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY,
    ) == _identity(
        profile.key_normalization,
        amount_tolerance=profile.amount_tolerance,
        reconciliation_mode=profile.reconciliation_mode,
    )
