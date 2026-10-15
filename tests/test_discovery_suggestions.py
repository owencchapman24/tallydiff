"""Protect deterministic advisory suggestion evidence and visible ambiguity."""

import ast
import csv
import inspect
from collections import Counter
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from io import StringIO

import pytest

import tallydiff
import tallydiff.discovery as discovery
from scripts import discovery_benchmark, discovery_calibration
from tallydiff import Source, export_exceptions_csv, ingest_csv, ingest_xlsx, reconcile


def _snapshot(header, rows, *, source):
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return discovery.discover_csv(stream.getvalue(), source=source)


def _pair(header_a, rows_a, header_b, rows_b):
    return (
        _snapshot(header_a, rows_a, source=Source.A),
        _snapshot(header_b, rows_b, source=Source.B),
    )


def _ratio(numerator, denominator):
    return Fraction(numerator, denominator) if denominator else Fraction(-1)


def _key_evidence(suggestion):
    facts = suggestion.compatibility
    return (
        facts.overlap_distinct_count > 0,
        min(
            _ratio(facts.overlap_row_count_a, facts.eligible_row_count_a),
            _ratio(facts.overlap_row_count_b, facts.eligible_row_count_b),
        ),
        min(
            _ratio(facts.eligible_row_count_a, facts.row_count_a),
            _ratio(facts.eligible_row_count_b, facts.row_count_b),
        ),
        min(
            _ratio(facts.distinct_key_count_a, facts.eligible_row_count_a),
            _ratio(facts.distinct_key_count_b, facts.eligible_row_count_b),
        ),
        facts.overlap_distinct_count,
        sum(
            header.exact_normalized_match and bool(header.normalized_a)
            for header in suggestion.headers
        ),
        sum(len(header.shared_tokens) for header in suggestion.headers),
        -len(suggestion.key_pairs),
    )


def _amount_evidence(suggestion):
    header = suggestion.header
    return header.exact_normalized_match and bool(header.normalized_a), len(header.shared_tokens)


def _identity(suggestion):
    return suggestion.key_pairs


def _amount_identity(suggestion):
    return suggestion.column_a, suggestion.column_b


@pytest.fixture
def simple_pair():
    return _pair(
        ["Left", "Second"], [["A", "x"], ["B", "y"]], ["Right", "Other"], [["A", "x"], ["B", "y"]]
    )


def test_suggestion_contracts_are_frozen_slotted_and_have_no_opaque_scores(simple_pair):
    key_result = discovery.suggest_key_mappings(*simple_pair)
    amount_pair = _pair(["Amount"], [["1"]], ["amount"], [["2"]])
    amount_result = discovery.suggest_amount_mappings(*amount_pair)
    contracts = {
        discovery.KeyMappingSuggestion: ("key_pairs", "compatibility", "headers"),
        discovery.KeySuggestionResult: (
            "suggestions",
            "leading_count",
            "evaluated_single_count",
            "evaluated_composite_count",
            "composite_search_truncated",
        ),
        discovery.AmountMappingSuggestion: ("column_a", "column_b", "compatibility", "header"),
        discovery.AmountSuggestionResult: ("suggestions", "leading_count", "evaluated_count"),
    }
    for model, names in contracts.items():
        assert tuple(field.name for field in fields(model)) == names
        assert {"score", "confidence", "probability"}.isdisjoint(names)
    for value, name in (
        (key_result, "suggestions"),
        (key_result.suggestions[0], "key_pairs"),
        (amount_result, "leading_count"),
        (amount_result.suggestions[0], "column_a"),
    ):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, None)
        assert isinstance(hash(value), int)
    assert isinstance(key_result.suggestions, tuple)
    assert all(isinstance(value.headers, tuple) for value in key_result.suggestions)


