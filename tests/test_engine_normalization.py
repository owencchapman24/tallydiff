"""Explicit engine normalization, collision blocking, and exact-mode compatibility."""

from collections import Counter
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from itertools import permutations
from typing import get_type_hints

import pytest

import tallydiff
import tallydiff.engine as engine
from tallydiff import (
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationError,
    KeyNormalizationRules,
    NormalizationCollision,
    NormalizationCollisionError,
    OriginalKeyEvidence,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    reconcile,
)
from tallydiff.models import CompositeKey
from tallydiff.normalization import find_normalization_collisions


def config(**rules: bool) -> KeyNormalizationConfig:
    return KeyNormalizationConfig((KeyNormalizationRules(**rules),))


def record(source: Source, row: int, key: CompositeKey, amount: str = "1") -> SourceRecord:
    return SourceRecord(source, row, key, Decimal(amount), {"key": repr(key), "amount": amount})


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    ("configuration", "key_a", "key_b", "matching_key"),
    [
        (config(casefold=True), ("Straße",), ("STRASSE",), ("strasse",)),
        (
            config(collapse_whitespace=True),
            ("ACME\t \u2003CORP",),
            ("ACME CORP",),
            ("ACME CORP",),
        ),
        (config(remove_punctuation=True), ("ACME-01",), ("ACME01",), ("ACME01",)),
        (config(strip_leading_zeros=True), ("001042",), ("1042",), ("1042",)),
        (
            config(casefold=True, remove_punctuation=True),
            ("ACME-01",),
            ("acme01",),
            ("acme01",),
        ),
    ],
)
def test_cross_source_normalization_matches_under_both_modes(
    mode: ReconciliationMode,
    configuration: KeyNormalizationConfig,
    key_a: CompositeKey,
    key_b: CompositeKey,
    matching_key: CompositeKey,
) -> None:
    first, second = record(Source.A, 4, key_a), record(Source.B, 7, key_b)
    result = reconcile([first], [second], mode=mode, key_normalization=configuration)

    assert result.key_normalization is configuration
    assert result.mode is mode
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.key == matching_key
    assert finding.category is FindingCategory.EXACT_MATCH
    assert finding.rows_a[0] is first
    assert finding.rows_b[0] is second
    assert first.key == key_a
    assert second.key == key_b


def test_composite_matching_uses_different_rules_in_component_order() -> None:
    configuration = KeyNormalizationConfig(
        (
            KeyNormalizationRules(casefold=True),
            KeyNormalizationRules(collapse_whitespace=True),
            KeyNormalizationRules(remove_punctuation=True, strip_leading_zeros=True),
        )
    )
    first = record(Source.A, 2, ("Straße", "ACME\t CORP", "00-1042"))
    second = record(Source.B, 3, ("STRASSE", "ACME CORP", "1042"))

    result = reconcile([first], [second], key_normalization=configuration)

    assert result.findings[0].key == ("strasse", "ACME CORP", "1042")
    assert result.findings[0].category is FindingCategory.EXACT_MATCH
    assert first.key == ("Straße", "ACME\t CORP", "00-1042")


