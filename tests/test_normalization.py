"""Normalization semantics and independent collision preflight; no engine integration."""

import subprocess
import sys
from dataclasses import FrozenInstanceError
from decimal import Decimal
from itertools import permutations

import pytest

from tallydiff.models import CompositeKey, Source, SourceRecord
from tallydiff.normalization import (
    KeyNormalizationConfig,
    KeyNormalizationError,
    KeyNormalizationRules,
    NormalizationCollision,
    OriginalKeyEvidence,
    find_normalization_collisions,
    normalize_key,
)


def config(**rules: bool) -> KeyNormalizationConfig:
    return KeyNormalizationConfig((KeyNormalizationRules(**rules),))


def record(
    source_row: int,
    key: CompositeKey,
    source: Source = Source.A,
) -> SourceRecord:
    return SourceRecord(source, source_row, key, Decimal("12.34"), {"identifier": key[0]})


def test_default_configuration_preserves_exact_content() -> None:
    key = ("  AcMé-\t001  ",)
    assert normalize_key(key, KeyNormalizationConfig()) == key


def test_default_composite_rules_preserve_input_without_mutation() -> None:
    key = (" ACME   CORP ", "Straße/001", "0000")
    original = tuple(key)
    configuration = KeyNormalizationConfig((KeyNormalizationRules(),) * len(key))

    assert normalize_key(key, configuration) == original
    assert key == original


def test_normalization_returns_changed_content_without_changing_input() -> None:
    key = ("ACME-01", "001042")
    configuration = KeyNormalizationConfig(
        (
            KeyNormalizationRules(casefold=True, remove_punctuation=True),
            KeyNormalizationRules(strip_leading_zeros=True),
        )
    )

    assert normalize_key(key, configuration) == ("acme01", "1042")
    assert key == ("ACME-01", "001042")


def test_rules_and_configuration_are_immutable() -> None:
    rules = KeyNormalizationRules(casefold=True)
    configuration = KeyNormalizationConfig((rules,))

    with pytest.raises(FrozenInstanceError):
        rules.casefold = False
    with pytest.raises(FrozenInstanceError):
        configuration.component_rules = (KeyNormalizationRules(),)
    with pytest.raises(TypeError):
        configuration.component_rules[0] = KeyNormalizationRules()
    assert configuration.component_rules == (rules,)


@pytest.mark.parametrize(
    "name", ["casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros"]
)
@pytest.mark.parametrize("value", [None, 0, 1, "false", (), []])
def test_rule_flags_require_actual_booleans(name: str, value: object) -> None:
    with pytest.raises(TypeError, match=f"{name} must be a bool"):
        KeyNormalizationRules(**{name: value})


@pytest.mark.parametrize(
    "rules", [None, [], [KeyNormalizationRules()], KeyNormalizationRules(), {}, "casefold"]
)
def test_configuration_requires_an_immutable_rule_tuple(rules: object) -> None:
    with pytest.raises(TypeError, match="component_rules.*tuple of KeyNormalizationRules"):
        KeyNormalizationConfig(rules)


@pytest.mark.parametrize("rules", [(None,), (True,), ({},), ("casefold",)])
def test_configuration_rejects_wrong_rule_types(rules: object) -> None:
    with pytest.raises(TypeError, match="component_rules.*tuple of KeyNormalizationRules"):
        KeyNormalizationConfig(rules)


def test_configuration_rejects_empty_rule_tuple() -> None:
    with pytest.raises(ValueError, match="at least one rule set"):
        KeyNormalizationConfig(())


@pytest.mark.parametrize("configuration", [None, (), KeyNormalizationRules(), {}])
def test_normalize_rejects_wrong_configuration_types(configuration: object) -> None:
    with pytest.raises(TypeError, match="configuration must be a KeyNormalizationConfig"):
        normalize_key(("INV",), configuration)


@pytest.mark.parametrize("key", [None, "INV", ["INV"], {}, 1, (1,), ("INV", None)])
def test_normalize_rejects_wrong_key_types(key: object) -> None:
    with pytest.raises(TypeError, match="key must be a tuple of strings"):
        normalize_key(key, KeyNormalizationConfig())


def test_normalize_rejects_empty_composite_key() -> None:
    with pytest.raises(ValueError, match="at least one component"):
        normalize_key((), KeyNormalizationConfig())


