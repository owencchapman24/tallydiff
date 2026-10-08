from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from tallydiff import (
    FindingCategory,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    reconcile,
)


@pytest.mark.parametrize("amount", [0.1, 10, "10.00", True, None])
def test_amount_must_be_a_decimal(amount: object) -> None:
    with pytest.raises(TypeError, match="amount.*Decimal"):
        SourceRecord(Source.A, 1, ("INV",), amount)


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_amount_must_be_finite(amount: str) -> None:
    with pytest.raises(ValueError, match="amount.*finite"):
        SourceRecord(Source.A, 1, ("INV",), Decimal(amount))


@pytest.mark.parametrize("source", ["A", "B", "invalid", None])
def test_source_must_be_a_source_enum(source: object) -> None:
    with pytest.raises(TypeError, match="source.*Source"):
        SourceRecord(source, 1, ("INV",), Decimal("0"))


@pytest.mark.parametrize("source_row", [True, False, 1.5, "1", None])
def test_source_row_must_be_an_integer(source_row: object) -> None:
    with pytest.raises(TypeError, match="source_row.*integer"):
        SourceRecord(Source.A, source_row, ("INV",), Decimal("0"))


@pytest.mark.parametrize("source_row", [0, -1])
def test_source_row_must_be_positive(source_row: int) -> None:
    with pytest.raises(ValueError, match="source_row.*positive"):
        SourceRecord(Source.A, source_row, ("INV",), Decimal("0"))


@pytest.mark.parametrize("key", ["INV", ["INV"], (1,), (None,), None])
def test_key_must_be_an_immutable_tuple_of_strings(key: object) -> None:
    with pytest.raises(TypeError, match="key.*tuple.*strings"):
        SourceRecord(Source.A, 1, key, Decimal("0"))


@pytest.mark.parametrize("key", [(), ("",), ("INV", ""), (" \t",)])
def test_key_must_have_nonblank_components(key: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="key.*component"):
        SourceRecord(Source.A, 1, key, Decimal("0"))


def test_keys_are_compared_exactly_without_implicit_normalization() -> None:
    a = SourceRecord(Source.A, 1, (" INV",), Decimal("1.00"))
    b = SourceRecord(Source.B, 1, ("INV",), Decimal("1.00"))

    assert [finding.category for finding in reconcile([a], [b]).findings] == [
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
    ]


def test_raw_fields_are_an_immutable_snapshot() -> None:
    raw_fields = {"invoice": "INV", "amount": "1.00"}
    record = SourceRecord(Source.A, 1, ("INV",), Decimal("1.00"), raw_fields)

    raw_fields["amount"] = "2.00"

    assert dict(record.raw_fields) == {"invoice": "INV", "amount": "1.00"}
    with pytest.raises(TypeError):
        record.raw_fields["amount"] = "3.00"


@pytest.mark.parametrize("raw_fields", [None, [("amount", "1")], {"amount": 1}, {1: "1"}])
def test_raw_fields_must_be_a_string_mapping(raw_fields: object) -> None:
    with pytest.raises(TypeError, match="raw_fields.*mapping of strings"):
        SourceRecord(Source.A, 1, ("INV",), Decimal("1"), raw_fields)


def test_result_default_mode_preserves_existing_constructor_positions() -> None:
    default = ReconciliationResult(Decimal("10"), Decimal("9"), ())
    with_tolerance = ReconciliationResult(Decimal("10"), Decimal("9"), (), Decimal("0.01"))

    assert default.mode is with_tolerance.mode is ReconciliationMode.UNIQUE
    assert default.amount_tolerance == Decimal("0")
    assert with_tolerance.amount_tolerance == Decimal("0.01")


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_result_retains_explicit_mode(mode: ReconciliationMode) -> None:
    result = ReconciliationResult(Decimal("0"), Decimal("0"), (), mode=mode)

    assert result.mode is mode
    with pytest.raises(FrozenInstanceError):
        result.mode = ReconciliationMode.UNIQUE


@pytest.mark.parametrize(
    "mode",
    ["unique", "grouped_by_key", "invalid", "", None, 0, True, Decimal("0"), Source.A],
)
def test_result_rejects_invalid_mode_types(mode: object) -> None:
    with pytest.raises(TypeError, match="mode must be a ReconciliationMode enum member"):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), mode=mode)