def test_all_four_rules_compose_in_the_engine() -> None:
    rules = KeyNormalizationRules(True, True, True, True)
    configuration = KeyNormalizationConfig((rules, rules))
    first = record(Source.A, 2, (" \tACME-01\n", " 00-1042 "))
    second = record(Source.B, 3, ("acme01", "1042"))

    result = reconcile([first], [second], key_normalization=configuration)

    assert result.findings[0].key == ("acme01", "1042")
    assert result.findings[0].category is FindingCategory.EXACT_MATCH


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    ("amount_b", "tolerance", "category", "delta"),
    [
        ("10", "0", FindingCategory.EXACT_MATCH, "0"),
        ("9", "0", FindingCategory.AMOUNT_MISMATCH, "1"),
        ("10.01", "0.01", FindingCategory.WITHIN_TOLERANCE, "-0.01"),
        ("10.02", "0.01", FindingCategory.AMOUNT_MISMATCH, "-0.02"),
    ],
)
def test_normalized_amount_classification_and_true_deltas(
    mode: ReconciliationMode,
    amount_b: str,
    tolerance: str,
    category: FindingCategory,
    delta: str,
) -> None:
    result = reconcile(
        [record(Source.A, 2, ("ACME-01",), "10")],
        [record(Source.B, 3, ("ACME01",), amount_b)],
        amount_tolerance=Decimal(tolerance),
        mode=mode,
        key_normalization=config(remove_punctuation=True),
    )

    assert result.findings[0].category is category
    assert result.findings[0].key == ("ACME01",)
    assert result.total_a == Decimal("10")
    assert result.total_b == Decimal(amount_b)
    assert result.control_difference == result.finding_delta_sum == Decimal(delta)
    assert result.findings[0].delta == Decimal(delta)
    if category is FindingCategory.WITHIN_TOLERANCE:
        assert result.tolerated_delta_total == Decimal(delta)
        assert result.exceptions == ()


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("amount", ["0", "10"])
def test_normalized_one_sided_presence_remains_an_exception(
    mode: ReconciliationMode, source: Source, amount: str
) -> None:
    row = record(source, 2, ("ACME-01",), amount)
    result = reconcile(
        [row] if source is Source.A else [],
        [row] if source is Source.B else [],
        amount_tolerance=Decimal("100"),
        mode=mode,
        key_normalization=config(remove_punctuation=True),
    )

    assert result.findings[0].key == ("ACME01",)
    assert result.findings[0].category is (
        FindingCategory.A_ONLY if source is Source.A else FindingCategory.B_ONLY
    )
    assert result.exceptions == result.findings
    assert result.control_difference == result.finding_delta_sum


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize(
    ("amounts_a", "amounts_b", "grouped_category"),
    [
        (("5", "7"), ("12",), FindingCategory.EXACT_MATCH),
        (("5", "7"), ("12.01",), FindingCategory.WITHIN_TOLERANCE),
        (("5", "7"), ("13",), FindingCategory.AMOUNT_MISMATCH),
        (("5", "-5"), (), FindingCategory.A_ONLY),
        ((), ("5", "-5"), FindingCategory.B_ONLY),
        (("5", "7"), ("6", "6"), FindingCategory.EXACT_MATCH),
    ],
)
def test_identical_original_duplicates_keep_existing_mode_semantics(
    mode: ReconciliationMode,
    amounts_a: tuple[str, ...],
    amounts_b: tuple[str, ...],
    grouped_category: FindingCategory,
) -> None:
    records_a = [
        record(Source.A, row, ("ACME-01",), value) for row, value in enumerate(amounts_a, 2)
    ]
    records_b = [
        record(Source.B, row, ("ACME01",), value) for row, value in enumerate(amounts_b, 2)
    ]
    result = reconcile(
        records_a,
        records_b,
        amount_tolerance=Decimal("0.01"),
        mode=mode,
        key_normalization=config(remove_punctuation=True),
    )

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.key == ("ACME01",)
    assert finding.category is (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else grouped_category
    )
    assert finding.rows_a == tuple(records_a)
    assert finding.rows_b == tuple(records_b)
    assert result.control_difference == result.finding_delta_sum == finding.delta


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("sources", [(Source.A,), (Source.B,), (Source.A, Source.B)])
@pytest.mark.parametrize(
    ("configuration", "originals"),
    [
        (config(casefold=True), ("ACME", "acme")),
        (config(collapse_whitespace=True), ("ACME  CORP", "ACME CORP")),
        (config(remove_punctuation=True), ("ACME-01", "ACME01")),
        (config(strip_leading_zeros=True), ("001", "1")),
        (config(casefold=True, remove_punctuation=True), ("ACME-01", "acme01")),
        (
            config(
                casefold=True,
                collapse_whitespace=True,
                remove_punctuation=True,
                strip_leading_zeros=True,
            ),
            (" \t00-1\n", "1"),
        ),
    ],
)
def test_each_collision_rule_blocks_both_modes_and_retains_both_sources(
    mode: ReconciliationMode,
    sources: tuple[Source, ...],
    configuration: KeyNormalizationConfig,
    originals: tuple[str, str],
) -> None:
    records_a = (
        [record(Source.A, 8, (originals[0],)), record(Source.A, 3, (originals[1],))]
        if Source.A in sources
        else []
    )
    records_b = (
        [record(Source.B, 9, (originals[0],)), record(Source.B, 2, (originals[1],))]
        if Source.B in sources
        else []
    )

    with pytest.raises(NormalizationCollisionError) as caught:
        reconcile(records_a, records_b, mode=mode, key_normalization=configuration)

    error = caught.value
    assert isinstance(error, ValueError)
    assert error.collisions_a == find_normalization_collisions(
        records_a, configuration, source=Source.A
    )
    assert error.collisions_b == find_normalization_collisions(
        records_b, configuration, source=Source.B
    )
    assert len(error.collisions_a) == int(Source.A in sources)
    assert len(error.collisions_b) == int(Source.B in sources)
    assert f"File A: {len(error.collisions_a)}" in str(error)
    assert f"File B: {len(error.collisions_b)}" in str(error)


