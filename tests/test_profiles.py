import json
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext

import pytest

from tallydiff import (
    ColumnMapping,
    MappingProfile,
    ProfileError,
    ReconciliationMode,
    export_mapping_profile,
    load_mapping_profile,
)
from tallydiff.presentation import ColumnMapping as PresentationColumnMapping
from tallydiff.presentation import configuration_id

MAPPING = ColumnMapping(
    (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")), "Invoice Amount", "Gross Amount"
)
COLUMNS_A = ("Vendor ID", "Invoice Number", "Invoice Amount")
COLUMNS_B = ("Supplier", "Invoice Ref", "Gross Amount")

V1_PROFILE = b"""{
  "format": "tallydiff-mapping-profile",
  "version": 1,
  "key_pairs": [
    {
      "file_a": "Vendor ID",
      "file_b": "Supplier"
    },
    {
      "file_a": "Invoice Number",
      "file_b": "Invoice Ref"
    }
  ],
  "amount_columns": {
    "file_a": "Invoice Amount",
    "file_b": "Gross Amount"
  },
  "amount_tolerance": "0.0100"
}
"""


@pytest.fixture(params=[1, 2])
def document(request: pytest.FixtureRequest) -> dict:
    profile = json.loads(V1_PROFILE)
    if request.param == 2:
        profile.update(version=2, reconciliation_mode="unique")
    return profile


@pytest.fixture
def v2_document() -> dict:
    profile = json.loads(V1_PROFILE)
    profile.update(version=2, reconciliation_mode="unique")
    return profile


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_serialized_profile_has_only_versioned_configuration_fields(
    mode: ReconciliationMode,
) -> None:
    document = json.loads(
        export_mapping_profile(
            MAPPING, amount_tolerance=Decimal("0.0100"), reconciliation_mode=mode
        )
    )
    assert document == {
        "format": "tallydiff-mapping-profile",
        "version": 3,
        "key_pairs": [
            {"file_a": "Vendor ID", "file_b": "Supplier"},
            {"file_a": "Invoice Number", "file_b": "Invoice Ref"},
        ],
        "amount_columns": {"file_a": "Invoice Amount", "file_b": "Gross Amount"},
        "amount_tolerance": "0.0100",
        "reconciliation_mode": mode.value,
        "key_normalization": [
            {
                "casefold": False,
                "collapse_whitespace": False,
                "remove_punctuation": False,
                "strip_leading_zeros": False,
            }
            for _ in MAPPING.key_pairs
        ],
    }
    assert export_mapping_profile(MAPPING).endswith(b"\n")
    assert PresentationColumnMapping is ColumnMapping


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("tolerance", ["0", "0.0100", "0.000000000000000000000012345678900"])
def test_round_trip_preserves_order_and_exact_decimal_digits(
    tolerance: str, mode: ReconciliationMode
) -> None:
    with localcontext() as context:
        context.prec = 2
        data = export_mapping_profile(
            MAPPING, amount_tolerance=Decimal(tolerance), reconciliation_mode=mode
        )
        profile = load_mapping_profile(data)
        assert profile == MappingProfile(MAPPING, Decimal(tolerance), mode)
        assert profile.mapping.key_pairs == MAPPING.key_pairs
        assert profile.amount_tolerance.as_tuple() == Decimal(tolerance).as_tuple()
        assert json.loads(data)["amount_tolerance"] == tolerance
        assert (
            export_mapping_profile(
                profile.mapping,
                amount_tolerance=profile.amount_tolerance,
                reconciliation_mode=profile.reconciliation_mode,
            )
            == data
        )


def test_utf8_bom_unicode_and_exact_column_whitespace_are_preserved() -> None:
    mapping = ColumnMapping(((" Vendor é ", "供应商"),), " Total ", "金額")
    data = export_mapping_profile(mapping)
    assert load_mapping_profile(b"\xef\xbb\xbf" + data).mapping == mapping
    assert load_mapping_profile(data.decode()).mapping == mapping


@pytest.mark.parametrize("data", [b"{", b"not json", b"\xff", b"[" * 1200])
def test_invalid_json_is_a_concise_profile_error(data: bytes) -> None:
    with pytest.raises(ProfileError, match="valid UTF-8 JSON") as error:
        load_mapping_profile(data)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("format", "other", "Unsupported profile format"),
        ("version", 4, "Unsupported profile version"),
        ("version", True, "Unsupported profile version"),
        ("key_pairs", {}, "key_pairs must be an array"),
        ("key_pairs", [], "at least one key mapping"),
        ("key_pairs", [["Vendor ID", "Supplier"]], "must be an object"),
        ("amount_columns", [], "amount_columns must be an object"),
        ("amount_tolerance", 0, "must be decimal text"),
        ("amount_tolerance", 0.01, "not a JSON number"),
        ("amount_tolerance", None, "must be decimal text"),
    ],
)
def test_wrong_format_version_types_and_shapes_are_rejected(
    document: dict, field: str, value: object, message: str
) -> None:
    document[field] = value
    with pytest.raises(ProfileError, match=message):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("root_value", [[], None, "profile"])