def test_observed_key_overlap_outranks_exact_header_decoys():
    pair = _pair(
        ["Invoice Number", "Locator"],
        [["X1", "K1"], ["X2", "K2"], ["X3", "K3"]],
        ["Invoice Number", "Receipt"],
        [["Y1", "K1"], ["Y2", "K2"], ["Y3", "K3"]],
    )
    result = discovery.suggest_key_mappings(*pair)
    assert result.leading_count == 1
    assert result.suggestions[0].key_pairs == (("Locator", "Receipt"),)
    assert result.suggestions[0].headers[0].shared_tokens == ()
    decoy = next(
        value
        for value in result.suggestions
        if value.key_pairs == (("Invoice Number", "Invoice Number"),)
    )
    assert decoy.headers[0].exact_normalized_match
    assert decoy.compatibility.overlap_distinct_count == 0


def test_overlap_coverage_precedes_eligible_coverage():
    pair = _pair(
        ["Sparse", "Dense"],
        [["K1", "K1"], ["", "K2"], ["", "K3"]],
        ["Some", "All"],
        [["K1", "K1"], ["", "B2"], ["", "B3"]],
    )
    result = discovery.suggest_key_mappings(*pair)
    assert result.suggestions[0].key_pairs == (("Sparse", "Some"),)
    assert result.suggestions[0].compatibility.eligible_row_count_a == 1
    dense = next(value for value in result.suggestions if value.key_pairs == (("Dense", "All"),))
    assert dense.compatibility.eligible_row_count_a == 3
    assert _key_evidence(result.suggestions[0])[1] > _key_evidence(dense)[1]


def test_complete_overlap_precedes_legitimate_partial_overlap_without_excluding_it():
    pair = _pair(
        ["Left"],
        [["K1"], ["K2"], ["K3"]],
        ["Full", "Partial"],
        [["K1", "K2"], ["K2", "K3"], ["K3", "K4"]],
    )
    result = discovery.suggest_key_mappings(*pair)
    assert result.leading_count == 1
    assert [value.key_pairs for value in result.suggestions] == [
        (("Left", "Full"),),
        (("Left", "Partial"),),
    ]
    full, partial = result.suggestions
    assert full.compatibility.overlap_distinct_count == 3
    assert partial.compatibility.overlap_distinct_count == 2
    assert (
        partial.compatibility.only_a_distinct_count
        == partial.compatibility.only_b_distinct_count
        == 1
    )


def test_eligible_coverage_precedes_uniqueness_for_legitimate_duplicate_keys():
    pair = _pair(
        ["Full", "Partial"],
        [["K1", "K1"], ["K1", ""], ["K2", ""]],
        ["All", "Some"],
        [["K1", "K1"], ["K1", ""], ["K2", ""]],
    )
    result = discovery.suggest_key_mappings(*pair)
    assert result.suggestions[0].key_pairs == (("Full", "All"),)
    assert result.suggestions[0].compatibility.duplicate_excess_count_a == 1
    partial = next(
        value for value in result.suggestions if value.key_pairs == (("Partial", "Some"),)
    )
    assert partial.compatibility.duplicate_excess_count_a == 0
    assert _key_evidence(result.suggestions[0])[2] > _key_evidence(partial)[2]


@pytest.mark.parametrize(
    "headers_a,headers_b,leader",
    [
        (
            ["Locator", "Invoice Number"],
            ["Receipt", "invoice_number"],
            (("Invoice Number", "invoice_number"),),
        ),
        (
            ["Locator", "Invoice Number"],
            ["Receipt", "Invoice Ref"],
            (("Invoice Number", "Invoice Ref"),),
        ),
    ],
)
def test_header_evidence_breaks_late_ties_when_key_data_evidence_is_identical(
    headers_a, headers_b, leader
):
    pair = _pair(headers_a, [["K1", "K1"], ["K2", "K2"]], headers_b, [["K1", "K1"], ["K2", "K2"]])
    result = discovery.suggest_key_mappings(*pair)
    assert result.suggestions[0].key_pairs == leader
    assert result.leading_count == 1


def test_duplicate_resolving_composite_is_found_from_nonadjacent_columns():
    pair = _pair(
        ["Notes", "Vendor", "Other", "Invoice"],
        [["left", "V1", "A", "100"], ["left", "V1", "A", "101"], ["left", "V2", "A", "100"]],
        ["Supplier", "Extra", "Ref", "Memo"],
        [["V1", "B", "100", "right"], ["V1", "B", "101", "right"], ["V2", "B", "100", "right"]],
    )
    result = discovery.suggest_key_mappings(*pair)
    assert result.leading_count == 1
    assert result.suggestions[0].key_pairs == (("Vendor", "Supplier"), ("Invoice", "Ref"))
    assert result.suggestions[0].compatibility.distinct_key_count_a == 3
    assert result.suggestions[0].compatibility.duplicate_excess_count_a == 0
    for column_a, column_b in result.suggestions[0].key_pairs:
        singleton = discovery.compare_key_mapping(*pair, key_pairs=[(column_a, column_b)])
        assert singleton.duplicate_excess_count_a == singleton.duplicate_excess_count_b == 1


def test_equally_effective_composite_with_equal_header_facts_follows_singletons():
    pair = _pair(
        ["First", "Second"],
        [["K1", "K1"], ["K2", "K2"]],
        ["Third", "Fourth"],
        [["K1", "K1"], ["K2", "K2"]],
    )
    result = discovery.suggest_key_mappings(*pair, max_suggestions=20)
    assert result.leading_count == 4
    assert len(result.suggestions) == 6
    assert all(len(value.key_pairs) == 1 for value in result.suggestions[:4])
    assert all(len(value.key_pairs) == 2 for value in result.suggestions[4:])
    assert _key_evidence(result.suggestions[0])[:-1] == _key_evidence(result.suggestions[-1])[:-1]


def test_punctuation_only_normalized_equality_is_not_positive_key_evidence():
    pair = _pair(
        ["$", "Locator"],
        [["K1", "K1"], ["K2", "K2"]],
        ["€", "Reference"],
        [["K1", "K1"], ["K2", "K2"]],
    )
    result = discovery.suggest_key_mappings(*pair, max_suggestions=1)
    assert result.leading_count == len(result.suggestions) == 4
    assert [value.key_pairs for value in result.suggestions] == [
        (("$", "€"),),
        (("$", "Reference"),),
        (("Locator", "€"),),
        (("Locator", "Reference"),),
    ]
    assert result.suggestions[0].headers[0].exact_normalized_match
    assert all(_key_evidence(value)[5:7] == (0, 0) for value in result.suggestions)


def test_complete_key_leading_tie_can_exceed_requested_and_safety_maximum():
    headers_a = tuple(f"Left{i}" for i in range(5))
    headers_b = tuple(f"Right{i}" for i in range(5))
    pair = _pair(headers_a, [["K1"] * 5, ["K2"] * 5], headers_b, [["K1"] * 5, ["K2"] * 5])
    result = discovery.suggest_key_mappings(*pair, max_suggestions=1)
    assert result.leading_count == len(result.suggestions) == 25
    assert result.evaluated_single_count == 25
    assert result.composite_search_truncated
    assert [value.key_pairs for value in result.suggestions] == [
        ((a, b),) for a in headers_a for b in headers_b
    ]


def test_base_pool_complete_tie_at_eight_is_not_truncated():
    pair = _pair(["Left0", "Left1"], [["K1", "K1"]], [f"Right{i}" for i in range(4)], [["K1"] * 4])
    result = discovery.suggest_key_mappings(*pair, max_suggestions=1)
    assert result.leading_count == 8
    assert result.evaluated_single_count == 8
    assert not result.composite_search_truncated


def test_base_pool_excluding_strictly_lower_groups_is_not_reported_as_truncated():
    headers_b = [f"X_Y{'!' * index}" for index in range(7)] + ["X Other", "Unrelated"]
    pair = _pair(["X Y"], [["K1"], ["K2"]], headers_b, [["K1"] * 9, ["K2"] * 9])
    result = discovery.suggest_key_mappings(*pair, max_suggestions=8)
    assert result.evaluated_single_count == 9
    assert result.evaluated_composite_count == 0
    assert result.leading_count == 7
    assert len(result.suggestions) == 8
    assert not result.composite_search_truncated


def test_composite_evaluation_limit_reports_omitted_valid_candidates(simple_pair, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_COMPOSITE_EVALUATIONS", 1)
    result = discovery.suggest_key_mappings(*simple_pair, max_suggestions=20)
    assert result.evaluated_composite_count == 1
    assert result.composite_search_truncated


def test_composite_bounds_and_canonical_pair_order_are_observable(monkeypatch):
    headers_a = [f"Left{i}" for i in range(4)]
    headers_b = [f"Right{i}" for i in range(4)]
    pair = _pair(headers_a, [["K1"] * 4, ["K2"] * 4], headers_b, [["K1"] * 4, ["K2"] * 4])
    observed = []
    original = discovery._key_compatibility_from_counts

    def record(snapshot_a, snapshot_b, key_pairs, counts_a, counts_b):
        observed.append(key_pairs)
        return original(snapshot_a, snapshot_b, key_pairs, counts_a, counts_b)

    monkeypatch.setattr(discovery, "_key_compatibility_from_counts", record)
    result = discovery.suggest_key_mappings(*pair, max_suggestions=20)
    assert discovery.MAX_COMPOSITE_BASE_PAIRS == 8
    assert discovery.MAX_COMPOSITE_EVALUATIONS == 64
    singles = [mapping for mapping in observed if len(mapping) == 1]
    composites = [mapping for mapping in observed if len(mapping) == 2]
    assert len(singles) == result.evaluated_single_count == 16
    assert len(composites) == result.evaluated_composite_count <= 28 <= 64
    assert len(set(composites)) == len(composites)
    assert len({component for mapping in composites for component in mapping}) <= 8
    for mapping in composites:
        assert len({component[0] for component in mapping}) == 2
        assert len({component[1] for component in mapping}) == 2
        assert headers_a.index(mapping[0][0]) < headers_a.index(mapping[1][0])
    assert all(len(mapping) <= 2 for mapping in observed)


def test_per_call_counter_cache_builds_once_per_side_and_ordered_index_tuple(
    simple_pair, monkeypatch
):
    calls = Counter()
    original = discovery._key_counts

    def record(snapshot, indices):
        calls[(snapshot.profile.source, indices)] += 1
        return original(snapshot, indices)

    monkeypatch.setattr(discovery, "_key_counts", record)
    first = discovery.suggest_key_mappings(*simple_pair, max_suggestions=20)
    assert calls
    assert all(count == 1 for count in calls.values())
    assert {(source, indices) for source, indices in calls if len(indices) == 1} == {
        (Source.A, (0,)),
        (Source.A, (1,)),
        (Source.B, (0,)),
        (Source.B, (1,)),
    }
    assert (Source.B, (0, 1)) in calls
    assert (Source.B, (1, 0)) in calls
    assert first == discovery.suggest_key_mappings(*simple_pair, max_suggestions=20)
    assert all(count == 2 for count in calls.values())


def test_cached_suggestion_facts_equal_direct_comparisons_and_policy_is_lexicographic(simple_pair):
    result = discovery.suggest_key_mappings(*simple_pair, max_suggestions=20)
    ranks = [_key_evidence(value) for value in result.suggestions]
    assert ranks == sorted(ranks, reverse=True)
    assert result.leading_count == sum(rank == ranks[0] for rank in ranks)
    for value in result.suggestions:
        assert value.compatibility == discovery.compare_key_mapping(
            *simple_pair, key_pairs=value.key_pairs
        )
        assert value.headers == tuple(discovery.compare_headers(*pair) for pair in value.key_pairs)
    assert result == discovery.suggest_key_mappings(*simple_pair, max_suggestions=20)


def test_key_empty_tables_and_no_eligible_rows_have_deterministic_bottom_ratios():
    empty = _pair(["Left", "Second"], [], ["Right", "Other"], [])
    result = discovery.suggest_key_mappings(*empty, max_suggestions=1)
    assert result.evaluated_single_count == 4
    assert result.leading_count == len(result.suggestions) == 4
    assert all(_key_evidence(value)[1:4] == (Fraction(-1),) * 3 for value in result.suggestions)
    blank = _pair(["Left"], [[""]], ["Right"], [[" "]])
    blank_result = discovery.suggest_key_mappings(*blank)
    assert blank_result.suggestions[0].compatibility.eligible_row_count_a == 0
    assert _key_evidence(blank_result.suggestions[0])[1] == Fraction(-1)


def test_amount_identical_meaningful_headers_outrank_other_runnable_pairs():
    pair = _pair(
        ["Amount", "Invoice Gross Charge"],
        [["1", "100"], ["2", "200"]],
        ["amount", "Invoice Gross Credit"],
        [["4", "100"], ["5", "200"]],
    )
    result = discovery.suggest_amount_mappings(*pair, max_suggestions=20)
    assert result.leading_count == 1
    assert _amount_identity(result.suggestions[0]) == ("Amount", "amount")
    decoy = next(
        value
        for value in result.suggestions
        if _amount_identity(value) == ("Invoice Gross Charge", "Invoice Gross Credit")
    )
    assert len(decoy.header.shared_tokens) == 2
    assert not decoy.header.exact_normalized_match
    assert result.suggestions[0].compatibility.control_difference != 0
    assert decoy.compatibility.control_difference == 0


def test_canonical_amount_pair_remains_visible_among_generic_header_ties():
    case = next(case for case in discovery_calibration.build_corpus() if case.name == "canonical")
    pair = (
        discovery.discover_csv(case.data_a, source=Source.A),
        discovery.discover_csv(case.data_b, source=Source.B),
    )
    result = discovery.suggest_amount_mappings(*pair, max_suggestions=1)
    leaders = result.suggestions[: result.leading_count]
    assert ("Invoice Amount", "Gross Amount") in {_amount_identity(value) for value in leaders}
    assert all(_amount_evidence(value) == _amount_evidence(leaders[0]) for value in leaders)
    assert result.evaluated_count == 25
    intended = next(
        value for value in leaders if _amount_identity(value) == ("Invoice Amount", "Gross Amount")
    )
    assert intended.compatibility.total_a == Decimal("2675")
    assert intended.compatibility.total_b == Decimal("2580")


@pytest.mark.parametrize("numeric_b", ["1002", "1002.00001", "-9999999999.123456789"])
def test_numeric_identifier_and_control_total_proximity_do_not_break_amount_ties(numeric_b):
    pair = _pair(
        ["Identifier", "Charge"],
        [["1001", "50"], ["1002", "60"]],
        ["Reference", "Posted"],
        [["1001", "40"], [numeric_b, "60"]],
    )
    result = discovery.suggest_amount_mappings(*pair, max_suggestions=1)
    assert result.leading_count == len(result.suggestions) == 4
    assert [_amount_identity(value) for value in result.suggestions] == [
        ("Identifier", "Reference"),
        ("Identifier", "Posted"),
        ("Charge", "Reference"),
        ("Charge", "Posted"),
    ]
    assert all(_amount_evidence(value) == (False, 0) for value in result.suggestions)
    assert result.suggestions[-1].compatibility.control_difference == Decimal("10")


def test_partial_invalid_and_blank_amount_columns_are_evaluated_but_never_emitted():
    pair = _pair(
        ["Good", "Partial", "Blank", "Text"],
        [["1", "1", "", "x"], ["2", "word", " \t", "y"]],
        ["Valid", "Mixed"],
        [["3", "1"], ["4", "False"]],
    )
    result = discovery.suggest_amount_mappings(*pair)
    assert result.evaluated_count == 8
    assert result.leading_count == 1
    assert [_amount_identity(value) for value in result.suggestions] == [("Good", "Valid")]


@pytest.mark.parametrize(
    "rows_a,rows_b", [([], []), ([], [["1", "2"]]), ([["word", ""]], [["3", "4"]])]
)
def test_empty_or_unrunnable_amount_tables_have_no_suggestions(rows_a, rows_b):
    pair = _pair(["Left", "Second"], rows_a, ["Right", "Other"], rows_b)
    result = discovery.suggest_amount_mappings(*pair)
    assert result.suggestions == ()
    assert result.leading_count == 0
    assert result.evaluated_count == 4


def test_punctuation_empty_equality_and_no_header_evidence_preserve_amount_ambiguity():
    pair = _pair(
        ["$", "Locator"], [["1", "10"], ["2", "20"]], ["€", "Reference"], [["1", "11"], ["2", "21"]]
    )
    result = discovery.suggest_amount_mappings(*pair, max_suggestions=1)
    assert result.leading_count == len(result.suggestions) == 4
    assert result.suggestions[0].header.exact_normalized_match
    assert result.suggestions[0].header.normalized_a == ""
    assert [_amount_identity(value) for value in result.suggestions] == [
        ("$", "€"),
        ("$", "Reference"),
        ("Locator", "€"),
        ("Locator", "Reference"),
    ]
    assert all(_amount_evidence(value) == (False, 0) for value in result.suggestions)


def test_amount_ordinary_lower_ranked_suggestions_respect_maximum():
    pair = _pair(
        ["Amount", "Identifier"],
        [["1", "1001"], ["2", "1002"]],
        ["amount", "Reference"],
        [["3", "1001"], ["4", "1002"]],
    )
    result = discovery.suggest_amount_mappings(*pair, max_suggestions=2)
    assert len(result.suggestions) == 2
    assert result.leading_count == 1
    assert _amount_identity(result.suggestions[0]) == ("Amount", "amount")
    assert result == discovery.suggest_amount_mappings(*pair, max_suggestions=2)
    assert [_amount_evidence(value) for value in result.suggestions] == sorted(
        [_amount_evidence(value) for value in result.suggestions], reverse=True
    )


class _IntSubclass(int):
    pass


@pytest.mark.parametrize("maximum", [True, False, 1.0, "8", None, _IntSubclass(2)])
def test_suggestion_maximum_requires_exact_int(simple_pair, maximum):
    for suggest in (discovery.suggest_key_mappings, discovery.suggest_amount_mappings):
        with pytest.raises(TypeError):
            suggest(*simple_pair, max_suggestions=maximum)


@pytest.mark.parametrize("maximum", [-1, 0, 21, 100])
def test_suggestion_maximum_enforces_one_to_twenty(simple_pair, maximum):
    for suggest in (discovery.suggest_key_mappings, discovery.suggest_amount_mappings):
        with pytest.raises(ValueError):
            suggest(*simple_pair, max_suggestions=maximum)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("wrong", [None, {}, "snapshot", 1])
def test_suggestions_reject_wrong_snapshot_types(simple_pair, side, wrong):
    pair = list(simple_pair)
    pair[side] = wrong
    for suggest in (discovery.suggest_key_mappings, discovery.suggest_amount_mappings):
        with pytest.raises(TypeError):
            suggest(*pair)


@pytest.mark.parametrize(
    "directions", [(Source.B, Source.A), (Source.A, Source.A), (Source.B, Source.B)]
)
def test_suggestions_reject_reversed_or_same_source_direction(directions):
    pair = (
        _snapshot(["Left"], [["1"]], source=directions[0]),
        _snapshot(["Right"], [["1"]], source=directions[1]),
    )
    for suggest in (discovery.suggest_key_mappings, discovery.suggest_amount_mappings):
        with pytest.raises(ValueError):
            suggest(*pair)


def test_suggestions_are_context_independent_and_do_not_reparse_sources(monkeypatch):
    pair = _pair(
        ["Identifier", "Amount"],
        [["K1", "12345678901234567890.123456789"], ["K2", "0.000000001"]],
        ["Reference", "amount"],
        [["K1", "12345678901234567890.123456789"], ["K2", "0"]],
    )
    expected_keys = discovery.suggest_key_mappings(*pair)
    expected_amounts = discovery.suggest_amount_mappings(*pair)

    def fail(*args, **kwargs):
        raise AssertionError("suggestions must consume immutable snapshots")

    for name in ("discover_csv", "discover_xlsx", "_open", "_key", "_amount", "parse_amount"):
        monkeypatch.setattr(discovery, name, fail)
    with localcontext() as context:
        context.prec = 2
        context.Emax = 3
        context.Emin = -3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        context.clear_flags()
        assert expected_keys == discovery.suggest_key_mappings(*pair)
        assert expected_amounts == discovery.suggest_amount_mappings(*pair)
        assert not any(context.flags.values())


def test_suggestions_do_not_mutate_snapshots_ingestion_reconciliation_or_export_bytes(xlsx_bytes):
    text = "id,amount,notes\nA,100,memo\nDUP,1,\nDUP,2,other\n"
    data = xlsx_bytes({"Data": [["id", "amount", "notes"], ["A", 100.01, "=1+1"], ["DUP", 3]]})
    before_a = ingest_csv(text, source=Source.A, key_columns=["id"], amount_column="amount")
    before_b = ingest_xlsx(
        data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
    )
    result = reconcile(before_a, before_b, amount_tolerance=Decimal("0.01"))
    exported = export_exceptions_csv(result)
    pair = (
        discovery.discover_csv(text, source=Source.A),
        discovery.discover_xlsx(data, source=Source.B, worksheet="Data"),
    )
    original = tuple((snapshot.profile, snapshot._key_rows, hash(snapshot)) for snapshot in pair)
    discovery.suggest_key_mappings(*pair)
    discovery.suggest_amount_mappings(*pair)
    assert original == tuple(
        (snapshot.profile, snapshot._key_rows, hash(snapshot)) for snapshot in pair
    )
    after_a = ingest_csv(text, source=Source.A, key_columns=["id"], amount_column="amount")
    after_b = ingest_xlsx(
        data, source=Source.B, worksheet="Data", key_columns=["id"], amount_column="amount"
    )
    after = reconcile(after_a, after_b, amount_tolerance=Decimal("0.01"))
    assert before_a == after_a
    assert before_b == after_b
    assert result == after
    assert exported == export_exceptions_csv(after)


def test_production_suggestion_policy_has_no_float_conversion_vocabulary_or_engine_dependencies():
    source = ast.parse(inspect.getsource(discovery))
    forbidden_imports = ("streamlit", "tallydiff.app", "tallydiff.engine", "tallydiff.review")
    finance_words = {
        "vendor",
        "supplier",
        "amount",
        "gross",
        "debit",
        "credit",
        "invoice",
        "tax",
        "payment",
        "charge",
    }
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            names = []
        assert not any(
            name == forbidden or name.startswith(forbidden + ".")
            for name in names
            for forbidden in forbidden_imports
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "float"
        if isinstance(node, ast.Dict):
            keys = {
                key.value.casefold()
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            assert finance_words.isdisjoint(keys)
    names = {
        "KeyMappingSuggestion",
        "KeySuggestionResult",
        "AmountMappingSuggestion",
        "AmountSuggestionResult",
        "suggest_key_mappings",
        "suggest_amount_mappings",
    }
    assert names.isdisjoint(tallydiff.__all__)
    assert all(not hasattr(tallydiff, name) for name in names)


def test_tiny_benchmark_retains_old_phases_and_reports_production_suggestions():
    measured = discovery_benchmark.benchmark_case(8, column_count=10, composite_limit=5)
    assert set(measured["elapsed_seconds"]) == {
        "discover_csv_pair",
        "single_key_pairs",
        "amount_pairs",
        "composite_key_pairs",
    }
    data_a, data_b = discovery_benchmark.generate_csv_pair(8, column_count=10)
    pair = (
        discovery.discover_csv(data_a, source=Source.A),
        discovery.discover_csv(data_b, source=Source.B),
    )
    keys = discovery.suggest_key_mappings(*pair)
    amounts = discovery.suggest_amount_mappings(*pair)
    production = measured["production_suggestions"]
    assert set(production["elapsed_seconds"]) == {"suggest_key_mappings", "suggest_amount_mappings"}
    assert all(value >= 0 for value in production["elapsed_seconds"].values())
    assert production["evaluated_single_count"] == keys.evaluated_single_count == 100
    assert production["evaluated_amount_count"] == amounts.evaluated_count == 100
    assert production["evaluated_composite_count"] == keys.evaluated_composite_count <= 64
    assert production["composite_search_truncated"] is keys.composite_search_truncated
    assert production["key_leading_count"] == keys.leading_count
    assert production["amount_leading_count"] == amounts.leading_count
    assert production["returned_key_count"] == len(keys.suggestions)
    assert production["returned_amount_count"] == len(amounts.suggestions)


def test_canonical_key_case_leads_with_invoice_and_retains_vendor_invoice_composite():
    case = next(case for case in discovery_calibration.build_corpus() if case.name == "canonical")
    pair = (
        discovery.discover_csv(case.data_a, source=Source.A),
        discovery.discover_csv(case.data_b, source=Source.B),
    )
    result = discovery.suggest_key_mappings(*pair, max_suggestions=20)
    assert result.leading_count == 1
    assert result.suggestions[0].key_pairs == (("Invoice Number", "Invoice Ref"),)
    composite = next(
        value
        for value in result.suggestions
        if value.key_pairs == (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref"))
    )
    assert composite.compatibility.overlap_distinct_count == 3
    assert composite.compatibility.duplicate_excess_count_a == 0
    assert _key_evidence(composite)[:-1] == _key_evidence(result.suggestions[0])[:-1]


def test_physical_row_permutations_preserve_suggestion_results(simple_pair):
    reversed_pair = tuple(
        _snapshot(
            tuple(column.name for column in snapshot.profile.columns),
            tuple(reversed(snapshot._key_rows)),
            source=snapshot.profile.source,
        )
        for snapshot in simple_pair
    )
    assert discovery.suggest_key_mappings(*simple_pair) == discovery.suggest_key_mappings(
        *reversed_pair
    )
    numeric = _pair(
        ["Amount", "Identifier"],
        [["1", "1001"], ["2", "1002"]],
        ["amount", "Reference"],
        [["3", "1001"], ["4", "1002"]],
    )
    numeric_reversed = tuple(
        _snapshot(
            tuple(column.name for column in snapshot.profile.columns),
            tuple(reversed(snapshot._key_rows)),
            source=snapshot.profile.source,
        )
        for snapshot in numeric
    )
    assert discovery.suggest_amount_mappings(*numeric) == discovery.suggest_amount_mappings(
        *numeric_reversed
    )


def test_fixed_sixty_four_composite_cap_reports_truncation_with_expanded_test_base(monkeypatch):
    monkeypatch.setattr(discovery, "MAX_COMPOSITE_BASE_PAIRS", 16)
    pair = _pair(
        [f"Left{i}" for i in range(4)],
        [["K1"] * 4, ["K2"] * 4],
        [f"Right{i}" for i in range(4)],
        [["K1"] * 4, ["K2"] * 4],
    )
    result = discovery.suggest_key_mappings(*pair)
    # Four A columns and four B columns permit C(4, 2) * P(4, 2) = 72 mappings.
    assert discovery.MAX_COMPOSITE_EVALUATIONS == 64
    assert result.evaluated_single_count == 16
    assert result.evaluated_composite_count == 64
    assert result.composite_search_truncated


def test_xlsx_boolean_formula_and_error_key_candidates_rank_below_actual_overlap(xlsx_bytes):
    snapshot_a = _snapshot(
        ["Identifier", "Invoice Number"], [["K1", "1"], ["K2", "2"], ["K3", "3"]], source=Source.A
    )
    data = xlsx_bytes(
        {"Data": [["Reference", "Invoice Number"], ["K1", True], ["K2", "=1+1"], ["K3", "#N/A"]]}
    )
    snapshot_b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
    result = discovery.suggest_key_mappings(snapshot_a, snapshot_b, max_suggestions=20)
    assert result.leading_count == 1
    assert result.suggestions[0].key_pairs == (("Identifier", "Reference"),)
    invalid = next(
        value
        for value in result.suggestions
        if value.key_pairs == (("Invoice Number", "Invoice Number"),)
    )
    assert invalid.headers[0].exact_normalized_match
    assert invalid.compatibility.eligible_row_count_b == 0
    assert invalid.compatibility.overlap_distinct_count == 0
    assert discovery.suggest_amount_mappings(snapshot_a, snapshot_b).suggestions == ()