def test_collision_error_preserves_deterministic_full_original_evidence() -> None:
    raw = {"identifier": "Z-1", "amount": "12.34", "note": "Original Évidence"}
    retained = SourceRecord(Source.A, 9, ("Z-1",), Decimal("12.34"), raw)
    records_a = [
        retained,
        record(Source.A, 3, ("Z-1",)),
        record(Source.A, 2, ("Z1",)),
        record(Source.A, 7, ("A-1",)),
        record(Source.A, 6, ("A1",)),
    ]
    records_b = [record(Source.B, 5, ("B-2",)), record(Source.B, 4, ("B2",))]
    configuration = config(remove_punctuation=True)
    errors = []
    for a, b in ((records_a, records_b), (reversed(records_a), reversed(records_b))):
        with pytest.raises(NormalizationCollisionError) as caught:
            reconcile(a, b, key_normalization=configuration)
        errors.append(caught.value)

    assert errors[0].collisions_a == errors[1].collisions_a
    assert errors[0].collisions_b == errors[1].collisions_b
    assert [collision.normalized_key for collision in errors[0].collisions_a] == [("A1",), ("Z1",)]
    original = errors[0].collisions_a[1].originals[0]
    assert original.original_key == ("Z-1",)
    assert [row.source_row for row in original.records] == [3, 9]
    assert original.records[1] is retained
    raw["note"] = "changed externally"
    assert retained.raw_fields["note"] == "Original Évidence"
    assert retained.key == ("Z-1",)
    with pytest.raises(TypeError):
        retained.raw_fields["note"] = "changed"
    with pytest.raises(FrozenInstanceError):
        original.original_key = ("changed",)
    for collision in (*errors[0].collisions_a, *errors[0].collisions_b):
        for evidence in collision.originals:
            for row in evidence.records:
                assert any(row is input_row for input_row in (*records_a, *records_b))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_collision_stops_before_classification_findings_or_partial_result(
    mode: ReconciliationMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("Collision must block before classification or result construction")

    monkeypatch.setattr(engine, "_classify", forbidden)
    monkeypatch.setattr(engine, "ReconciliationFinding", forbidden)
    monkeypatch.setattr(engine, "ReconciliationResult", forbidden)

    with pytest.raises(NormalizationCollisionError):
        reconcile(
            [record(Source.A, 2, ("ACME-01",)), record(Source.A, 3, ("ACME01",))],
            [record(Source.B, 2, ("unrelated",))],
            mode=mode,
            key_normalization=config(remove_punctuation=True),
        )


def test_partial_composite_convergence_is_safe_and_matches_full_keys() -> None:
    configuration = KeyNormalizationConfig(
        (KeyNormalizationRules(remove_punctuation=True), KeyNormalizationRules())
    )
    records_a = [
        record(Source.A, 2, ("ACME-01", "100")),
        record(Source.A, 3, ("ACME01", "101")),
    ]
    records_b = [
        record(Source.B, 2, ("ACME01", "100")),
        record(Source.B, 3, ("ACME-01", "101")),
    ]
    result = reconcile(records_a, records_b, key_normalization=configuration)

    assert [finding.key for finding in result.findings] == [("ACME01", "100"), ("ACME01", "101")]
    assert all(finding.category is FindingCategory.EXACT_MATCH for finding in result.findings)


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("component_index", [0, 1])
def test_blank_normalization_error_keeps_source_row_and_component_context(
    source: Source, component_index: int
) -> None:
    key = ("---", "valid") if component_index == 0 else ("valid", "---")
    configuration = KeyNormalizationConfig((KeyNormalizationRules(remove_punctuation=True),) * 2)
    row = record(source, 17, key)
    with pytest.raises(
        KeyNormalizationError,
        match=f"File {source.value} source row 17: key component {component_index + 1}.*blank",
    ):
        reconcile(
            [row] if source is Source.A else [],
            [row] if source is Source.B else [],
            key_normalization=configuration,
        )


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize(("key_length", "rule_count"), [(1, 2), (2, 1)])
def test_config_arity_is_checked_on_every_input_key(
    source: Source, key_length: int, rule_count: int
) -> None:
    configuration = KeyNormalizationConfig((KeyNormalizationRules(),) * rule_count)
    rows = [
        record(source, 2, ("valid",) * rule_count),
        record(source, 7, ("invalid",) * key_length),
    ]
    with pytest.raises(
        ValueError, match=f"File {source.value} source row 7: configuration length.*key length"
    ):
        reconcile(
            rows if source is Source.A else [],
            rows if source is Source.B else [],
            key_normalization=configuration,
        )


def test_all_arities_are_validated_before_normalizing_either_source() -> None:
    with pytest.raises(ValueError, match="File B source row 8: configuration length"):
        reconcile(
            [record(Source.A, 2, ("---",))],
            [record(Source.B, 8, ("A", "B"))],
            key_normalization=config(remove_punctuation=True),
        )


def test_blank_component_blocks_before_raising_a_collision_from_the_other_source() -> None:
    with pytest.raises(KeyNormalizationError, match="File B source row 8: key component 1"):
        reconcile(
            [record(Source.A, 2, ("ACME-01",)), record(Source.A, 3, ("ACME01",))],
            [record(Source.B, 8, ("---",))],
            key_normalization=config(remove_punctuation=True),
        )


@pytest.mark.parametrize("configuration", [(), {}, "casefold", True, 0, KeyNormalizationRules()])
def test_wrong_config_types_are_rejected_before_consuming_inputs(configuration: object) -> None:
    def forbidden():
        pytest.fail("Invalid configuration must fail before consuming inputs")
        yield

    with pytest.raises(
        TypeError, match="key_normalization must be a KeyNormalizationConfig or None"
    ):
        reconcile(forbidden(), forbidden(), key_normalization=configuration)


@pytest.mark.parametrize("configuration", [None, config(remove_punctuation=True)])
@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("problem", ["wrong-source", "duplicate-row"])
def test_source_integrity_is_validated_before_normalization(
    configuration: KeyNormalizationConfig | None, source: Source, problem: str
) -> None:
    other = Source.B if source is Source.A else Source.A
    if problem == "wrong-source":
        invalid = [record(other, 2, ("---",))]
        message = f"belongs to File {other.value}, not File {source.value}"
    else:
        invalid = [record(source, 2, ("---",)), record(source, 2, ("ACME",))]
        message = f"File {source.value} contains duplicate source row 2"

    with pytest.raises(ValueError, match=message):
        reconcile(
            invalid if source is Source.A else [],
            invalid if source is Source.B else [],
            key_normalization=configuration,
        )


def test_both_sources_are_validated_before_normalizing_file_a() -> None:
    with pytest.raises(ValueError, match="belongs to File A, not File B"):
        reconcile(
            [record(Source.A, 2, ("---",))],
            [record(Source.A, 7, ("ACME",))],
            key_normalization=config(remove_punctuation=True),
        )


@pytest.mark.parametrize("configuration", [None, KeyNormalizationConfig()])
def test_non_source_records_are_rejected_clearly(
    configuration: KeyNormalizationConfig | None,
) -> None:
    with pytest.raises(TypeError, match="records must contain only SourceRecord"):
        reconcile([None], [], key_normalization=configuration)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_normalized_findings_preserve_rows_totals_order_and_one_pass_inputs(
    mode: ReconciliationMode,
) -> None:
    configuration = KeyNormalizationConfig(
        (
            KeyNormalizationRules(casefold=True, remove_punctuation=True),
            KeyNormalizationRules(collapse_whitespace=True, strip_leading_zeros=True),
        )
    )
    records_a = [
        record(Source.A, 4, ("Z-9", " \t001 "), "2"),
        record(Source.A, 5, ("A-1", "00100"), "3"),
        record(Source.A, 2, ("A-1", "00100"), "10"),
        record(Source.A, 9, ("M-2", "00200"), "5"),
    ]
    records_b = [
        record(Source.B, 3, ("a1", "100"), "13.01"),
        record(Source.B, 8, ("z9", "1"), "2"),
        record(Source.B, 2, ("B-1", "3"), "0"),
    ]
    originals = [(row.key, row.amount, dict(row.raw_fields)) for row in (*records_a, *records_b)]

    class Once:
        def __init__(self, rows):
            self.rows = rows
            self.calls = 0

        def __iter__(self):
            self.calls += 1
            assert self.calls == 1
            yield from self.rows

    a, b = Once(records_a), Once(records_b)
    result = reconcile(
        a, b, mode=mode, amount_tolerance=Decimal("0.01"), key_normalization=configuration
    )
    assert a.calls == b.calls == 1
    assert result.total_a == Decimal("20")
    assert result.total_b == Decimal("15.01")
    assert result.control_difference == result.finding_delta_sum == Decimal("4.99")
    assert [finding.key for finding in result.findings] == [
        ("a1", "100"),
        ("b1", "3"),
        ("m2", "200"),
        ("z9", "1"),
    ]
    accounted = [row for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)]
    assert Counter(map(id, accounted)) == Counter(map(id, (*records_a, *records_b)))
    assert [row.source_row for row in result.findings[0].rows_a] == [2, 5]
    assert originals == [
        (row.key, row.amount, dict(row.raw_fields)) for row in (*records_a, *records_b)
    ]
    assert result.tolerated_delta_total == (
        Decimal("-0.01") if mode is ReconciliationMode.GROUPED_BY_KEY else Decimal("0")
    )

    for ordering in permutations(records_a):
        assert (
            reconcile(
                ordering,
                reversed(records_b),
                mode=mode,
                amount_tolerance=Decimal("0.01"),
                key_normalization=configuration,
            )
            == result
        )


