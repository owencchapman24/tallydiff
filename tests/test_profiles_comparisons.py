"""Schema v4 secondary configuration and strict v1–v3 migration."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, localcontext

import pytest

import tallydiff.engine as engine
import tallydiff.ingest as ingest
import tallydiff.normalization as normalization
import tallydiff.xlsx as xlsx
from tallydiff import (
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

DEPARTMENT = ComparisonFieldMapping("Department", "Cost Center")
CURRENCY = ComparisonFieldMapping("Currency", "Currency Code")
DATE = ComparisonFieldMapping("Posting Date", "Document Date")
MAPPING = ColumnMapping((("id_a", "id_b"), ("ref_a", "ref_b")), "amount", "gross")
RULE_FIELDS = ("casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros")
ALL_FALSE = dict.fromkeys(RULE_FIELDS, False)
TOP_FIELDS = (
    "format",
    "version",
    "key_pairs",
    "amount_columns",
    "amount_tolerance",
    "reconciliation_mode",
    "key_normalization",
    "comparison_fields",
)
ACTIVE = KeyNormalizationConfig(
    (KeyNormalizationRules(casefold=True), KeyNormalizationRules(remove_punctuation=True))
)
V3_PROFILE = b"""{
  "format": "tallydiff-mapping-profile",
  "version": 3,
  "key_pairs": [{"file_a": "id_a", "file_b": "id_b"}, {"file_a": "ref_a", "file_b": "ref_b"}],
  "amount_columns": {"file_a": "amount", "file_b": "gross"},
  "amount_tolerance": "0.0100",
  "reconciliation_mode": "grouped_by_key",
  "key_normalization": [
    {"casefold": false, "collapse_whitespace": false,
     "remove_punctuation": false, "strip_leading_zeros": false},
    {"casefold": false, "collapse_whitespace": false,
     "remove_punctuation": false, "strip_leading_zeros": false}
  ]
}
"""


@pytest.fixture
def document():
    result = json.loads(V3_PROFILE)
    result["version"] = 4
    result["comparison_fields"] = [{"file_a": "Department", "file_b": "Cost Center"}]
    return result


def _mapping(comparisons=(DEPARTMENT, CURRENCY)):
    return replace(MAPPING, comparison_fields=comparisons)


def _boundary(kind, comparisons, document):
    mapping = _mapping(comparisons)
    if kind == "model":
        return MappingProfile(mapping)
    if kind == "export":
        return export_mapping_profile(mapping)
    document["comparison_fields"] = [{"file_a": c.file_a, "file_b": c.file_b} for c in comparisons]
    return load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("comparisons", [(), (DEPARTMENT,), (CURRENCY, DATE, DEPARTMENT)])
@pytest.mark.parametrize(
    "configuration", [None, KeyNormalizationConfig((KeyNormalizationRules(),) * 2), ACTIVE]
)
def test_current_v4_schema_is_exact_deterministic_and_round_trips(mode, comparisons, configuration):
    mapping = _mapping(comparisons)
    tolerance = Decimal("0.000000000000000000000012345678900")
    with localcontext() as context:
        context.prec = 1
        data = export_mapping_profile(
            mapping,
            amount_tolerance=tolerance,
            reconciliation_mode=mode,
            key_normalization=configuration,
        )
        restored = load_mapping_profile(data)
    rules = (
        configuration.component_rules
        if configuration is not None
        else (KeyNormalizationRules(),) * 2
    )
    expected = {
        "format": "tallydiff-mapping-profile",
        "version": 4,
        "key_pairs": [{"file_a": "id_a", "file_b": "id_b"}, {"file_a": "ref_a", "file_b": "ref_b"}],
        "amount_columns": {"file_a": "amount", "file_b": "gross"},
        "amount_tolerance": "0.000000000000000000000012345678900",
        "reconciliation_mode": mode.value,
        "key_normalization": [
            {name: getattr(rule, name) for name in RULE_FIELDS} for rule in rules
        ],
        "comparison_fields": [{"file_a": c.file_a, "file_b": c.file_b} for c in comparisons],
    }
    actual = json.loads(data)
    assert actual == expected
    assert tuple(actual) == TOP_FIELDS
    assert all(tuple(pair) == ("file_a", "file_b") for pair in actual["comparison_fields"])
    assert all(tuple(rule) == RULE_FIELDS for rule in actual["key_normalization"])
    assert data == (json.dumps(expected, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf")
    assert restored == MappingProfile(mapping, tolerance, mode, configuration)
    assert restored.amount_tolerance.as_tuple() == tolerance.as_tuple()
    assert restored.mapping.comparison_fields == comparisons
    assert (
        export_mapping_profile(
            restored.mapping,
            amount_tolerance=restored.amount_tolerance,
            reconciliation_mode=restored.reconciliation_mode,
            key_normalization=restored.key_normalization,
        )
        == data
    )


def test_unicode_and_directional_names_preserve_exact_text_in_ascii_escaped_json():
    comparisons = (
        ComparisonFieldMapping(" Départment\t ", "成本中心"),
        ComparisonFieldMapping("é", "e\u0301"),
    )
    mapping = _mapping(comparisons)
    data = export_mapping_profile(mapping, key_normalization=ACTIVE)
    assert all(byte < 128 for byte in data)
    assert b"\\u00e9" in data and b"\\u6210" in data
    assert load_mapping_profile(b"\xef\xbb\xbf" + data).mapping == mapping
    assert load_mapping_profile(data.decode("utf-8")).mapping.comparison_fields == comparisons


def test_profile_retains_four_positional_fields_and_immutable_nested_comparisons():
    mapping = _mapping((CURRENCY, DEPARTMENT))
    profile = MappingProfile(mapping, Decimal("0.0100"), ReconciliationMode.GROUPED_BY_KEY, ACTIVE)
    assert [field.name for field in fields(MappingProfile)] == [
        "mapping",
        "amount_tolerance",
        "reconciliation_mode",
        "key_normalization",
    ]
    assert profile.mapping is mapping
    assert profile.mapping.comparison_fields is mapping.comparison_fields
    assert profile.key_normalization is ACTIVE
    with pytest.raises(FrozenInstanceError):
        profile.mapping = MAPPING
    with pytest.raises(FrozenInstanceError):
        profile.mapping.comparison_fields = ()
    with pytest.raises(FrozenInstanceError):
        profile.mapping.comparison_fields[0].file_a = "changed"
    with pytest.raises(TypeError):
        MappingProfile(mapping, Decimal("0"), ReconciliationMode.UNIQUE, None, ())


@pytest.mark.parametrize("kind", ["model", "export"])
@pytest.mark.parametrize("side", ["A", "B"])
@pytest.mark.parametrize("name", [None, "", " \t", "\u2003"])
def test_profile_boundary_rejects_incomplete_comparisons_with_number_and_side(
    kind, side, name, document
):
    second = ComparisonFieldMapping(
        name if side == "A" else "Posting Date", name if side == "B" else "Document Date"
    )
    with pytest.raises(
        ProfileError, match=f"Comparison mapping 2 File {side} must be a nonblank column name"
    ):
        _boundary(kind, (DEPARTMENT, second), document)


@pytest.mark.parametrize("kind", ["model", "export", "load"])
@pytest.mark.parametrize("side", ["A", "B"])
def test_directional_duplicate_comparisons_raise_profile_error(kind, side, document):
    second = ComparisonFieldMapping(
        "Department" if side == "A" else "Posting Date",
        "Cost Center" if side == "B" else "Document Date",
    )
    with pytest.raises(
        ProfileError, match=f"Comparison mapping 2 File {side} column must be selected only once"
    ):
        _boundary(kind, (DEPARTMENT, second), document)


@pytest.mark.parametrize("kind", ["model", "export", "load"])
@pytest.mark.parametrize("side", ["A", "B"])
def test_same_side_amount_overlap_raises_profile_error(kind, side, document):
    second = ComparisonFieldMapping(
        "amount" if side == "A" else "Posting Date", "gross" if side == "B" else "Document Date"
    )
    with pytest.raises(
        ProfileError, match=f"Comparison mapping 2 File {side} must not use the amount column"
    ):
        _boundary(kind, (DEPARTMENT, second), document)


def test_key_comparison_and_amount_key_overlap_remain_valid_without_cross_side_restrictions():
    mapping = ColumnMapping(
        MAPPING.key_pairs,
        "id_a",
        "id_b",
        comparison_fields=(
            ComparisonFieldMapping("ref_a", "ref_b"),
            ComparisonFieldMapping("id_b", "id_a"),
        ),
    )
    profile = MappingProfile(mapping)
    profile.validate_columns(("id_a", "ref_a", "id_b"), ("id_b", "ref_b", "id_a"))
    assert load_mapping_profile(export_mapping_profile(mapping)) == profile
    crossed_amounts = _mapping((ComparisonFieldMapping("gross", "amount"),))
    assert load_mapping_profile(export_mapping_profile(crossed_amounts)).mapping == crossed_amounts


@pytest.mark.parametrize("field", TOP_FIELDS)
def test_v4_requires_every_top_level_field(document, field):
    del document[field]
    with pytest.raises(ProfileError, match="Profile must contain.*required fields"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("value", [None, {}, "", "Department", False, True, 0, 1.5])
def test_comparison_fields_must_be_an_array(document, value):
    document["comparison_fields"] = value
    with pytest.raises(ProfileError, match="comparison_fields must be an array"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("value", [None, [], ["Department", "Cost Center"], "Department", False, 1])
def test_each_comparison_entry_must_be_an_object(document, value):
    document["comparison_fields"].append(value)
    with pytest.raises(ProfileError, match="Comparison mapping 2 must be an object"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("side", ["file_a", "file_b"])
def test_both_comparison_fields_are_required(document, side):
    del document["comparison_fields"][0][side]
    with pytest.raises(ProfileError, match="Comparison mapping 1.*required fields.*file_a, file_b"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("side", ["A", "B"])
@pytest.mark.parametrize("value", [None, True, 0, 1.5, [], {}, "", " \t", "\u2003"])
def test_comparison_names_must_be_actual_nonblank_strings(document, side, value):
    document["comparison_fields"][0]["file_a" if side == "A" else "file_b"] = value
    with pytest.raises(
        ProfileError, match=f"Comparison mapping 1 File {side} must be a nonblank column name"
    ):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("side", ["file_a", "file_b"])
@pytest.mark.parametrize("name", ["same", "different"])
def test_duplicate_json_comparison_members_remain_rejected(document, side, name):
    data = json.dumps(document)
    value = document["comparison_fields"][0][side]
    original = f"{json.dumps(side)}: {json.dumps(value)}"
    duplicate = value if name == "same" else "other"
    data = data.replace(original, f"{original}, {json.dumps(side)}: {json.dumps(duplicate)}")
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data)


def test_duplicate_top_level_comparison_fields_remain_rejected(document):
    data = json.dumps(document).replace(
        '"comparison_fields":', '"comparison_fields": [], "comparison_fields":'
    )
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data)


@pytest.mark.parametrize(
    "field",
    [
        "strategy",
        "type",
        "options",
        "normalization",
        "source_records",
        "raw_fields",
        "source_values",
        "values_a",
        "values_b",
        "status",
        "field_comparisons",
        "findings",
        "totals",
        "filenames",
        "hashes",
        "worksheets",
        "timestamps",
        "paths",
        "collisions",
        "authority_decisions",
    ],
)
@pytest.mark.parametrize("location", ["root", "comparison"])
def test_profile_strict_fields_reject_comparison_results_and_runtime_metadata(
    document, field, location
):
    target = document if location == "root" else document["comparison_fields"][0]
    target[field] = "private runtime evidence"
    with pytest.raises(ProfileError, match="only these required fields"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("field", RULE_FIELDS)
@pytest.mark.parametrize("value", [0, "false", None])
def test_v4_preserves_strict_normalization_booleans(document, field, value):
    document["key_normalization"][1][field] = value
    with pytest.raises(ProfileError, match=f"Key normalization 2 {field} must be a boolean"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize(
    "version,mode,active",
    [
        (1, ReconciliationMode.UNIQUE, False),
        (2, ReconciliationMode.UNIQUE, False),
        (2, ReconciliationMode.GROUPED_BY_KEY, False),
        (3, ReconciliationMode.UNIQUE, False),
        (3, ReconciliationMode.GROUPED_BY_KEY, False),
        (3, ReconciliationMode.UNIQUE, True),
        (3, ReconciliationMode.GROUPED_BY_KEY, True),
    ],
)
def test_historical_versions_load_without_comparisons_and_upgrade_to_v4(version, mode, active):
    document = json.loads(V3_PROFILE)
    document["version"] = version
    if version == 1:
        del document["reconciliation_mode"]
    else:
        document["reconciliation_mode"] = mode.value
    if version < 3:
        del document["key_normalization"]
    elif active:
        document["key_normalization"][0]["casefold"] = True
    data = json.dumps(document).encode("utf-8")
    before = deepcopy(document)
    profile = load_mapping_profile(data)
    assert profile.mapping == MAPPING
    assert profile.mapping.comparison_fields == ()
    assert profile.reconciliation_mode is mode
    expected_normalization = (
        KeyNormalizationConfig((KeyNormalizationRules(casefold=True), KeyNormalizationRules()))
        if active
        else None
    )
    assert profile.key_normalization == expected_normalization
    assert profile.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    upgraded = export_mapping_profile(
        profile.mapping,
        amount_tolerance=profile.amount_tolerance,
        reconciliation_mode=profile.reconciliation_mode,
        key_normalization=profile.key_normalization,
    )
    assert json.loads(upgraded)["version"] == 4
    assert json.loads(upgraded)["comparison_fields"] == []
    assert load_mapping_profile(upgraded) == profile
    assert json.loads(data) == document == before


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("comparisons", [[], [{"file_a": "Department", "file_b": "Cost Center"}]])
def test_legacy_profiles_reject_even_empty_comparison_fields(version, comparisons):
    document = json.loads(V3_PROFILE)
    document["version"] = version
    if version < 3:
        del document["key_normalization"]
    if version == 1:
        del document["reconciliation_mode"]
    document["comparison_fields"] = comparisons
    with pytest.raises(ProfileError, match="Profile.*only these required fields"):
        load_mapping_profile(json.dumps(document))


def test_original_v3_bytes_require_normalization_and_canonicalize_all_false():
    profile = load_mapping_profile(V3_PROFILE)
    assert profile.key_normalization is None
    assert profile.mapping.comparison_fields == ()
    document = json.loads(V3_PROFILE)
    del document["key_normalization"]
    with pytest.raises(ProfileError, match="required fields.*key_normalization"):
        load_mapping_profile(json.dumps(document))


def test_validate_columns_accepts_reordering_extra_columns_and_exact_selected_names():
    profile = MappingProfile(_mapping())
    profile.validate_columns(
        ("Currency", "Department", "amount", "ref_a", "id_a", "extra"),
        ("extra", "gross", "id_b", "Currency Code", "Cost Center", "ref_b"),
    )


@pytest.mark.parametrize("side", ["A", "B"])
@pytest.mark.parametrize("replacement", [None, "department", " Department "])
def test_validate_columns_reports_missing_directional_comparison_exactly(side, replacement):
    profile = MappingProfile(_mapping())
    a, b = (
        ["id_a", "ref_a", "amount", "Department", "Currency"],
        ["id_b", "ref_b", "gross", "Cost Center", "Currency Code"],
    )
    columns = a if side == "A" else b
    missing = "Department" if side == "A" else "Cost Center"
    columns.remove(missing)
    if replacement is not None:
        columns.append(replacement)
    with pytest.raises(ProfileError) as error:
        profile.validate_columns(a, b)
    assert (
        str(error.value)
        == f'Cannot apply profile: File {side} is missing required column "{missing}".'
    )


def test_validate_columns_reports_all_roles_atomically_without_duplicate_requirements():
    profile = MappingProfile(
        _mapping((ComparisonFieldMapping("id_a", "id_b"), DEPARTMENT, CURRENCY))
    )
    before = profile.mapping
    with pytest.raises(ProfileError) as error:
        profile.validate_columns((), ())
    expected = [
        ("A", "id_a"),
        ("A", "ref_a"),
        ("A", "amount"),
        ("A", "Department"),
        ("A", "Currency"),
        ("B", "id_b"),
        ("B", "ref_b"),
        ("B", "gross"),
        ("B", "Cost Center"),
        ("B", "Currency Code"),
    ]
    assert str(error.value) == "Cannot apply profile: " + " ".join(
        f'File {side} is missing required column "{name}".' for side, name in expected
    )
    assert profile.mapping is before


def test_profiles_never_process_source_records_comparisons_or_normalization(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("profile persistence must remain configuration-only")

    monkeypatch.setattr(engine, "reconcile", forbidden)
    monkeypatch.setattr(ingest, "ingest_csv", forbidden)
    monkeypatch.setattr(xlsx, "ingest_xlsx", forbidden)
    monkeypatch.setattr(normalization, "normalize_key", forbidden)
    mapping = _mapping((DEPARTMENT, CURRENCY, DATE))
    data = export_mapping_profile(mapping, key_normalization=ACTIVE)
    profile = load_mapping_profile(data)
    profile.validate_columns(
        ("id_a", "ref_a", "amount", "Department", "Currency", "Posting Date"),
        ("id_b", "ref_b", "gross", "Cost Center", "Currency Code", "Document Date"),
    )
    assert tuple(json.loads(data)) == TOP_FIELDS
