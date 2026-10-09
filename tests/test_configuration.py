"""Secondary mapping contracts and unchanged primary column selection behavior."""

from dataclasses import FrozenInstanceError, replace

import pytest

from tallydiff import ColumnMapping, ComparisonFieldMapping

BASE = ColumnMapping((("invoice", "reference"),), "amount", "gross")
DEPARTMENT = ComparisonFieldMapping("Department", "Cost Center")
POSTING_DATE = ComparisonFieldMapping("Posting Date", "Document Date")
COLUMNS_A = ("invoice", "vendor", "amount", "Department", "Posting Date", "Currency")
COLUMNS_B = ("reference", "supplier", "gross", "Cost Center", "Document Date", "Currency Code")


@pytest.mark.parametrize(
    ("file_a", "file_b", "complete"),
    [
        ("Department", "Cost Center", True),
        (" Department ", " Cost Center ", True),
        (None, "Cost Center", False),
        ("Department", None, False),
        (None, None, False),
        ("", "Cost Center", False),
        ("Department", "", False),
        (" \t", "Cost Center", False),
        ("Department", "\u2003", False),
    ],
)
def test_comparison_mapping_retains_exact_editable_selections(file_a, file_b, complete) -> None:
    mapping = ComparisonFieldMapping(file_a, file_b)

    assert mapping.file_a == file_a
    assert mapping.file_b == file_b
    assert mapping.is_complete is complete


@pytest.mark.parametrize("side", ["file_a", "file_b"])
@pytest.mark.parametrize("value", [0, True, 1.5, [], {}, ("Department",), object()])
def test_comparison_mapping_rejects_non_string_selections(side, value) -> None:
    with pytest.raises(TypeError, match=f"{side} must be a str or None"):
        replace(DEPARTMENT, **{side: value})


@pytest.mark.parametrize("side", ["file_a", "file_b"])
def test_comparison_mapping_is_frozen_and_slotted(side) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(DEPARTMENT, side, "changed")
    assert not hasattr(DEPARTMENT, "__dict__")


def test_column_mapping_preserves_three_positional_arguments_and_empty_default() -> None:
    mapping = ColumnMapping(BASE.key_pairs, BASE.amount_a, BASE.amount_b)

    assert mapping == BASE == replace(BASE, comparison_fields=())
    assert mapping.comparison_fields == ()
    assert mapping.comparisons_a == mapping.comparisons_b == ()


def test_column_mapping_comparison_fields_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        ColumnMapping(BASE.key_pairs, BASE.amount_a, BASE.amount_b, (DEPARTMENT,))


def test_column_mapping_retains_comparison_order_and_direction() -> None:
    comparisons = (POSTING_DATE, DEPARTMENT)
    mapping = replace(BASE, comparison_fields=comparisons)

    assert mapping.comparison_fields is comparisons
    assert mapping.comparisons_a == ("Posting Date", "Department")
    assert mapping.comparisons_b == ("Document Date", "Cost Center")
    assert mapping.problem(COLUMNS_A, COLUMNS_B) is None
    assert mapping != replace(BASE, comparison_fields=tuple(reversed(comparisons)))
    with pytest.raises(FrozenInstanceError):
        mapping.comparison_fields = ()


def test_directional_properties_retain_incomplete_selections() -> None:
    comparisons = (
        ComparisonFieldMapping(None, "Cost Center"),
        ComparisonFieldMapping("Date", None),
    )
    mapping = replace(BASE, comparison_fields=comparisons)

    assert mapping.comparisons_a == (None, "Date")
    assert mapping.comparisons_b == ("Cost Center", None)


@pytest.mark.parametrize(
    "comparisons",
    [None, [], [DEPARTMENT], {}, "Department", (None,), ("Department",), (DEPARTMENT, object())],
)
def test_column_mapping_rejects_wrong_comparison_container_or_member(comparisons) -> None:
    with pytest.raises(
        TypeError, match="comparison_fields must be a tuple of ComparisonFieldMapping"
    ):
        replace(BASE, comparison_fields=comparisons)


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize(
    "comparison",
    [
        ComparisonFieldMapping(None, "Document Date"),
        ComparisonFieldMapping("Posting Date", None),
        ComparisonFieldMapping("", "Document Date"),
        ComparisonFieldMapping("Posting Date", " \t"),
        ComparisonFieldMapping("missing", "Document Date"),
        ComparisonFieldMapping("Posting Date", "missing"),
    ],
)
def test_incomplete_or_missing_comparison_has_a_numbered_message(index, comparison) -> None:
    comparisons = (DEPARTMENT,) * (index - 1) + (comparison,)
    mapping = replace(BASE, comparison_fields=comparisons)

    # Blank names remain incomplete even if supplied among available columns.
    assert mapping.problem((*COLUMNS_A, ""), (*COLUMNS_B, " \t")) == (
        f"Choose a File A and File B column for comparison field {index}."
    )