@pytest.mark.parametrize(("key_length", "rule_count"), [(1, 2), (2, 1), (1, 3)])
def test_configuration_length_must_match_key_exactly(key_length: int, rule_count: int) -> None:
    configuration = KeyNormalizationConfig((KeyNormalizationRules(),) * rule_count)
    with pytest.raises(ValueError, match="configuration length must equal key length"):
        normalize_key(("INV",) * key_length, configuration)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("ACME", "acme"), ("AcMe", "acme"), ("Straße", "strasse"), ("ÉCOLE", "école")],
)
def test_unicode_casefold(value: str, expected: str) -> None:
    assert normalize_key((value,), config(casefold=True)) == (expected,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  ACME   CORP  ", "ACME CORP"),
        (" A\tB\n\r C ", "A B C"),
        ("\u00a0A\u2003\u202fB\u3000", "A B"),
    ],
)
def test_whitespace_collapse(value: str, expected: str) -> None:
    assert normalize_key((value,), config(collapse_whitespace=True)) == (expected,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ACME-01", "ACME01"),
        ("INV/1042", "INV1042"),
        ("A.B", "AB"),
        ("A_—(«“、”»)B", "AB"),
        ("École—東京", "École東京"),
        ("€$£¥-01", "€$£¥01"),
        ("A+−=×÷-B", "A+−=×÷B"),
        ("A-😀☀️B", "A😀☀️B"),
        (" A - B ", " A  B "),
        ("e\u0301-\u200b", "e\u0301\u200b"),
    ],
)
def test_only_unicode_punctuation_is_removed(value: str, expected: str) -> None:
    assert normalize_key((value,), config(remove_punctuation=True)) == (expected,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("001042", "1042"),
        ("0000", "0"),
        ("0", "0"),
        ("1042", "1042"),
        ("-001", "-001"),
        ("+001", "+001"),
        ("001A", "001A"),
        ("1.00", "1.00"),
        ("١٢٣", "١٢٣"),
        ("٠٠١", "٠٠١"),
        ("００１", "００１"),
        ("00١", "00١"),
        ("00²", "00²"),
        (" 001 ", " 001 "),
    ],
)
def test_leading_zeros_apply_only_to_ascii_digit_strings(value: str, expected: str) -> None:
    assert normalize_key((value,), config(strip_leading_zeros=True)) == (expected,)


def test_long_identifier_does_not_require_numeric_parsing() -> None:
    value = "0" * 5000 + "123456789" * 1000
    assert normalize_key((value,), config(strip_leading_zeros=True)) == ("123456789" * 1000,)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("casefold", "AcMé"),
        ("collapse_whitespace", " A\t  B "),
        ("remove_punctuation", "ACME-01/2.3"),
        ("strip_leading_zeros", "001042"),
    ],
)
def test_disabled_rules_preserve_text(name: str, value: str) -> None:
    assert normalize_key((value,), config(**{name: False})) == (value,)


@pytest.mark.parametrize(
    ("rules", "value", "expected"),
    [
        (KeyNormalizationRules(casefold=True, remove_punctuation=True), "ACME-01", "acme01"),
        (
            KeyNormalizationRules(collapse_whitespace=True, remove_punctuation=True),
            " A\t - \nB ",
            "A  B",
        ),
        (
            KeyNormalizationRules(collapse_whitespace=True, remove_punctuation=True),
            "- A -",
            " A ",
        ),
        (
            KeyNormalizationRules(remove_punctuation=True, strip_leading_zeros=True),
            "-001",
            "1",
        ),
        (
            KeyNormalizationRules(remove_punctuation=True, strip_leading_zeros=True),
            "00.10",
            "10",
        ),
    ],
)
def test_composition_follows_fixed_order(
    rules: KeyNormalizationRules, value: str, expected: str
) -> None:
    assert normalize_key((value,), KeyNormalizationConfig((rules,))) == (expected,)


def test_all_four_rules_compose_in_component_order() -> None:
    rules = KeyNormalizationRules(
        casefold=True,
        collapse_whitespace=True,
        remove_punctuation=True,
        strip_leading_zeros=True,
    )
    configuration = KeyNormalizationConfig((rules, rules, rules))

    assert normalize_key((" \tACME-01\n", " \t00-1042\n", " 0000 "), configuration) == (
        "acme01",
        "1042",
        "0",
    )


