import json
from decimal import Decimal, localcontext

import pytest

from tallydiff import (
    ColumnMapping,
    MappingProfile,
    ProfileError,
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


@pytest.fixture
def document() -> dict:
    return json.loads(export_mapping_profile(MAPPING, amount_tolerance=Decimal("0.0100")))


def test_serialized_profile_has_only_versioned_configuration_fields(document: dict) -> None:
    assert document == {
        "format": "tallydiff-mapping-profile",
        "version": 1,
        "key_pairs": [
            {"file_a": "Vendor ID", "file_b": "Supplier"},
            {"file_a": "Invoice Number", "file_b": "Invoice Ref"},
        ],
        "amount_columns": {"file_a": "Invoice Amount", "file_b": "Gross Amount"},
        "amount_tolerance": "0.0100",
    }
    assert export_mapping_profile(MAPPING).endswith(b"\n")
    assert PresentationColumnMapping is ColumnMapping


@pytest.mark.parametrize("tolerance", ["0", "0.0100", "0.000000000000000000000012345678900"])
def test_round_trip_preserves_order_and_exact_decimal_digits(tolerance: str) -> None:
    with localcontext() as context:
        context.prec = 2
        data = export_mapping_profile(MAPPING, amount_tolerance=Decimal(tolerance))
        profile = load_mapping_profile(data)
        assert profile == MappingProfile(MAPPING, Decimal(tolerance))
        assert profile.mapping.key_pairs == MAPPING.key_pairs
        assert profile.amount_tolerance.as_tuple() == Decimal(tolerance).as_tuple()
        assert json.loads(data)["amount_tolerance"] == tolerance
        assert (
            export_mapping_profile(profile.mapping, amount_tolerance=profile.amount_tolerance)
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
        ("version", 2, "Unsupported profile version"),
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
    with pytest.raises(ProfileError, match="duplicate field names"):
        load_mapping_profile(data.replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(ProfileError, match="non-finite value"):
        load_mapping_profile(data.replace('"0.0100"', "NaN"))


def test_export_rejects_incomplete_configuration_and_non_decimal_tolerance() -> None:
    with pytest.raises(ProfileError, match="nonblank column name"):
        export_mapping_profile(ColumnMapping(((None, "Supplier"),), "amount", "gross"))
    with pytest.raises(ProfileError, match="must be a Decimal"):
        export_mapping_profile(MAPPING, amount_tolerance=0.01)


@pytest.mark.parametrize("side", ["A", "B"])
def test_compatibility_reports_exact_missing_directional_columns(side: str) -> None:
    profile = MappingProfile(MAPPING)
    a = COLUMNS_A[1:] if side == "A" else COLUMNS_A
    b = COLUMNS_B[1:] if side == "B" else COLUMNS_B
    missing = "Vendor ID" if side == "A" else "Supplier"
    with pytest.raises(ProfileError, match=f'File {side} is missing required column "{missing}"'):
        profile.validate_columns(a, b)


def test_compatibility_checks_amount_columns_and_never_swaps_sides_or_guesses() -> None:
    profile = MappingProfile(MAPPING)
    with pytest.raises(ProfileError) as error:
        profile.validate_columns(COLUMNS_A[:-1], COLUMNS_B[:-1])
    assert 'File A is missing required column "Invoice Amount"' in str(error.value)
    assert 'File B is missing required column "Gross Amount"' in str(error.value)
    with pytest.raises(ProfileError):
        profile.validate_columns(COLUMNS_B, COLUMNS_A)
    with pytest.raises(ProfileError):
        profile.validate_columns(("vendor id", *COLUMNS_A[1:]), COLUMNS_B)


def test_extra_columns_and_changed_header_order_remain_compatible() -> None:
    profile = MappingProfile(MAPPING)
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