def test_profile_root_must_be_an_object(root_value: object) -> None:
    with pytest.raises(ProfileError, match="Profile must be an object"):
        load_mapping_profile(json.dumps(root_value))


@pytest.mark.parametrize("location", ["root", "pair", "amounts"])
def test_required_fields_and_unknown_fields_are_strict(document: dict, location: str) -> None:
    target = (
        document
        if location == "root"
        else (document["key_pairs"][0] if location == "pair" else document["amount_columns"])
    )
    removed = next(iter(target))
    value = target.pop(removed)
    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))
    target[removed] = value
    target["unexpected"] = "value"
    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("side", ["file_a", "file_b"])
def test_blank_and_duplicate_key_names_are_rejected(document: dict, side: str) -> None:
    document["key_pairs"][0][side] = " \t"
    with pytest.raises(ProfileError, match="nonblank column name"):
        load_mapping_profile(json.dumps(document))
    document["key_pairs"][0][side] = document["key_pairs"][1][side]
    with pytest.raises(ProfileError, match="selected only once"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("value", ["", None, ["amount"]])
def test_amount_columns_must_be_nonblank_strings(document: dict, value: object) -> None:
    document["amount_columns"]["file_a"] = value
    with pytest.raises(ProfileError, match="File A amount column must be a nonblank column name"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("text", ["", "invalid", "NaN", "Infinity", "-0.01"])
def test_invalid_negative_and_nonfinite_tolerance_is_rejected(document: dict, text: str) -> None:
    document["amount_tolerance"] = text
    with pytest.raises(ProfileError, match="amount_tolerance must be"):
        load_mapping_profile(json.dumps(document))


def test_duplicate_json_fields_and_nonstandard_constants_are_rejected(document: dict) -> None:
    data = json.dumps(document)
    version_field = f'"version": {document["version"]}'
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data.replace(version_field, f"{version_field}, {version_field}"))
    with pytest.raises(ProfileError, match="non-finite value"):
        load_mapping_profile(data.replace('"0.0100"', "NaN"))


def test_export_rejects_incomplete_configuration_and_non_decimal_tolerance() -> None:
    with pytest.raises(ProfileError, match="nonblank column name"):
        export_mapping_profile(ColumnMapping(((None, "Supplier"),), "amount", "gross"))
    with pytest.raises(ProfileError, match="must be a Decimal"):
        export_mapping_profile(MAPPING, amount_tolerance=0.01)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("side", ["A", "B"])
def test_compatibility_reports_exact_missing_directional_columns(
    side: str, mode: ReconciliationMode
) -> None:
    profile = MappingProfile(MAPPING, reconciliation_mode=mode)
    a = COLUMNS_A[1:] if side == "A" else COLUMNS_A
    b = COLUMNS_B[1:] if side == "B" else COLUMNS_B
    missing = "Vendor ID" if side == "A" else "Supplier"
    with pytest.raises(ProfileError, match=f'File {side} is missing required column "{missing}"'):
        profile.validate_columns(a, b)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_compatibility_checks_amount_columns_and_never_swaps_sides_or_guesses(
    mode: ReconciliationMode,
) -> None:
    profile = MappingProfile(MAPPING, reconciliation_mode=mode)
    with pytest.raises(ProfileError) as error:
        profile.validate_columns(COLUMNS_A[:-1], COLUMNS_B[:-1])
    assert 'File A is missing required column "Invoice Amount"' in str(error.value)
    assert 'File B is missing required column "Gross Amount"' in str(error.value)
    with pytest.raises(ProfileError):
        profile.validate_columns(COLUMNS_B, COLUMNS_A)
    with pytest.raises(ProfileError):
        profile.validate_columns(("vendor id", *COLUMNS_A[1:]), COLUMNS_B)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_extra_columns_and_changed_header_order_remain_compatible(mode: ReconciliationMode) -> None:
    profile = MappingProfile(MAPPING, reconciliation_mode=mode)
    profile.validate_columns((*reversed(COLUMNS_A), "new A column"), (*COLUMNS_B, "new B column"))


def test_effective_identity_ignores_json_formatting_and_profile_metadata() -> None:
    data = export_mapping_profile(MAPPING, amount_tolerance=Decimal("0.0100"))
    equivalent = json.loads(data)
    equivalent["amount_tolerance"] = "0.01"
    profiles = [
        load_mapping_profile(data),
        load_mapping_profile(json.dumps(json.loads(data))),
        load_mapping_profile(json.dumps(equivalent)),
    ]
    identities = [
        configuration_id(
            b"a",
            b"b",
            p.mapping,
            name_a="next-month-a.csv",
            name_b="next-month-b.csv",
            amount_tolerance=p.amount_tolerance,
        )
        for p in profiles
    ]
    assert len(set(identities)) == 1
    assert profiles[0].amount_tolerance.as_tuple() != profiles[2].amount_tolerance.as_tuple()


def test_default_mode_preserves_existing_profile_and_export_callers() -> None:
    default = MappingProfile(MAPPING)
    with_tolerance = MappingProfile(MAPPING, Decimal("0.0100"))
    exported = json.loads(export_mapping_profile(MAPPING))

    assert default.reconciliation_mode is ReconciliationMode.UNIQUE
    assert with_tolerance.reconciliation_mode is ReconciliationMode.UNIQUE
    assert with_tolerance.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    assert exported["version"] == 3
    assert exported["reconciliation_mode"] == "unique"
    assert exported["amount_tolerance"] == "0"
    assert load_mapping_profile(export_mapping_profile(MAPPING)) == default


def test_profile_accepts_grouped_mode_as_immutable_configuration() -> None:
    profile = MappingProfile(
        MAPPING, Decimal("0.01"), reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY
    )

    assert profile.reconciliation_mode is ReconciliationMode.GROUPED_BY_KEY
    with pytest.raises(FrozenInstanceError):
        profile.reconciliation_mode = ReconciliationMode.UNIQUE


@pytest.mark.parametrize("factory", [MappingProfile, export_mapping_profile])
@pytest.mark.parametrize(
    "mode", ["unique", "grouped_by_key", "unknown", None, True, 0, Decimal("0"), [], {}]
)
def test_profile_and_export_require_an_actual_mode_enum(factory, mode: object) -> None:
    with pytest.raises(ProfileError, match="reconciliation_mode.*ReconciliationMode enum member"):
        factory(MAPPING, reconciliation_mode=mode)


@pytest.mark.parametrize("data", [V1_PROFILE, V1_PROFILE.decode(), b"\xef\xbb\xbf" + V1_PROFILE])
def test_real_v1_profile_loads_as_unique_and_round_trips_logically(data: bytes | str) -> None:
    profile = load_mapping_profile(data)

    assert profile == MappingProfile(MAPPING, Decimal("0.0100"))
    assert profile.reconciliation_mode is ReconciliationMode.UNIQUE
    assert profile.key_normalization is None
    assert profile.mapping.key_pairs == MAPPING.key_pairs
    assert profile.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    profile.validate_columns(COLUMNS_A, COLUMNS_B)
    upgraded = export_mapping_profile(
        profile.mapping,
        amount_tolerance=profile.amount_tolerance,
        reconciliation_mode=profile.reconciliation_mode,
    )
    assert json.loads(upgraded)["version"] == 3
    assert json.loads(upgraded)["reconciliation_mode"] == "unique"
    assert load_mapping_profile(upgraded) == profile


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_v2_loads_each_exact_supported_mode(v2_document: dict, mode: ReconciliationMode) -> None:
    v2_document["reconciliation_mode"] = mode.value

    profile = load_mapping_profile(json.dumps(v2_document))

    assert profile.reconciliation_mode is mode
    assert profile.key_normalization is None
    assert profile.mapping == MAPPING
    assert profile.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()


@pytest.mark.parametrize(
    "mode",
    [
        "unknown",
        "",
        "UNIQUE",
        "GROUPED_BY_KEY",
        "Unique",
        "Grouped_By_Key",
        " unique",
        "unique ",
        " grouped_by_key",
        "grouped_by_key ",
        "unique\n",
        "grouped_by_key\t",
    ],
)
def test_v2_rejects_unknown_case_and_whitespace_mode_variants(v2_document: dict, mode: str) -> None:
    v2_document["reconciliation_mode"] = mode

    with pytest.raises(ProfileError, match="Unsupported reconciliation_mode") as error:
        load_mapping_profile(json.dumps(v2_document))

    assert error.value.__suppress_context__


@pytest.mark.parametrize("mode", [None, True, False, 0, 1, 2.0, [], {}, ["unique"]])
def test_v2_rejects_non_string_modes(v2_document: dict, mode: object) -> None:
    v2_document["reconciliation_mode"] = mode

    with pytest.raises(ProfileError, match="reconciliation_mode must be a string"):
        load_mapping_profile(json.dumps(v2_document))


def test_v2_requires_mode_instead_of_defaulting(v2_document: dict) -> None:
    del v2_document["reconciliation_mode"]

    with pytest.raises(ProfileError, match="required fields.*reconciliation_mode"):
        load_mapping_profile(json.dumps(v2_document))


@pytest.mark.parametrize("mode", ["unique", "grouped_by_key"])
def test_v1_rejects_the_v2_mode_field(mode: str) -> None:
    document = json.loads(V1_PROFILE)
    document["reconciliation_mode"] = mode

    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize(
    "version", [-1, 0, 4, 99, True, False, "1", "2", "3", None, 1.0, 2.0, 3.0, [], {}]
)
def test_unsupported_and_wrong_type_versions_raise_clear_errors(
    document: dict, version: object
) -> None:
    document["version"] = version

    with pytest.raises(
        ProfileError, match="Unsupported profile version; expected version 1, 2, or 3"
    ):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize(
    "field", ["format", "version", "key_pairs", "amount_columns", "amount_tolerance"]
)
def test_all_shared_fields_are_required_in_each_version(document: dict, field: str) -> None:
    del document[field]

    with pytest.raises(ProfileError, match="required fields"):
        load_mapping_profile(json.dumps(document))


@pytest.mark.parametrize("duplicate_value", ["unique", "grouped_by_key"])
def test_duplicate_mode_fields_are_rejected_even_if_values_agree(
    v2_document: dict, duplicate_value: str
) -> None:
    data = json.dumps(v2_document)
    data = data.replace(
        '"reconciliation_mode": "unique"',
        f'"reconciliation_mode": "unique", "reconciliation_mode": "{duplicate_value}"',
    )

    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_export_formatting_preserves_v1_fields_with_v3_schema_additions(
    mode: ReconciliationMode,
) -> None:
    expected = V1_PROFILE.replace(b'"version": 1', b'"version": 3').replace(
        b'  "amount_tolerance": "0.0100"\n',
        (
            f'  "amount_tolerance": "0.0100",\n  "reconciliation_mode": "{mode.value}",\n'
            '  "key_normalization": [\n'
            "    {\n"
            '      "casefold": false,\n'
            '      "collapse_whitespace": false,\n'
            '      "remove_punctuation": false,\n'
            '      "strip_leading_zeros": false\n'
            "    },\n"
            "    {\n"
            '      "casefold": false,\n'
            '      "collapse_whitespace": false,\n'
            '      "remove_punctuation": false,\n'
            '      "strip_leading_zeros": false\n'
            "    }\n"
            "  ]\n"
        ).encode(),
    )

    data = export_mapping_profile(
        MAPPING, amount_tolerance=Decimal("0.0100"), reconciliation_mode=mode
    )

    assert data == expected
    assert data == export_mapping_profile(
        MAPPING, amount_tolerance=Decimal("0.0100"), reconciliation_mode=mode
    )
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