def test_component_rules_are_not_reordered_or_shared_between_positions() -> None:
    configuration = KeyNormalizationConfig(
        (KeyNormalizationRules(casefold=True), KeyNormalizationRules(remove_punctuation=True))
    )

    assert normalize_key(("ACME-01", "ACME-01"), configuration) == ("acme-01", "ACME01")


@pytest.mark.parametrize(
    ("value", "configuration"),
    [
        ("", KeyNormalizationConfig()),
        (" \t\u2003", KeyNormalizationConfig()),
        (" \t\u2003", config(collapse_whitespace=True)),
        ("---", config(remove_punctuation=True)),
        ("—/“”", config(remove_punctuation=True)),
        (" - \t- ", config(remove_punctuation=True)),
        (" - \t- ", config(collapse_whitespace=True, remove_punctuation=True)),
        ("", config(strip_leading_zeros=True)),
    ],
)
def test_blank_normalized_component_fails_visibly(
    value: str, configuration: KeyNormalizationConfig
) -> None:
    with pytest.raises(KeyNormalizationError, match="key component 1 normalizes to blank"):
        normalize_key((value,), configuration)


def test_blank_error_identifies_the_component_and_original_value() -> None:
    configuration = KeyNormalizationConfig(
        (KeyNormalizationRules(), KeyNormalizationRules(remove_punctuation=True))
    )
    with pytest.raises(KeyNormalizationError, match="component 2.*original value: '---'"):
        normalize_key(("ACME", "---"), configuration)


def test_distinct_original_keys_converge_into_a_collision() -> None:
    first, second = record(2, ("ACME-01",)), record(3, ("ACME01",))
    collisions = find_normalization_collisions(
        [first, second], config(remove_punctuation=True), source=Source.A
    )

    assert collisions == (
        NormalizationCollision(
            source=Source.A,
            normalized_key=("ACME01",),
            originals=(
                OriginalKeyEvidence(("ACME-01",), (first,)),
                OriginalKeyEvidence(("ACME01",), (second,)),
            ),
        ),
    )


@pytest.mark.parametrize(
    "configuration", [KeyNormalizationConfig(), config(remove_punctuation=True)]
)
def test_identical_original_duplicate_keys_are_not_normalization_collisions(
    configuration: KeyNormalizationConfig,
) -> None:
    records = [record(2, ("ACME-01",)), record(3, ("ACME-01",))]

    assert find_normalization_collisions(records, configuration, source=Source.A) == ()


def test_exact_configuration_does_not_create_collisions() -> None:
    records = [record(2, ("ACME-01",)), record(3, ("ACME01",))]

    assert find_normalization_collisions(records, KeyNormalizationConfig(), source=Source.A) == ()


def test_three_distinct_originals_and_all_duplicate_rows_are_retained() -> None:
    records = [
        record(8, ("ACME/01",)),
        record(6, ("ACME-01",)),
        record(3, ("ACME01",)),
        record(2, ("ACME-01",)),
    ]
    collisions = find_normalization_collisions(
        records, config(remove_punctuation=True), source=Source.A
    )

    assert len(collisions) == 1
    collision = collisions[0]
    assert collision.normalized_key == ("ACME01",)
    assert [original.original_key for original in collision.originals] == [
        ("ACME-01",),
        ("ACME/01",),
        ("ACME01",),
    ]
    assert [
        tuple(row.source_row for row in original.records) for original in collision.originals
    ] == [
        (2, 6),
        (8,),
        (3,),
    ]
    for original in collision.originals:
        for row in original.records:
            assert any(row is input_row for input_row in records)
            assert row.key == original.original_key


def test_multiple_collisions_and_evidence_order_are_independent_of_input_order() -> None:
    records = [
        record(9, ("Z-1",)),
        record(8, ("A1",)),
        record(7, ("A-1",)),
        record(2, ("Z1",)),
        record(3, ("Z-1",)),
    ]
    configuration = config(remove_punctuation=True)
    expected = find_normalization_collisions(records, configuration, source=Source.A)

    assert [collision.normalized_key for collision in expected] == [("A1",), ("Z1",)]
    assert [original.original_key for original in expected[0].originals] == [("A-1",), ("A1",)]
    assert [row.source_row for row in expected[1].originals[0].records] == [3, 9]
    for ordering in permutations(records):
        assert find_normalization_collisions(ordering, configuration, source=Source.A) == expected


