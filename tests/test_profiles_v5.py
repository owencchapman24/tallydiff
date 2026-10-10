"""Fixed policy binding in current profiles and explicit historical migration."""

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from inspect import signature

import pytest

from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ColumnMapping,
    ComparisonFieldMapping,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    MappingProfile,
    ProfileError,
    ReconciliationMode,
    export_mapping_profile,
    load_mapping_profile,
)

MAPPING = ColumnMapping((("id", "ref"), ("invoice", "document")), "amount", "gross")
ACTIVE = KeyNormalizationConfig(
    (KeyNormalizationRules(casefold=True), KeyNormalizationRules(strip_leading_zeros=True))
)
COMPARISONS = (
    ComparisonFieldMapping("currency", "currency_code"),
    ComparisonFieldMapping("department", "cost_center"),
    ComparisonFieldMapping("date", "posting_date"),
)
FIELDS = (
    "format",
    "version",
    "key_pairs",
    "amount_columns",
    "amount_tolerance",
    "reconciliation_mode",
    "key_normalization",
    "comparison_fields",
    "one_to_many_policy",
)
V4_PROFILE = b"""{
  "format": "tallydiff-mapping-profile",
  "version": 4,
  "key_pairs": [{"file_a": "id", "file_b": "ref"}, {"file_a": "invoice", "file_b": "document"}],
  "amount_columns": {"file_a": "amount", "file_b": "gross"},
  "amount_tolerance": "0.000000000000000012300",
  "reconciliation_mode": "grouped_by_key",
  "key_normalization": [
    {"casefold": true, "collapse_whitespace": false,
     "remove_punctuation": false, "strip_leading_zeros": false},
    {"casefold": false, "collapse_whitespace": false,
     "remove_punctuation": false, "strip_leading_zeros": true}
  ],
  "comparison_fields": [
    {"file_a": "currency", "file_b": "currency_code"},
    {"file_a": "department", "file_b": "cost_center"},
    {"file_a": "date", "file_b": "posting_date"}
  ]
}
"""


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_mode_alone_derives_immutable_policy_without_changing_positional_api(mode):
    profile = MappingProfile(MAPPING, Decimal("0.0100"), mode, ACTIVE)
    expected = (
        EXACT_UNIQUE_ONE_TO_MANY_POLICY if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY else None
    )
    assert profile.one_to_many_policy is expected
    assert "one_to_many_policy" not in signature(MappingProfile).parameters
    with pytest.raises((FrozenInstanceError, TypeError, AttributeError)):
        profile.one_to_many_policy = None


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("normalization", [None, ACTIVE])
@pytest.mark.parametrize("comparisons", [(), COMPARISONS])
@pytest.mark.parametrize(
    "tolerance", ["-0.0000", "0.0000000000000000000012300", "12345678901234567890.00000000000100"]
)
def test_v5_round_trip_preserves_all_configuration_and_decimal_digits(
    mode, normalization, comparisons, tolerance
):
    mapping = replace(MAPPING, comparison_fields=comparisons)
    with localcontext() as context:
        context.prec = 1
        data = export_mapping_profile(
            mapping,
            amount_tolerance=Decimal(tolerance),
            reconciliation_mode=mode,
            key_normalization=normalization,
        )
        profile = load_mapping_profile(data)
    document = json.loads(data)
    assert tuple(document) == FIELDS
    assert document["version"] == 5
    assert document["amount_tolerance"] == tolerance
    assert document["one_to_many_policy"] == (
        "exact_unique_v1" if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY else None
    )
    assert data == (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    assert profile == MappingProfile(mapping, Decimal(tolerance), mode, normalization)
    assert profile.amount_tolerance.as_tuple() == Decimal(tolerance).as_tuple()
    assert profile.mapping.comparison_fields == comparisons
    assert (
        export_mapping_profile(
            profile.mapping,
            amount_tolerance=profile.amount_tolerance,
            reconciliation_mode=profile.reconciliation_mode,
            key_normalization=profile.key_normalization,
        )
        == data
    )


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_historical_profiles_migrate_to_v5_without_enabling_inference(version):
    document = json.loads(V4_PROFILE)
    document["version"] = version
    if version < 4:
        del document["comparison_fields"]
    if version < 3:
        del document["key_normalization"]
    if version == 1:
        del document["reconciliation_mode"]
    data = V4_PROFILE if version == 4 else json.dumps(document).encode("utf-8")
    original = bytes(data)
    profile = load_mapping_profile(data)
    assert profile == MappingProfile(
        replace(MAPPING, comparison_fields=COMPARISONS if version == 4 else ()),
        Decimal("0.000000000000000012300"),
        ReconciliationMode.UNIQUE if version == 1 else ReconciliationMode.GROUPED_BY_KEY,
        ACTIVE if version >= 3 else None,
    )
    assert profile.one_to_many_policy is None
    upgraded = export_mapping_profile(
        profile.mapping,
        amount_tolerance=profile.amount_tolerance,
        reconciliation_mode=profile.reconciliation_mode,
        key_normalization=profile.key_normalization,
    )
    current = json.loads(upgraded)
    assert tuple(current) == FIELDS
    assert current["version"] == 5
    assert current["one_to_many_policy"] is None
    assert current["amount_tolerance"] == document["amount_tolerance"]
    assert load_mapping_profile(upgraded) == profile
    assert data == original


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    "policy", ["", "unknown", "EXACT_UNIQUE_V1", "exact_unique_v1 ", True, False, 0, 1.5, [], {}]
)
def test_v5_rejects_malformed_policy_values_and_wrong_mode_pairings(mode, policy):
    document = json.loads(export_mapping_profile(MAPPING, reconciliation_mode=mode))
    document["one_to_many_policy"] = policy
    with pytest.raises(ProfileError, match="one_to_many_policy must be"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_legacy_v5_modes_reject_even_the_canonical_policy_id(mode):
    document = json.loads(export_mapping_profile(MAPPING, reconciliation_mode=mode))
    document["one_to_many_policy"] = "exact_unique_v1"
    with pytest.raises(ProfileError, match="must be null"):
        load_mapping_profile(json.dumps(document))


def test_bounded_v5_rejects_null_policy():
    document = json.loads(
        export_mapping_profile(MAPPING, reconciliation_mode=ReconciliationMode.BOUNDED_ONE_TO_MANY)
    )
    document["one_to_many_policy"] = None
    with pytest.raises(ProfileError, match='must be "exact_unique_v1"'):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("field", FIELDS)
def test_v5_requires_every_schema_field(mode, field):
    document = json.loads(export_mapping_profile(MAPPING, reconciliation_mode=mode))
    del document[field]
    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_v5_rejects_extra_or_duplicate_policy_fields(mode):
    data = export_mapping_profile(MAPPING, reconciliation_mode=mode).decode("utf-8")
    document = json.loads(data)
    document["max_candidate_rows"] = 12
    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))
    policy_text = json.dumps(json.loads(data)["one_to_many_policy"])
    field = f'"one_to_many_policy": {policy_text}'
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data.replace(field, f"{field}, {field}"))


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_historical_schemas_reject_policy_field_even_when_null(version):
    document = json.loads(V4_PROFILE)
    document["version"] = version
    if version < 4:
        del document["comparison_fields"]
    if version < 3:
        del document["key_normalization"]
    if version == 1:
        del document["reconciliation_mode"]
    document["one_to_many_policy"] = None
    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))