@pytest.mark.parametrize("cancel_large_amounts", [False, True])
def test_normalized_grouped_arithmetic_ignores_callers_decimal_context(
    cancel_large_amounts: bool,
) -> None:
    records_a = [
        record(Source.A, 1, ("INV-1",), "1E+1000"),
        record(Source.A, 2, ("INV-1",), "0.0001"),
    ]
    records_b = [
        record(Source.B, 1, ("INV1",), "1E+1000"),
        record(Source.B, 2, ("INV1",), "0.0002"),
    ]
    if cancel_large_amounts:
        records_a.append(record(Source.A, 3, ("INV-1",), "-1E+1000"))
        records_b.append(record(Source.B, 3, ("INV1",), "-1E+1000"))
    prefix = "0" if cancel_large_amounts else "1" + "0" * 1000
    expected_a, expected_b = Decimal(prefix + ".0001"), Decimal(prefix + ".0002")
    configuration = config(remove_punctuation=True)

    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        before = context.copy()
        result = reconcile(
            iter(records_a),
            iter(records_b),
            amount_tolerance=Decimal("0.0001"),
            mode=ReconciliationMode.GROUPED_BY_KEY,
            key_normalization=configuration,
        )

        assert result.total_a == result.findings[0].amount_a == expected_a
        assert result.total_b == result.findings[0].amount_b == expected_b
        assert result.control_difference == result.finding_delta_sum == Decimal("-0.0001")
        assert result.tolerated_delta_total == Decimal("-0.0001")
        assert result.findings[0].category is FindingCategory.WITHIN_TOLERANCE
        assert context.prec == before.prec
        assert context.Emax == before.Emax
        assert context.Emin == before.Emin
        assert context.traps == before.traps
        assert context.flags == before.flags


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("tolerance", ["0", "0.01"])
def test_none_and_explicit_exact_rules_preserve_all_default_categories(
    mode: ReconciliationMode, tolerance: str
) -> None:
    records_a = [
        record(Source.A, 1, ("EXACT",), "1"),
        record(Source.A, 2, ("MISMATCH",), "2"),
        record(Source.A, 3, ("TOLERATED",), "1"),
        record(Source.A, 4, ("DUP",), "1"),
        record(Source.A, 5, ("DUP",), "2"),
        record(Source.A, 6, ("A-ONLY",), "0"),
        record(Source.A, 7, ("A-ZERO",), "5"),
        record(Source.A, 8, ("A-ZERO",), "-5"),
        record(Source.A, 9, ("ACME-01",), "1"),
        record(Source.A, 10, ("HUGE",), "1E+1000"),
    ]
    records_b = [
        record(Source.B, 1, ("EXACT",), "1"),
        record(Source.B, 2, ("MISMATCH",), "1"),
        record(Source.B, 3, ("TOLERATED",), "1.005"),
        record(Source.B, 4, ("DUP",), "3"),
        record(Source.B, 6, ("B-ONLY",), "0"),
        record(Source.B, 7, ("B-ZERO",), "7"),
        record(Source.B, 8, ("B-ZERO",), "-7"),
        record(Source.B, 9, ("ACME01",), "1"),
        record(Source.B, 10, ("HUGE",), "1E+1000"),
    ]
    options = {"mode": mode, "amount_tolerance": Decimal(tolerance)}
    default = reconcile(records_a, records_b, **options)
    explicit_none = reconcile(
        iter(records_a), (row for row in records_b), key_normalization=None, **options
    )
    exact_config = KeyNormalizationConfig()
    explicit_exact = reconcile(records_a, records_b, key_normalization=exact_config, **options)

    assert default == explicit_none
    assert explicit_exact == replace(default, key_normalization=exact_config)
    assert default.key_normalization is None
    categories = {finding.key[0]: finding.category for finding in default.findings}
    assert categories["EXACT"] is categories["HUGE"] is FindingCategory.EXACT_MATCH
    assert categories["MISMATCH"] is FindingCategory.AMOUNT_MISMATCH
    assert categories["TOLERATED"] is (
        FindingCategory.WITHIN_TOLERANCE if tolerance == "0.01" else FindingCategory.AMOUNT_MISMATCH
    )
    assert categories["DUP"] is (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.EXACT_MATCH
    )
    assert categories["A-ONLY"] is categories["ACME-01"] is FindingCategory.A_ONLY
    assert categories["B-ONLY"] is categories["ACME01"] is FindingCategory.B_ONLY
    assert categories["A-ZERO"] is (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.A_ONLY
    )
    assert categories["B-ZERO"] is (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.B_ONLY
    )
    assert default.control_difference == default.finding_delta_sum == Decimal("0.995")


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    "configuration", [None, KeyNormalizationConfig((KeyNormalizationRules(),) * 2)]
)
def test_empty_result_retains_explicit_configuration(
    mode: ReconciliationMode, configuration: KeyNormalizationConfig | None
) -> None:
    result = reconcile([], [], mode=mode, key_normalization=configuration)
    assert result.key_normalization is configuration
    assert result.mode is mode
    assert result.findings == ()
    assert result.total_a == result.total_b == Decimal("0")


def test_result_field_preserves_constructor_positions_and_is_immutable() -> None:
    default = ReconciliationResult(Decimal("10"), Decimal("9"), ())
    legacy = ReconciliationResult(
        Decimal("10"), Decimal("9"), (), Decimal("0.01"), ReconciliationMode.GROUPED_BY_KEY
    )
    configuration = config(casefold=True)
    explicit = ReconciliationResult(
        Decimal("10"),
        Decimal("9"),
        (),
        Decimal("0.01"),
        ReconciliationMode.GROUPED_BY_KEY,
        configuration,
    )

    assert default.key_normalization is legacy.key_normalization is None
    assert default.amount_tolerance == Decimal("0")
    assert legacy.amount_tolerance == Decimal("0.01")
    assert legacy.mode is ReconciliationMode.GROUPED_BY_KEY
    assert explicit.key_normalization is configuration
    assert explicit != legacy
    with pytest.raises(FrozenInstanceError):
        explicit.key_normalization = None


@pytest.mark.parametrize("configuration", [(), {}, "casefold", True, 0, KeyNormalizationRules()])
def test_result_rejects_wrong_normalization_configuration_types(configuration: object) -> None:
    with pytest.raises(
        TypeError, match="key_normalization must be a KeyNormalizationConfig or None"
    ):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), key_normalization=configuration)


def test_public_types_keep_import_compatibility_and_runtime_annotations() -> None:
    from tallydiff.normalization import KeyNormalizationConfig as OriginalConfig
    from tallydiff.normalization import KeyNormalizationRules as OriginalRules
    from tallydiff.normalization_config import KeyNormalizationConfig as LowLevelConfig

    assert KeyNormalizationConfig is OriginalConfig is LowLevelConfig
    assert KeyNormalizationRules is OriginalRules
    assert (
        get_type_hints(ReconciliationResult)["key_normalization"] == KeyNormalizationConfig | None
    )
    for value in (
        KeyNormalizationRules,
        KeyNormalizationConfig,
        KeyNormalizationError,
        NormalizationCollision,
        OriginalKeyEvidence,
        NormalizationCollisionError,
    ):
        assert getattr(tallydiff, value.__name__) is value
        assert value.__name__ in tallydiff.__all__