def test_collision_retains_immutable_original_source_evidence() -> None:
    raw_fields = {"identifier": "ACME-01", "amount": "12.34", "note": "Original Évidence"}
    first = SourceRecord(Source.A, 2, ("ACME-01",), Decimal("12.34"), raw_fields)
    second = record(3, ("ACME01",))
    records = [first, second]
    collisions = find_normalization_collisions(
        records, config(remove_punctuation=True), source=Source.A
    )
    raw_fields["note"] = "Changed outside SourceRecord"
    records.clear()
    collision = collisions[0]
    evidence = collision.originals[0]
    retained = evidence.records[0]

    assert retained is first
    assert retained.key == evidence.original_key == ("ACME-01",)
    assert retained.source is Source.A
    assert retained.source_row == 2
    assert retained.amount == Decimal("12.34")
    assert dict(retained.raw_fields) == {
        "identifier": "ACME-01",
        "amount": "12.34",
        "note": "Original Évidence",
    }
    with pytest.raises(FrozenInstanceError):
        collision.normalized_key = ("changed",)
    with pytest.raises(FrozenInstanceError):
        evidence.original_key = ("changed",)
    with pytest.raises(FrozenInstanceError):
        retained.key = ("changed",)
    with pytest.raises(TypeError):
        collisions[0] = collision
    with pytest.raises(TypeError):
        collision.originals[0] = evidence
    with pytest.raises(TypeError):
        evidence.records[0] = second
    with pytest.raises(TypeError):
        retained.raw_fields["note"] = "changed"


@pytest.mark.parametrize("second_component", ["100", "101"])
def test_collision_requires_full_composite_key_convergence(second_component: str) -> None:
    records = [record(2, ("ACME-01", "100")), record(3, ("ACME01", second_component))]
    configuration = KeyNormalizationConfig(
        (KeyNormalizationRules(remove_punctuation=True), KeyNormalizationRules())
    )
    collisions = find_normalization_collisions(records, configuration, source=Source.A)

    if second_component == "100":
        assert len(collisions) == 1
        assert collisions[0].normalized_key == ("ACME01", "100")
    else:
        assert collisions == ()


def test_composite_collision_uses_the_rule_for_each_component() -> None:
    records = [record(2, ("ACME-01", "00100")), record(3, ("ACME01", "100"))]
    configuration = KeyNormalizationConfig(
        (
            KeyNormalizationRules(remove_punctuation=True),
            KeyNormalizationRules(strip_leading_zeros=True),
        )
    )

    (collision,) = find_normalization_collisions(records, configuration, source=Source.A)
    assert collision.normalized_key == ("ACME01", "100")
    assert [original.original_key for original in collision.originals] == [
        ("ACME-01", "00100"),
        ("ACME01", "100"),
    ]


def test_cross_source_convergence_is_not_a_same_source_collision() -> None:
    first = record(2, ("ACME-01",), Source.A)
    second = record(2, ("ACME01",), Source.B)
    configuration = config(remove_punctuation=True)

    assert normalize_key(first.key, configuration) == normalize_key(second.key, configuration)
    assert find_normalization_collisions([first], configuration, source=Source.A) == ()
    assert find_normalization_collisions([second], configuration, source=Source.B) == ()


@pytest.mark.parametrize("source", [Source.A, Source.B])
def test_each_source_is_checked_independently(source: Source) -> None:
    records = [record(2, ("ACME-01",), source), record(3, ("ACME01",), source)]

    (collision,) = find_normalization_collisions(
        records, config(remove_punctuation=True), source=source
    )
    assert collision.source is source
    assert all(row.source is source for original in collision.originals for row in original.records)


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("include_correct_source", [False, True])
def test_wrong_source_and_mixed_source_records_are_rejected(
    source: Source, include_correct_source: bool
) -> None:
    other = Source.B if source is Source.A else Source.A
    records = [record(3, ("ACME01",), other)]
    if include_correct_source:
        records.insert(0, record(2, ("ACME-01",), source))

    with pytest.raises(ValueError, match=f"belongs to File {other.value}, not File {source.value}"):
        find_normalization_collisions(records, config(remove_punctuation=True), source=source)


@pytest.mark.parametrize("source", ["A", "B", None, 1, True])
def test_preflight_requires_a_declared_source_enum_even_for_empty_input(source: object) -> None:
    with pytest.raises(TypeError, match="source must be a Source enum member"):
        find_normalization_collisions([], KeyNormalizationConfig(), source=source)


