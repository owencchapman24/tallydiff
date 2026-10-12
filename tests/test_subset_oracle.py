"""Production-free proof of signed DP, witness rank, construction and frozen truth."""

import ast
import hashlib
import inspect
import json
import sys
from decimal import Decimal, localcontext
from random import Random

import pytest

import scripts.subset_oracle as oracle


def hand(anchor, candidates, *, reverse=False, remaining=1_000_000, exhausted=False):
    scenario = oracle.SubsetScenario(
        ("V", "1"),
        "hand",
        tuple(candidates) if reverse else (anchor,),
        (anchor,) if reverse else tuple(candidates),
    )
    pair = oracle.pair_from_scenarios((scenario,))
    return oracle.correspondence(pair.records_a, pair.records_b, remaining, exhausted)


@pytest.mark.parametrize(
    "anchor,candidates,status,witnesses,examined",
    (
        (300, (100, 200), "unique_exact", (), 3),
        (300, (100, 200, 50), "unique_exact", (), 7),
        (300, (100, 200, 150, 150), "ambiguous", ((0, 1), (2, 3)), 10),
        (300, (100, 100, 200), "ambiguous", ((0, 2), (1, 2)), 6),
        (200, (100, 100), "unique_exact", (), 3),
        (300, (300, 100, 200), "ambiguous", ((0,), (1, 2)), 6),
        (300, (300, 50), "singleton_only", ((0,),), 3),
        (300, (100, 250), "no_exact_subset", (), 3),
        (-300, (-100, -200), "unique_exact", (), 3),
        (0, (150, -150), "unique_exact", (), 3),
        (0, (0, 0), "ambiguous", ((0,), (1,)), 2),
        (100, (150, -50, 0), "ambiguous", ((0, 1), (0, 1, 2)), 7),
        (4095, tuple(1 << i for i in range(12)), "unique_exact", (), 4095),
        (8191, tuple(1 << i for i in range(13)), "bound_exceeded", (), 0),
    ),
)
@pytest.mark.parametrize("reverse", (False, True))
def test_hand_audited_signed_physical_witnesses(
    anchor, candidates, status, witnesses, examined, reverse
):
    analysis, exhausted = hand(anchor, candidates, reverse=reverse)
    assert analysis.status == status
    assert analysis.examined_combinations == examined
    assert exhausted is False
    actual = tuple(
        tuple(row.source_row - 2 for row in (solution.rows_a if reverse else solution.rows_b))
        for solution in analysis.ambiguity_witnesses
    )
    assert actual == witnesses
    assert analysis.search_complete == (examined == (1 << len(candidates)) - 1)
    if analysis.accepted_solution:
        selected = (
            analysis.accepted_solution.rows_a if reverse else analysis.accepted_solution.rows_b
        )
        assert len(selected) >= 2
        assert sum(row.units for row in selected) == anchor
        assert analysis.accepted_solution.units == anchor
    if len(candidates) == 13:
        assert analysis.reason == "candidate_row_limit"
        assert analysis.planned_combinations == analysis.reserved_combinations == 0


def test_dp_and_comb_rank_against_small_exhaustive_integer_masks():
    rng = Random(88)
    for count in range(1, 9):
        masks = sorted(range(1, 1 << count), key=lambda mask: oracle.mask_order(mask, count))
        assert [oracle.witness_rank(mask, count) for mask in masks] == list(range(1, 1 << count))
        for _ in range(30):
            candidates = tuple(rng.randint(-3, 3) for _ in range(count))
            by_sum = {}
            for mask in masks:
                subtotal = sum(
                    units for index, units in enumerate(candidates) if mask & (1 << index)
                )
                by_sum.setdefault(subtotal, []).append(mask)
            for target in range(-12, 13):
                assert oracle.dp_witness_masks(target, candidates) == tuple(
                    by_sum.get(target, ())[:2]
                )


@pytest.mark.parametrize("reverse", (False, True))
def test_exact_remaining_boundary_first_failure_and_sticky_oracle(reverse):
    accepted, exhausted = hand(300, (100, 200), remaining=3, reverse=reverse)
    assert accepted.reserved_combinations == 3 and not exhausted
    failed, exhausted = hand(300, (100, 200), remaining=2, reverse=reverse)
    assert failed.reason == "run_budget_exhausted" and exhausted
    sticky, exhausted = hand(300, (100, 200), remaining=100, exhausted=exhausted, reverse=reverse)
    assert sticky.reason == "run_budget_exhausted" and exhausted
    assert sticky.examined_combinations == sticky.reserved_combinations == 0
    assert sticky.planned_combinations == 3


@pytest.mark.parametrize("early", (False, True))
def test_full_budget_plan_and_post_failure_precedence(early):
    truth = oracle.calibration_pair(early_ambiguity=early).expected()
    summary = truth.summary()
    assert summary["reserved"] == 1_000_000
    assert summary["examined"] == (696 if early else 1_000_000)
    assert summary["planned"] == 1_000_006
    assert summary["analyzed"] == 353
    assert len(truth.groups) == 354
    assert [group.analysis.reason for group in truth.groups[-6:-1]] == [
        "run_budget_exhausted",
        "run_budget_exhausted",
        "candidate_row_limit",
        "both_sides_multiple",
        "missing_opposite_side",
    ]
    assert truth.groups[-1].analysis is None


def test_subset_oracle_stdlib_only_no_product_normalizers_or_float(monkeypatch):
    tree = ast.parse(inspect.getsource(oracle))
    forbidden = {
        "tallydiff",
        "subset_matching",
        "engine",
        "ReconciliationResult",
        "CorrespondenceAnalysis",
        "SubsetSolution",
        "analyze_bounded_one_to_many",
        "reconcile",
        "export_exceptions_csv",
        "sum_decimals",
        "normalize_key",
        "float",
        "from_float",
        "combinations",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module.split(".")[0] in sys.stdlib_module_names
            assert all(alias.name not in forbidden for alias in node.names)
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden
        elif isinstance(node, ast.Constant):
            assert not isinstance(node.value, float)
        elif isinstance(node, ast.BinOp):
            assert not isinstance(node.op, ast.Div)
    original_import = __import__

    def guard(name, *args, **kwargs):
        assert not name.startswith("tallydiff")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guard)
    with localcontext() as context:
        context.prec = 2
        pair = oracle.generate_subset_pair(257, normalized=True, shuffle_seed=77)
        for mode in oracle.MODES:
            truth = pair.expected(Decimal("0.01"), mode=mode)
            assert truth.summary()["key_groups"] == 257
            assert oracle.expected_subset_export(truth)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


FROZEN = [
    {
        "parameters": {"groups": 100, "seed": 42},
        "csv_a": "88a4a224a47a3183ea2bd19ac45944b01795a86038ca9a2bba1382fcde01b7ad",
        "csv_b": "0ce2190bdd05079cd4cdbcf5221d018aa45d644f78c6367302df9e4abc0d5a1a",
        "summary": {
            "mode": "bounded_one_to_many",
            "normalized": False,
            "key_groups": 100,
            "records_a": 115,
            "records_b": 189,
            "categories": {
                "exact_match": 47,
                "within_tolerance": 7,
                "amount_mismatch": 9,
                "a_only": 3,
                "b_only": 3,
                "duplicate_ambiguous": 31,
            },
            "statuses": {
                "unique_exact": 37,
                "ambiguous": 15,
                "no_exact_subset": 3,
                "singleton_only": 3,
                "bound_exceeded": 1,
                "not_eligible": 9,
            },
            "analyzed": 68,
            "inferred": 37,
            "exceptions": 92,
            "tolerated": 4,
            "total_a": "150000000000000000000000000000143934.0009",
            "total_b": "150000000000000000000000000000146333.9659",
            "control_difference": "-2399.9650",
            "tolerated_delta_total": "0.0200",
            "planned": 4374,
            "reserved": 4374,
            "examined": 4350,
        },
        "groups_sha": "dde32092d138b6f3a2d96807cd52760f06bce8aa9b934581cb1dd01317858bbd",
    },
    {
        "parameters": {"groups": 257, "seed": 2026, "shuffle_seed": 77},
        "csv_a": "e22c0db1f1cd2247fb2e33cb414842cdff6d94f2f069947a5bae93c047d2fabf",
        "csv_b": "1df6c904fbdc4eaac43ec300b29f9a463714990a9c4e0a9e5e13a0a80b578dfc",
        "summary": {
            "mode": "bounded_one_to_many",
            "normalized": False,
            "key_groups": 257,
            "records_a": 297,
            "records_b": 457,
            "categories": {
                "exact_match": 122,
                "within_tolerance": 16,
                "amount_mismatch": 23,
                "a_only": 7,
                "b_only": 8,
                "duplicate_ambiguous": 81,
            },
            "statuses": {
                "unique_exact": 97,
                "ambiguous": 40,
                "no_exact_subset": 8,
                "singleton_only": 8,
                "bound_exceeded": 1,
                "not_eligible": 24,
            },
            "analyzed": 178,
            "inferred": 97,
            "exceptions": 240,
            "tolerated": 8,
            "total_a": "410000000000000000000000000000388584.0024",
            "total_b": "410000000000000000000000000000404083.9224",
            "control_difference": "-15499.9200",
            "tolerated_delta_total": "0.0400",
            "planned": 4839,
            "reserved": 4839,
            "examined": 4757,
        },
        "groups_sha": "e8a2a4435a9977b65f3a65e1c5151d0c0e9a191300f130471466b6c0de1dc0e3",
    },
    {
        "parameters": {"groups": 1000, "seed": 42},
        "csv_a": "89a0ebf8cf1250c7d33a730e9aaab113ea72dbed3a0869c618ce893c53b1c9b8",
        "csv_b": "714f704a8283c28012c41a173520d6afb71d82bc481a880deeef45eef265466e",
        "summary": {
            "mode": "bounded_one_to_many",
            "normalized": False,
            "key_groups": 1000,
            "records_a": 1156,
            "records_b": 1776,
            "categories": {
                "exact_match": 469,
                "within_tolerance": 63,
                "amount_mismatch": 94,
                "a_only": 30,
                "b_only": 30,
                "duplicate_ambiguous": 314,
            },
            "statuses": {
                "unique_exact": 376,
                "ambiguous": 155,
                "no_exact_subset": 31,
                "singleton_only": 31,
                "bound_exceeded": 4,
                "not_eligible": 93,
            },
            "analyzed": 690,
            "inferred": 376,
            "exceptions": 936,
            "tolerated": 32,
            "total_a": "1710000000000000000000000000001244043.0093",
            "total_b": "1710000000000000000000000000001332592.6943",
            "control_difference": "-88549.6850",
            "tolerated_delta_total": "0.1600",
            "planned": 19267,
            "reserved": 19267,
            "examined": 19019,
        },
        "groups_sha": "a18b11fe0af2c1986f414dab8d6299e18e82d2f1a38dc7fb19caff8b18d9c5a7",
    },
    {
        "parameters": {"groups": 257, "seed": 42, "shuffle_seed": 31, "normalized": True},
        "csv_a": "72ef12f11847c5cbe65fddb0f97e85e91c200fcdb4362a176763d20627d1b35b",
        "csv_b": "6c335b869004275784d9c400c124dc8f6561e81b86dcc47e20d99ddfe3ee103a",
        "summary": {
            "mode": "bounded_one_to_many",
            "normalized": True,
            "key_groups": 257,
            "records_a": 297,
            "records_b": 457,
            "categories": {
                "exact_match": 122,
                "within_tolerance": 16,
                "amount_mismatch": 23,
                "a_only": 7,
                "b_only": 8,
                "duplicate_ambiguous": 81,
            },
            "statuses": {
                "unique_exact": 97,
                "ambiguous": 40,
                "no_exact_subset": 8,
                "singleton_only": 8,
                "bound_exceeded": 1,
                "not_eligible": 24,
            },
            "analyzed": 178,
            "inferred": 97,
            "exceptions": 240,
            "tolerated": 8,
            "total_a": "370000000000000000000000000000306034.0024",
            "total_b": "370000000000000000000000000000325833.9224",
            "control_difference": "-19799.9200",
            "tolerated_delta_total": "0.0400",
            "planned": 4839,
            "reserved": 4839,
            "examined": 4756,
        },
        "groups_sha": "9fe65c6e50a633945b50c847ef0e64e879f537c5ce59a7a8d1933c42bb9785e7",
    },
]


@pytest.mark.parametrize("recipe", FROZEN)
def test_frozen_subset_bytes_summary_and_complete_ordered_truth(recipe):
    pair = oracle.generate_subset_pair(**recipe["parameters"])
    truth = pair.expected(Decimal("0.01"))
    assert hashlib.sha256(pair.csv_a.encode()).hexdigest() == recipe["csv_a"]
    assert hashlib.sha256(pair.csv_b.encode()).hexdigest() == recipe["csv_b"]
    assert truth.summary() == recipe["summary"]
    assert digest(oracle.ordered_truth(truth)) == recipe["groups_sha"]
    assert pair == oracle.generate_subset_pair(**recipe["parameters"])


def test_population_secondary_evidence_normalization_and_shuffled_ordinals():
    pair = oracle.generate_subset_pair(1000, normalized=True, shuffle_seed=91)
    truth = pair.expected(Decimal("0.01"))
    assert set(scenario.kind for scenario in pair.scenarios) == {
        kind for kind, _, _ in oracle.RECIPES
    } | {"candidate_12", "candidate_13"}
    assert {group.analysis.status for group in truth.groups if group.analysis} == set(
        oracle.STATUSES
    )
    assert any(
        group.inferred and group.records_a[0].key != group.records_b[0].key
        for group in truth.groups
    )
    for records in (pair.records_a, pair.records_b):
        assert [row.source_row for row in records] == list(range(2, len(records) + 2))
    for group in truth.groups:
        assert (
            all(field.status == "not_comparable" for field in group.field_comparisons)
            if group.analysis
            else True
        )
        for field in group.field_comparisons:
            assert field.values_a == tuple(
                sorted({row.raw_fields[field.mapping[0]] for row in group.records_a})
            )
            assert field.values_b == tuple(
                sorted({row.raw_fields[field.mapping[1]] for row in group.records_b})
            )


@pytest.mark.parametrize("mode", oracle.MODES)
def test_independent_review_partition_and_integer_identities(mode):
    pair = oracle.generate_subset_pair(1000)
    truth = pair.expected(Decimal("0.01"), mode=mode)
    summary = truth.summary()
    assert sum(group.delta_units for group in truth.groups) == sum(
        row.units for row in pair.records_a
    ) - sum(row.units for row in pair.records_b)
    tolerated = sum(
        group.delta_units for group in truth.groups if group.key in truth.tolerated_keys
    )
    assert summary["tolerated_delta_total"] == oracle.money(tolerated)
    assert sum(
        group.delta_units for group in truth.groups if group.is_exception
    ) + tolerated == sum(group.delta_units for group in truth.groups)
    assert all(group.is_exception for group in truth.groups if group.inferred)


@pytest.mark.parametrize(
    "tolerance,error",
    (
        (1, TypeError),
        (Decimal("NaN"), ValueError),
        (Decimal("Infinity"), ValueError),
        (Decimal("-1"), ValueError),
    ),
)
def test_invalid_tolerance(tolerance, error):
    with pytest.raises(error):
        oracle.generate_subset_pair(1).expected(tolerance)


def test_invalid_mode():
    with pytest.raises(ValueError):
        oracle.generate_subset_pair(1).expected(mode="unknown")


@pytest.mark.parametrize(
    "delta,category",
    (
        (0, "exact_match"),
        (99, "within_tolerance"),
        (100, "within_tolerance"),
        (-100, "within_tolerance"),
        (101, "amount_mismatch"),
        (-101, "amount_mismatch"),
    ),
)
def test_ordinary_tolerance_is_inclusive_and_preserves_signed_delta(delta, category):
    pair = oracle.pair_from_scenarios(
        (oracle.SubsetScenario(("V", "1"), "boundary", (10000,), (10000 - delta,)),)
    )
    truth = pair.expected(Decimal("0.01"))
    assert truth.groups[0].category == category
    assert truth.groups[0].delta_units == delta
    assert truth.groups[0].analysis is None


def test_any_tiny_residual_remains_review_mismatch_within_tolerance():
    pair = oracle.pair_from_scenarios(
        (oracle.SubsetScenario(("V", "1"), "tiny_residual", (3000000,), (1000000, 2000000, 1)),)
    )
    group = pair.expected(Decimal("1")).groups[0]
    assert group.category == "amount_mismatch" and group.inferred and group.is_exception
    assert group.analysis.unassigned_rows_b[0].units == 1


def test_conditional_correspondence_export_columns_for_ordinary_and_analyzed_truth():
    ordinary = oracle.generate_subset_pair(1).expected()
    assert b"Correspondence evidence" not in oracle.expected_subset_export(ordinary)
    analyzed = oracle.generate_subset_pair(6).expected()
    assert b"Correspondence evidence" in oracle.expected_subset_export(analyzed)
    disabled = oracle.generate_subset_pair(100).expected(mode="unique")
    assert b"Correspondence evidence" not in oracle.expected_subset_export(disabled)


def test_shuffle_preserves_financial_and_secondary_truth_with_reassigned_physical_ordinals():
    first = oracle.generate_subset_pair(257, seed=88, shuffle_seed=1)
    second = oracle.generate_subset_pair(257, seed=88, shuffle_seed=2)
    assert first.scenarios == second.scenarios
    assert first.csv_a != second.csv_a and first.csv_b != second.csv_b

    def semantics(pair):
        return tuple(
            (
                group.key,
                group.category,
                group.units_a,
                group.units_b,
                group.field_comparisons,
                group.analysis.status if group.analysis else None,
            )
            for group in pair.expected(Decimal("0.01")).groups
        )

    assert semantics(first) == semantics(second)