@pytest.mark.parametrize(
    ("side", "second"),
    [
        ("A", ComparisonFieldMapping("Department", "Document Date")),
        ("B", ComparisonFieldMapping("Posting Date", "Cost Center")),
    ],
)
def test_repeated_secondary_columns_are_rejected_independently_by_side(side, second) -> None:
    mapping = replace(BASE, comparison_fields=(DEPARTMENT, second))

    assert mapping.problem(COLUMNS_A, COLUMNS_B) == (
        f"Each File {side} comparison column must be selected only once."
    )


@pytest.mark.parametrize(
    ("side", "comparison"),
    [
        ("A", ComparisonFieldMapping("amount", "Cost Center")),
        ("B", ComparisonFieldMapping("Department", "gross")),
    ],
)
def test_secondary_amount_overlap_is_rejected_independently_by_side(side, comparison) -> None:
    mapping = replace(BASE, comparison_fields=(comparison,))

    assert mapping.problem(COLUMNS_A, COLUMNS_B) == (
        f"File {side} comparison fields must not use the amount column."
    )


@pytest.mark.parametrize(
    "comparison",
    [
        ComparisonFieldMapping("invoice", "reference"),
        ComparisonFieldMapping("invoice", "Cost Center"),
        ComparisonFieldMapping("Department", "reference"),
    ],
)
def test_secondary_fields_may_overlap_matching_keys(comparison) -> None:
    mapping = replace(BASE, comparison_fields=(comparison,))

    assert mapping.problem(COLUMNS_A, COLUMNS_B) is None


@pytest.mark.parametrize("comparisons", [(), (DEPARTMENT,)])
def test_primary_amount_may_still_supply_a_key(comparisons) -> None:
    mapping = ColumnMapping(
        (("amount", "gross"),), "amount", "gross", comparison_fields=comparisons
    )

    assert mapping.problem(COLUMNS_A, COLUMNS_B) is None


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        (ColumnMapping((), "amount", "gross"), "Add at least one matching key field."),
        (
            ColumnMapping(((None, "reference"),), "amount", "gross"),
            "Choose a File A and File B column for key field 1.",
        ),
        (
            ColumnMapping((("invoice", None),), "amount", "gross"),
            "Choose a File A and File B column for key field 1.",
        ),
        (
            ColumnMapping((("invoice", "reference"), ("vendor", "missing")), "amount", "gross"),
            "Choose a File A and File B column for key field 2.",
        ),
        (
            ColumnMapping((("invoice", "reference"), ("invoice", "supplier")), "amount", "gross"),
            "Each File A key column must be selected only once.",
        ),
        (
            ColumnMapping((("invoice", "reference"), ("vendor", "reference")), "amount", "gross"),
            "Each File B key column must be selected only once.",
        ),
        (ColumnMapping(BASE.key_pairs, None, "gross"), "Choose an amount column for both files."),
        (ColumnMapping(BASE.key_pairs, "amount", None), "Choose an amount column for both files."),
        (
            ColumnMapping(BASE.key_pairs, "missing", "gross"),
            "Choose an amount column for both files.",
        ),
    ],
)
def test_zero_comparison_validation_messages_are_unchanged(mapping, message) -> None:
    assert mapping.comparison_fields == ()
    assert mapping.problem(COLUMNS_A, COLUMNS_B) == message


def test_primary_validation_keeps_precedence_over_secondary_validation() -> None:
    mapping = ColumnMapping((), None, None, comparison_fields=(ComparisonFieldMapping(None, None),))

    assert mapping.problem(COLUMNS_A, COLUMNS_B) == "Add at least one matching key field."


def test_comparison_column_names_are_not_silently_trimmed() -> None:
    mapping = replace(
        BASE, comparison_fields=(ComparisonFieldMapping(" Department ", "Cost Center"),)
    )

    assert mapping.problem(COLUMNS_A, COLUMNS_B) == (
        "Choose a File A and File B column for comparison field 1."
    )
    assert mapping.problem((*COLUMNS_A, " Department "), COLUMNS_B) is None