@pytest.mark.parametrize("configuration", [None, (), KeyNormalizationRules(), {}])
def test_preflight_rejects_wrong_configuration_even_for_empty_input(configuration: object) -> None:
    with pytest.raises(TypeError, match="configuration must be a KeyNormalizationConfig"):
        find_normalization_collisions([], configuration, source=Source.A)


@pytest.mark.parametrize("records", [None, 1, True])
def test_preflight_rejects_noniterable_input(records: object) -> None:
    with pytest.raises(TypeError, match="records must be an iterable of SourceRecord"):
        find_normalization_collisions(records, KeyNormalizationConfig(), source=Source.A)


@pytest.mark.parametrize("invalid_record", [None, {}, ("ACME",), "ACME", 1])
def test_preflight_rejects_non_source_records(invalid_record: object) -> None:
    with pytest.raises(TypeError, match="records must contain only SourceRecord"):
        find_normalization_collisions(
            [record(2, ("ACME",)), invalid_record], KeyNormalizationConfig(), source=Source.A
        )


@pytest.mark.parametrize("second_key", [("ACME-01",), ("ACME01",)])
def test_preflight_rejects_duplicate_source_row_numbers(second_key: CompositeKey) -> None:
    with pytest.raises(ValueError, match="File A contains duplicate source row 2"):
        find_normalization_collisions(
            [record(2, ("ACME-01",)), record(2, second_key)],
            config(remove_punctuation=True),
            source=Source.A,
        )


def test_preflight_rejects_component_count_mismatch_in_any_record() -> None:
    with pytest.raises(ValueError, match="configuration length must equal key length"):
        find_normalization_collisions(
            [record(2, ("ACME",)), record(3, ("ACME", "100"))],
            KeyNormalizationConfig(),
            source=Source.A,
        )


def test_preflight_reports_blank_result_with_source_row_and_component_context() -> None:
    with pytest.raises(KeyNormalizationError, match="File A source row 7: key component 1.*blank"):
        find_normalization_collisions(
            [record(7, ("---",))], config(remove_punctuation=True), source=Source.A
        )


def test_preflight_accepts_empty_input() -> None:
    assert find_normalization_collisions([], KeyNormalizationConfig(), source=Source.A) == ()


def test_preflight_consumes_one_pass_iterable_once() -> None:
    records = [record(2, ("ACME-01",)), record(3, ("ACME01",))]
    consumed = []

    def once():
        for row in records:
            consumed.append(row.source_row)
            yield row

    (collision,) = find_normalization_collisions(
        once(), config(remove_punctuation=True), source=Source.A
    )
    assert consumed == [2, 3]
    assert collision.normalized_key == ("ACME01",)


def test_import_and_preflight_leave_engine_exact_matching_and_duplicates_unchanged() -> None:
    from tallydiff.engine import reconcile
    from tallydiff.models import FindingCategory, ReconciliationMode

    first, duplicate, canonical = (
        record(2, ("ACME-01",)),
        record(3, ("ACME-01",)),
        record(4, ("ACME01",)),
    )
    records_a = [first, duplicate, canonical]
    records_b = [record(2, ("ACME01",), Source.B)]

    for mode in (ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY):
        before = reconcile(records_a, records_b, mode=mode)
        (collision,) = find_normalization_collisions(
            records_a, config(remove_punctuation=True), source=Source.A
        )
        assert collision.normalized_key == ("ACME01",)
        after = reconcile(records_a, records_b, mode=mode)

        assert after == before
        assert [finding.key for finding in after.findings] == [("ACME-01",), ("ACME01",)]
        assert after.findings[0].rows_a == (first, duplicate)
        assert after.findings[0].category is (
            FindingCategory.DUPLICATE_AMBIGUOUS
            if mode is ReconciliationMode.UNIQUE
            else FindingCategory.A_ONLY
        )
        assert after.findings[1].category is FindingCategory.EXACT_MATCH


def test_importing_normalization_does_not_change_engine_behavior() -> None:
    script = """
from decimal import Decimal

from tallydiff import FindingCategory, Source, SourceRecord, reconcile

first = SourceRecord(Source.A, 2, ("ACME-01",), Decimal("1"))
second = SourceRecord(Source.B, 2, ("ACME01",), Decimal("1"))
before = reconcile([first], [second])
assert before.key_normalization is None

import tallydiff.normalization

assert reconcile([first], [second]) == before
assert [finding.category for finding in before.findings] == [
    FindingCategory.A_ONLY, FindingCategory.B_ONLY
]
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr
