"""Schema v3 configuration-only persistence and strict legacy compatibility."""

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

import tallydiff.engine as engine
import tallydiff.normalization as normalization
from tallydiff import (
    ColumnMapping,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    MappingProfile,
    ProfileError,
    ReconciliationMode,
    export_mapping_profile,
    load_mapping_profile,
)

MAPPING = ColumnMapping(
    (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")), "Invoice Amount", "Gross Amount"
)
COLUMNS_A = ("Vendor ID", "Invoice Number", "Invoice Amount")
COLUMNS_B = ("Supplier", "Invoice Ref", "Gross Amount")
RULE_FIELDS = ("casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros")
TOP_FIELDS = (
    "format",
    "version",
    "key_pairs",
    "amount_columns",
    "amount_tolerance",
    "reconciliation_mode",
    "key_normalization",
)
ALL_FALSE = {
    "casefold": False,
    "collapse_whitespace": False,
    "remove_punctuation": False,
    "strip_leading_zeros": False,
}
V2_PROFILE = b"""{
  "format": "tallydiff-mapping-profile",
  "version": 2,
  "key_pairs": [
    {"file_a": "Vendor ID", "file_b": "Supplier"},
    {"file_a": "Invoice Number", "file_b": "Invoice Ref"}
  ],
  "amount_columns": {"file_a": "Invoice Amount", "file_b": "Gross Amount"},
  "amount_tolerance": "0.0100",
  "reconciliation_mode": "unique"
}
"""


@pytest.fixture
def v3_document() -> dict:
    document = json.loads(V2_PROFILE)
    document["version"] = 3
    document["key_normalization"] = [dict(ALL_FALSE) for _ in MAPPING.key_pairs]
    return document


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_v3_export_exact_schema_order_boolean_rules_and_format(mode: ReconciliationMode) -> None:
    data = export_mapping_profile(
        MAPPING, amount_tolerance=Decimal("0.0100"), reconciliation_mode=mode
    )
    document = json.loads(data)

    assert tuple(document) == TOP_FIELDS
    assert document["version"] == 3
    assert document["reconciliation_mode"] == mode.value
    assert document["amount_tolerance"] == "0.0100"
    assert document["key_normalization"] == [ALL_FALSE, ALL_FALSE]
    for rules in document["key_normalization"]:
        assert tuple(rules) == RULE_FIELDS
        assert all(type(value) is bool for value in rules.values())
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert data == (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    assert data == export_mapping_profile(
        MAPPING, amount_tolerance=Decimal("0.0100"), reconciliation_mode=mode
    )


@pytest.mark.parametrize("count", [1, 2, 3])
def test_explicit_all_false_and_none_have_one_canonical_profile_and_identical_bytes(
    count: int,
) -> None:
    mapping = ColumnMapping(
        tuple((f"key_a_{index}", f"key_b_{index}") for index in range(count)), "a", "b"
    )
    configuration = KeyNormalizationConfig((KeyNormalizationRules(),) * count)
    exact = MappingProfile(mapping)
    explicit = MappingProfile(mapping, key_normalization=configuration)

    assert exact.key_normalization is explicit.key_normalization is None
    assert exact == explicit
    expected = export_mapping_profile(mapping)
    assert export_mapping_profile(mapping, key_normalization=configuration) == expected
    assert load_mapping_profile(expected).key_normalization is None
    assert len(json.loads(expected)["key_normalization"]) == count


@pytest.mark.parametrize("name", RULE_FIELDS)
@pytest.mark.parametrize("index", [0, 1])
def test_v3_restores_each_enabled_rule_and_preserves_exact_component_positions(
    v3_document: dict, name: str, index: int
) -> None:
    v3_document["key_normalization"][index][name] = True
    expected_rules = [KeyNormalizationRules(), KeyNormalizationRules()]
    expected_rules[index] = KeyNormalizationRules(**{name: True})

    profile = load_mapping_profile(json.dumps(v3_document))

    assert profile.key_normalization == KeyNormalizationConfig(tuple(expected_rules))
    assert len(profile.key_normalization.component_rules) == len(MAPPING.key_pairs)
    assert profile.key_normalization.component_rules[1 - index] == KeyNormalizationRules()


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    "configuration",
    [
        None,
        KeyNormalizationConfig((KeyNormalizationRules(),) * 2),
        KeyNormalizationConfig((KeyNormalizationRules(True, True, True, True),) * 2),
        KeyNormalizationConfig(
            (KeyNormalizationRules(casefold=True), KeyNormalizationRules(strip_leading_zeros=True))
        ),
        KeyNormalizationConfig(
            (KeyNormalizationRules(), KeyNormalizationRules(True, True, True, True))
        ),
    ],
)
def test_v3_round_trip_restores_exact_configuration_and_deterministic_bytes(
    mode: ReconciliationMode, configuration: KeyNormalizationConfig | None
) -> None:
    original = MappingProfile(MAPPING, Decimal("0.0100"), mode, configuration)
    data = export_mapping_profile(
        MAPPING,
        amount_tolerance=original.amount_tolerance,
        reconciliation_mode=mode,
        key_normalization=configuration,
    )
    restored = load_mapping_profile(data)

    assert restored == original
    assert restored.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    assert restored.key_normalization == original.key_normalization
    assert restored.mapping.key_pairs == MAPPING.key_pairs
    assert (
        export_mapping_profile(
            restored.mapping,
            amount_tolerance=restored.amount_tolerance,
            reconciliation_mode=restored.reconciliation_mode,
            key_normalization=restored.key_normalization,
        )
        == data
    )


def test_mixed_rule_flags_are_serialized_exactly_without_dropping_components() -> None:
    configuration = KeyNormalizationConfig(
        (
            KeyNormalizationRules(True, False, True, False),
            KeyNormalizationRules(False, True, False, True),
        )
    )
    document = json.loads(export_mapping_profile(MAPPING, key_normalization=configuration))

    assert document["key_normalization"] == [
        {
            "casefold": True,
            "collapse_whitespace": False,
            "remove_punctuation": True,
            "strip_leading_zeros": False,
        },
        {
            "casefold": False,
            "collapse_whitespace": True,
            "remove_punctuation": False,
            "strip_leading_zeros": True,
        },
    ]


def test_v3_unicode_column_names_remain_ascii_escaped_without_text_changes() -> None:
    mapping = ColumnMapping(((" Vendor é ", "供应商"),), " Total ", "金額")
    configuration = KeyNormalizationConfig((KeyNormalizationRules(casefold=True),))
    data = export_mapping_profile(mapping, key_normalization=configuration)

    assert all(value < 128 for value in data)
    assert b"\\u00e9" in data
    assert load_mapping_profile(b"\xef\xbb\xbf" + data).mapping == mapping
    assert load_mapping_profile(data.decode("utf-8")).key_normalization == configuration


@pytest.mark.parametrize("field", TOP_FIELDS)
def test_v3_requires_every_top_level_field(v3_document: dict, field: str) -> None:
    del v3_document[field]
    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("value", [None, {}, "", "casefold", False, True, 0, 1])
def test_v3_normalization_requires_an_array(v3_document: dict, value: object) -> None:
    v3_document["key_normalization"] = value
    with pytest.raises(ProfileError, match="key_normalization must be an array"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("count", [0, 1, 3])
def test_v3_normalization_array_must_match_key_pair_count(v3_document: dict, count: int) -> None:
    v3_document["key_normalization"] = [ALL_FALSE] * count
    with pytest.raises(ProfileError, match="one rule object per key mapping"):
        load_mapping_profile(json.dumps(v3_document))


def test_v3_empty_keys_and_rules_fail_as_profile_error(v3_document: dict) -> None:
    v3_document["key_pairs"] = []
    v3_document["key_normalization"] = []
    with pytest.raises(ProfileError):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("value", [None, False, 0, 1, "casefold", ["casefold"]])
def test_v3_rule_entries_must_be_objects(v3_document: dict, index: int, value: object) -> None:
    v3_document["key_normalization"][index] = value
    with pytest.raises(ProfileError, match=f"Key normalization {index + 1} must be an object"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("field", RULE_FIELDS)
@pytest.mark.parametrize("index", [0, 1])
def test_v3_requires_each_rule_field(v3_document: dict, field: str, index: int) -> None:
    del v3_document["key_normalization"][index][field]
    with pytest.raises(ProfileError, match=f"Key normalization {index + 1}.*required fields"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("field", ["unknown", "regex", "Casefold", "order", "strip_accents"])
def test_v3_rejects_extra_or_unsupported_rule_fields(v3_document: dict, field: str) -> None:
    v3_document["key_normalization"][0][field] = False
    with pytest.raises(ProfileError, match="Key normalization 1.*required fields"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("field", RULE_FIELDS)
def test_v3_rejects_case_changed_rule_names(v3_document: dict, field: str) -> None:
    rules = v3_document["key_normalization"][0]
    rules[field.upper()] = rules.pop(field)
    with pytest.raises(ProfileError, match="Key normalization 1.*required fields"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("field", ["Key_normalization", "key_Normalization", "key_normalisation"])
def test_v3_rejects_wrong_normalization_field_names(v3_document: dict, field: str) -> None:
    v3_document[field] = v3_document.pop("key_normalization")
    with pytest.raises(ProfileError, match="Profile.*required fields"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("field", RULE_FIELDS)
@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, "true", "false", None, [], {}])
def test_v3_rule_values_must_be_actual_json_booleans(
    v3_document: dict, field: str, value: object
) -> None:
    v3_document["key_normalization"][1][field] = value
    with pytest.raises(ProfileError, match=f"Key normalization 2 {field} must be a boolean"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("field", RULE_FIELDS)
@pytest.mark.parametrize("duplicate", ["false", "true"])
def test_v3_rejects_duplicate_rule_fields_even_if_values_agree(
    v3_document: dict, field: str, duplicate: str
) -> None:
    data = json.dumps(v3_document)
    original = f'"{field}": false'
    data = data.replace(original, f'{original}, "{field}": {duplicate}', 1)
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data)


@pytest.mark.parametrize("duplicate", ["null", "[]", "[{}]"])
def test_v3_rejects_duplicate_top_level_normalization(v3_document: dict, duplicate: str) -> None:
    data = json.dumps(v3_document)
    data = data.replace(
        '"key_normalization":', f'"key_normalization": {duplicate}, "key_normalization":'
    )
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data)


@pytest.mark.parametrize("location", ["root", "pair", "amounts"])
def test_v3_preserves_duplicate_protection_at_existing_object_levels(
    v3_document: dict, location: str
) -> None:
    data = json.dumps(v3_document)
    field, value = {
        "root": ("version", "3"),
        "pair": ("file_a", '"Vendor ID"'),
        "amounts": ("file_a", '"Invoice Amount"'),
    }[location]
    original = f'"{field}": {value}'
    data = data.replace(original, f"{original}, {original}", 1)
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data)


@pytest.mark.parametrize("mode", [None, True, 0, 1, [], {}, "Unique", "unique ", "unknown"])
def test_v3_requires_exact_supported_mode(v3_document: dict, mode: object) -> None:
    v3_document["reconciliation_mode"] = mode
    with pytest.raises(ProfileError, match="reconciliation_mode"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("number", ["3.0", "3.00", "3e0", "1.0", "2e0"])
def test_numeric_version_variants_are_not_coerced(v3_document: dict, number: str) -> None:
    data = json.dumps(v3_document).replace('"version": 3', f'"version": {number}')
    with pytest.raises(ProfileError, match="expected version 1, 2, or 3"):
        load_mapping_profile(data)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("encoding", ["bytes", "str", "bom"])
def test_original_v2_json_bytes_still_load_with_no_normalization(
    mode: ReconciliationMode, encoding: str
) -> None:
    data = V2_PROFILE.replace(b'"unique"', json.dumps(mode.value).encode("ascii"))
    supplied = (
        data.decode("utf-8")
        if encoding == "str"
        else (b"\xef\xbb\xbf" + data if encoding == "bom" else data)
    )
    profile = load_mapping_profile(supplied)

    assert profile == MappingProfile(MAPPING, Decimal("0.0100"), mode)
    assert profile.reconciliation_mode is mode
    assert profile.key_normalization is None
    assert profile.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    assert b'"version": 2' in data


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("value", [None, [], [ALL_FALSE, ALL_FALSE]])
def test_legacy_versions_reject_normalization_field(version: int, value: object) -> None:
    document = json.loads(V2_PROFILE)
    document["version"] = version
    if version == 1:
        del document["reconciliation_mode"]
    document["key_normalization"] = value
    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("factory", [MappingProfile, export_mapping_profile])
@pytest.mark.parametrize("value", [(), [], {}, False, 0, "casefold", KeyNormalizationRules()])
def test_profile_and_export_reject_wrong_normalization_types(factory, value: object) -> None:
    with pytest.raises(
        ProfileError, match="key_normalization must be a KeyNormalizationConfig or None"
    ):
        factory(MAPPING, key_normalization=value)


@pytest.mark.parametrize("factory", [MappingProfile, export_mapping_profile])
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("enabled", [False, True])
def test_profile_and_export_check_arity_before_canonicalizing(
    factory, count: int, enabled: bool
) -> None:
    configuration = KeyNormalizationConfig((KeyNormalizationRules(casefold=enabled),) * count)
    with pytest.raises(ProfileError, match="key_normalization length must equal key_pairs length"):
        factory(MAPPING, key_normalization=configuration)


def test_profile_appends_immutable_configuration_without_changing_old_positions() -> None:
    default = MappingProfile(MAPPING)
    tolerance = MappingProfile(MAPPING, Decimal("0.0100"))
    legacy = MappingProfile(MAPPING, Decimal("0.0100"), ReconciliationMode.GROUPED_BY_KEY)
    configuration = KeyNormalizationConfig(
        (KeyNormalizationRules(casefold=True), KeyNormalizationRules())
    )
    explicit = MappingProfile(
        MAPPING, Decimal("0.0100"), ReconciliationMode.GROUPED_BY_KEY, configuration
    )

    assert (
        default.key_normalization is tolerance.key_normalization is legacy.key_normalization is None
    )
    assert default.reconciliation_mode is tolerance.reconciliation_mode is ReconciliationMode.UNIQUE
    assert legacy.reconciliation_mode is ReconciliationMode.GROUPED_BY_KEY
    assert tolerance.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    assert explicit.key_normalization is configuration
    with pytest.raises(FrozenInstanceError):
        explicit.key_normalization = None
    with pytest.raises(FrozenInstanceError):
        explicit.key_normalization.component_rules = (KeyNormalizationRules(),)
    with pytest.raises(FrozenInstanceError):
        explicit.key_normalization.component_rules[0].casefold = False


def test_profile_round_trip_does_not_run_normalization_or_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("Profiles persist configuration without processing source keys")

    monkeypatch.setattr(normalization, "normalize_key", forbidden)
    monkeypatch.setattr(normalization, "find_normalization_collisions", forbidden)
    monkeypatch.setattr(engine, "reconcile", forbidden)
    mapping = ColumnMapping((("---", " ACME-01 "),), "amount", "gross")
    configuration = KeyNormalizationConfig((KeyNormalizationRules(True, True, True, True),))
    data = export_mapping_profile(mapping, key_normalization=configuration)
    profile = load_mapping_profile(data)

    assert profile.mapping == mapping
    assert profile.key_normalization == configuration
    assert set(json.loads(data)) == set(TOP_FIELDS)


@pytest.mark.parametrize(
    "field",
    [
        "source_records",
        "raw_fields",
        "source_values",
        "normalized_values",
        "filename",
        "hash",
        "worksheet",
        "totals",
        "findings",
        "collision_evidence",
        "timestamp",
        "machine_path",
    ],
)
def test_v3_rejects_source_and_runtime_metadata(v3_document: dict, field: str) -> None:
    v3_document[field] = "private runtime evidence"
    with pytest.raises(ProfileError, match="Profile.*required fields"):
        load_mapping_profile(json.dumps(v3_document))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_normalized_profile_column_validation_preserves_exact_directional_requirements(
    mode: ReconciliationMode,
) -> None:
    configuration = KeyNormalizationConfig((KeyNormalizationRules(True, True, True, True),) * 2)
    normalized = MappingProfile(MAPPING, reconciliation_mode=mode, key_normalization=configuration)
    exact = MappingProfile(MAPPING, reconciliation_mode=mode)
    restored = load_mapping_profile(
        export_mapping_profile(MAPPING, reconciliation_mode=mode, key_normalization=configuration)
    )
    restored.validate_columns((*reversed(COLUMNS_A), "extra"), (*reversed(COLUMNS_B), "extra"))

    for columns_a, columns_b in (
        (COLUMNS_A[1:], COLUMNS_B),
        (COLUMNS_A, COLUMNS_B[1:]),
        (COLUMNS_A[:-1], COLUMNS_B[:-1]),
        (COLUMNS_B, COLUMNS_A),
        (("vendor id", *COLUMNS_A[1:]), COLUMNS_B),
    ):
        with pytest.raises(ProfileError) as exact_error:
            exact.validate_columns(columns_a, columns_b)
        for profile in (normalized, restored):
            with pytest.raises(ProfileError) as normalized_error:
                profile.validate_columns(columns_a, columns_b)
            assert str(normalized_error.value) == str(exact_error.value)
