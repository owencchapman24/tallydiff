"""Deterministic physical subset search without enabling engine inference."""

import json
from dataclasses import fields, replace
from decimal import ROUND_DOWN, Decimal, localcontext
from itertools import combinations

import pytest

import tallydiff
import tallydiff.engine as engine
import tallydiff.subset_matching as subset_matching
from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ComparisonFieldMapping,
    CorrespondenceAnalysis,
    CorrespondenceReason,
    CorrespondenceStatus,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    Source,
    SourceRecord,
    SubsetSolution,
    reconcile,
)
from tallydiff.subset_matching import analyze_bounded_one_to_many

POLICY = EXACT_UNIQUE_ONE_TO_MANY_POLICY
S = CorrespondenceStatus
R = CorrespondenceReason


def _record(source, ordinal, amount, key="INV"):
    return SourceRecord(source, ordinal, (key,), Decimal(amount), {"evidence": str(ordinal)})


def _group(amount="300", amounts=("100", "200"), source=Source.A, ordinals=None):
    anchor = (_record(source, 2, amount),)
    opposite = Source.B if source is Source.A else Source.A
    if ordinals is None:
        ordinals = range(3, len(amounts) + 3)
    candidates = tuple(
        _record(opposite, ordinal, value) for ordinal, value in zip(ordinals, amounts, strict=True)
    )
    return (anchor, candidates) if source is Source.A else (candidates, anchor)


def _analyze(rows, **kwargs):
    return analyze_bounded_one_to_many(
        *rows, remaining_planned_budget=POLICY.max_planned_combinations_per_run, **kwargs
    )


def _fail_search(*args, **kwargs):
    pytest.fail("subset search must not be reached")


def _assert_unresolved(analysis, rows, status, reason=None, *, counters=(0, 0, 0), complete=False):
    assert analysis.status is status
    assert analysis.reason is reason
    assert analysis.policy is POLICY
    assert analysis.accepted_solution is None
    assert analysis.unassigned_rows_a is rows[0]
    assert analysis.unassigned_rows_b is rows[1]
    assert (
        analysis.planned_combinations,
        analysis.examined_combinations,
        analysis.reserved_combinations,
    ) == counters
    assert analysis.search_complete is complete
    if status not in (S.AMBIGUOUS, S.SINGLETON_ONLY):
        assert analysis.ambiguity_witnesses == ()


def _candidate_rows(solution, anchor_source):
    # Singleton witnesses cannot establish direction; use the parent shape.
    return solution.rows_b if anchor_source is Source.A else solution.rows_a


def _assert_original_references(analysis, rows):
    assert isinstance(analysis, CorrespondenceAnalysis)
    solutions = (
        (analysis.accepted_solution,) if analysis.accepted_solution is not None else ()
    ) + analysis.ambiguity_witnesses
    assert all(isinstance(solution, SubsetSolution) for solution in solutions)
    for side, parents in (("a", rows[0]), ("b", rows[1])):
        retained = getattr(analysis, f"unassigned_rows_{side}") + tuple(
            row for solution in solutions for row in getattr(solution, f"rows_{side}")
        )
        assert all(any(row is parent for parent in parents) for row in retained)


def _model_bytes(value):
    """Serialize every stored model field, including original record evidence."""

    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, SourceRecord):
            return {
                "source": item.source.value,
                "source_row": item.source_row,
                "key": item.key,
                "amount": str(item.amount),
                "raw_fields": dict(item.raw_fields),
            }
        if isinstance(item, tuple):
            return [encode(member) for member in item]
        if hasattr(item, "__dataclass_fields__"):
            return {field.name: encode(getattr(item, field.name)) for field in fields(item)}
        return item

    return json.dumps(encode(value), ensure_ascii=False, sort_keys=True).encode("utf-8")


def test_both_empty_is_a_programmer_error():
    with pytest.raises(ValueError, match="at least one"):
        _analyze(((), ()))


@pytest.mark.parametrize("counts", [(1, 1), (1, 0), (0, 1)])
@pytest.mark.parametrize("exhausted", [False, True])
def test_ordinary_groups_need_no_analysis_even_without_budget(monkeypatch, counts, exhausted):
    monkeypatch.setattr(subset_matching, "combinations", _fail_search)
    rows = tuple(
        tuple(_record(source, 2, "100") for _ in range(count))
        for source, count in zip((Source.A, Source.B), counts, strict=True)
    )
    assert (
        analyze_bounded_one_to_many(
            *rows, remaining_planned_budget=0, run_budget_exhausted=exhausted
        )
        is None
    )


@pytest.mark.parametrize(
    "counts,reason",
    [
        ((2, 0), R.MISSING_OPPOSITE_SIDE),
        ((0, 2), R.MISSING_OPPOSITE_SIDE),
        ((13, 0), R.MISSING_OPPOSITE_SIDE),
        ((0, 13), R.MISSING_OPPOSITE_SIDE),
        ((2, 2), R.BOTH_SIDES_MULTIPLE),
        ((13, 13), R.BOTH_SIDES_MULTIPLE),
    ],
)
@pytest.mark.parametrize("exhausted", [False, True])
def test_structural_rejection_precedes_bounds_and_budget(monkeypatch, counts, reason, exhausted):
    monkeypatch.setattr(subset_matching, "combinations", _fail_search)
    monkeypatch.setattr(subset_matching, "sum_decimals", _fail_search)
    rows = tuple(
        tuple(_record(source, ordinal, "1") for ordinal in range(2, count + 2))
        for source, count in zip((Source.A, Source.B), counts, strict=True)
    )
    analysis = analyze_bounded_one_to_many(
        *rows, remaining_planned_budget=0, run_budget_exhausted=exhausted
    )
    _assert_unresolved(analysis, rows, S.NOT_ELIGIBLE, reason)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("container", [list, iter, lambda rows: None, lambda rows: "rows"])
def test_rejects_mutable_or_non_tuple_containers(side, container):
    rows = list(_group())
    rows[side] = container(rows[side])
    with pytest.raises(TypeError, match="tuple of SourceRecord"):
        _analyze(rows)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("member", [None, "record", Decimal("1"), object()])
def test_rejects_non_record_members(side, member):
    rows = list(_group())
    rows[side] = (member,)
    with pytest.raises(TypeError, match="tuple of SourceRecord"):
        _analyze(rows)


@pytest.mark.parametrize("side", [0, 1])
def test_rejects_wrong_source(side):
    rows = list(_group())
    rows[side] = (replace(rows[side][0], source=Source.B if side == 0 else Source.A),)
    with pytest.raises(ValueError, match="must contain only File"):
        _analyze(rows)


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("ordinals", [(9, 3), (3, 3)])
def test_rejects_unsorted_and_repeated_ordinals(source, ordinals):
    with pytest.raises(ValueError, match="ordered by source_row without duplicates"):
        _analyze(_group(source=source, ordinals=ordinals))


@pytest.mark.parametrize("policy", [None, "exact_unique_v1", {}, object()])
def test_rejects_non_policy_values_even_for_ordinary_groups(policy):
    with pytest.raises(TypeError, match="policy must be a OneToManyPolicy"):
        _analyze(_group(amounts=("300",)), policy=policy)


@pytest.mark.parametrize(
    "feature,value,required",
    [
        ("minimum_accepted_subset_rows", 3, "2"),
        ("exact_amounts_only", False, "True"),
        ("physical_row_uniqueness", False, "True"),
        ("singleton_rivals_count", False, "True"),
    ],
)
@pytest.mark.parametrize("amounts", [("100", "200"), ("300",)], ids=["searchable", "ordinary"])
def test_unsupported_policy_semantics_rejected_before_search_or_early_return(
    monkeypatch, feature, value, required, amounts
):
    monkeypatch.setattr(subset_matching, "combinations", _fail_search)
    monkeypatch.setattr(subset_matching, "sum_decimals", _fail_search)
    canonical_snapshot = _model_bytes(POLICY)
    policy = replace(POLICY, **{feature: value})
    with pytest.raises(ValueError, match=f"{feature} must be {required}"):
        _analyze(_group(amounts=amounts), policy=policy)
    assert _model_bytes(POLICY) == canonical_snapshot


def test_supported_semantics_allow_alternate_policy_id_and_numerical_limits():
    policy = replace(
        POLICY,
        policy_id="isolated_test",
        max_candidate_rows=3,
        max_planned_combinations_per_run=7,
        max_ambiguity_witnesses=3,
    )
    analysis = analyze_bounded_one_to_many(
        *_group("300", ("100", "100", "200")), policy=policy, remaining_planned_budget=7
    )
    assert policy is not POLICY
    assert analysis.policy is policy
    assert analysis.status is S.AMBIGUOUS
    assert len(analysis.ambiguity_witnesses) == 2
    assert analysis.reserved_combinations == 7


def test_canonical_policy_remains_unchanged_after_validation():
    analysis = _analyze(_group())
    assert analysis.status is S.UNIQUE_EXACT
    assert analysis.policy is POLICY
    assert {field.name: getattr(POLICY, field.name) for field in fields(POLICY)} == {
        "policy_id": "exact_unique_v1",
        "max_candidate_rows": 12,
        "minimum_accepted_subset_rows": 2,
        "max_planned_combinations_per_run": 1_000_000,
        "max_ambiguity_witnesses": 2,
        "exact_amounts_only": True,
        "physical_row_uniqueness": True,
        "singleton_rivals_count": True,
    }


class _IntSubclass(int):
    pass


@pytest.mark.parametrize("budget", [True, False, 1.0, Decimal("1"), "1", None, _IntSubclass(1)])
def test_budget_requires_an_actual_integer(budget):
    with pytest.raises(TypeError, match="remaining_planned_budget must be an integer"):
        analyze_bounded_one_to_many(*_group(), remaining_planned_budget=budget)


def test_budget_must_be_nonnegative():
    with pytest.raises(ValueError, match="nonnegative"):
        analyze_bounded_one_to_many(*_group(), remaining_planned_budget=-1)


@pytest.mark.parametrize("exhausted", [0, 1, None, "false", 1.0])
def test_exhausted_flag_requires_an_actual_boolean(exhausted):
    with pytest.raises(TypeError, match="run_budget_exhausted must be a boolean"):
        _analyze(_group(), run_budget_exhausted=exhausted)


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("count", [2, 12])
def test_full_set_unique_search_at_candidate_boundaries(source, count):
    rows = _group(str(count), ("1",) * count, source)
    planned = (1 << count) - 1
    analysis = analyze_bounded_one_to_many(*rows, remaining_planned_budget=planned)
    assert analysis.status is S.UNIQUE_EXACT
    assert analysis.policy is POLICY
    assert analysis.reason is None
    assert analysis.ambiguity_witnesses == ()
    assert analysis.unassigned_rows_a == analysis.unassigned_rows_b == ()
    assert analysis.search_complete
    assert (
        analysis.planned_combinations,
        analysis.examined_combinations,
        analysis.reserved_combinations,
    ) == (planned,) * 3
    solution = analysis.accepted_solution
    assert solution.amount == Decimal(count)
    assert all(actual is parent for actual, parent in zip(solution.rows_a, rows[0], strict=True))
    assert all(actual is parent for actual, parent in zip(solution.rows_b, rows[1], strict=True))


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("exhausted", [False, True])
def test_thirteen_candidates_rejected_without_planning_or_enumeration(
    monkeypatch, source, exhausted
):
    monkeypatch.setattr(subset_matching, "combinations", _fail_search)
    monkeypatch.setattr(subset_matching, "sum_decimals", _fail_search)
    rows = _group("13", ("1",) * 13, source)
    analysis = analyze_bounded_one_to_many(
        *rows, remaining_planned_budget=0, run_budget_exhausted=exhausted
    )
    _assert_unresolved(analysis, rows, S.BOUND_EXCEEDED, R.CANDIDATE_ROW_LIMIT)


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("count", [2, 12])
@pytest.mark.parametrize("prior_exhaustion", [False, True])
def test_budget_rejection_never_partially_enumerates(monkeypatch, source, count, prior_exhaustion):
    monkeypatch.setattr(subset_matching, "combinations", _fail_search)
    monkeypatch.setattr(subset_matching, "sum_decimals", _fail_search)
    rows = _group(str(count), ("1",) * count, source)
    planned = (1 << count) - 1
    analysis = analyze_bounded_one_to_many(
        *rows,
        remaining_planned_budget=planned if prior_exhaustion else planned - 1,
        run_budget_exhausted=prior_exhaustion,
    )
    _assert_unresolved(
        analysis, rows, S.BOUND_EXCEEDED, R.RUN_BUDGET_EXHAUSTED, counters=(planned, 0, 0)
    )


def test_supplied_policy_snapshot_and_bounds_are_used():
    policy = replace(POLICY, max_candidate_rows=3, max_planned_combinations_per_run=7)
    rows = _group("3", ("1",) * 3)
    analysis = analyze_bounded_one_to_many(*rows, policy=policy, remaining_planned_budget=7)
    assert analysis.policy is policy
    assert analysis.status is S.UNIQUE_EXACT
    assert analysis.reserved_combinations == 7
    rejected = analyze_bounded_one_to_many(
        *_group("4", ("1",) * 4), policy=policy, remaining_planned_budget=7
    )
    assert rejected.reason is R.CANDIDATE_ROW_LIMIT
    assert rejected.planned_combinations == rejected.reserved_combinations == 0


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize(
    "amount,amounts,status,selected,residual,examined,complete",
    [
        ("300", ("100", "200"), S.UNIQUE_EXACT, (3, 4), (), 3, True),
        ("300", ("100", "700", "200"), S.UNIQUE_EXACT, (3, 5), (4,), 7, True),
        ("200", ("100", "100"), S.UNIQUE_EXACT, (3, 4), (), 3, True),
        ("99", ("40", "60"), S.NO_EXACT_SUBSET, (), (3, 4), 3, True),
        ("300", ("100", "199.99"), S.NO_EXACT_SUBSET, (), (3, 4), 3, True),
        ("300", ("300", "10"), S.SINGLETON_ONLY, (3,), (3, 4), 3, True),
        ("100", ("1", "100", "2"), S.SINGLETON_ONLY, (4,), (3, 4, 5), 7, True),
        ("300", ("300", "100", "200"), S.AMBIGUOUS, ((3,), (4, 5)), (3, 4, 5), 6, False),
        ("300", ("100", "100", "200"), S.AMBIGUOUS, ((3, 5), (4, 5)), (3, 4, 5), 6, False),
        ("100", ("150", "-50"), S.UNIQUE_EXACT, (3, 4), (), 3, True),
        ("-100", ("-150", "50"), S.UNIQUE_EXACT, (3, 4), (), 3, True),
        ("0", ("150", "-150"), S.UNIQUE_EXACT, (3, 4), (), 3, True),
        ("0", ("0", "0"), S.AMBIGUOUS, ((3,), (4,)), (3, 4), 2, False),
        ("100", ("150", "-50", "0"), S.AMBIGUOUS, ((3, 4), (3, 4, 5)), (3, 4, 5), 7, True),
    ],
)
def test_search_outcomes_in_both_directions(
    source, amount, amounts, status, selected, residual, examined, complete
):
    rows = _group(amount, amounts, source)
    analysis = _analyze(rows)
    planned = (1 << len(amounts)) - 1
    candidates = rows[1] if source is Source.A else rows[0]
    assert analysis.status is status
    assert analysis.reason is None
    assert analysis.policy is POLICY
    assert (
        analysis.planned_combinations,
        analysis.examined_combinations,
        analysis.reserved_combinations,
    ) == (planned, examined, planned)
    assert analysis.search_complete is complete
    _assert_original_references(analysis, rows)
    if status is S.UNIQUE_EXACT:
        solution = analysis.accepted_solution
        assert solution.amount is (rows[0][0] if source is Source.A else rows[1][0]).amount
        assert tuple(row.source_row for row in _candidate_rows(solution, source)) == selected
        assert analysis.ambiguity_witnesses == ()
        anchor_residuals = (
            analysis.unassigned_rows_a if source is Source.A else analysis.unassigned_rows_b
        )
        assert anchor_residuals == ()
        unassigned = (
            analysis.unassigned_rows_b if source is Source.A else analysis.unassigned_rows_a
        )
        assert tuple(row.source_row for row in unassigned) == residual
        selected_ids = {id(row) for row in _candidate_rows(solution, source)}
        assert all(
            (id(row) in selected_ids) != any(row is rest for rest in unassigned)
            for row in candidates
        )
    else:
        _assert_unresolved(
            analysis, rows, status, counters=(planned, examined, planned), complete=complete
        )
        if status is S.SINGLETON_ONLY:
            assert len(analysis.ambiguity_witnesses) == 1
            assert (
                tuple(
                    row.source_row
                    for row in _candidate_rows(analysis.ambiguity_witnesses[0], source)
                )
                == selected
            )
        elif status is S.AMBIGUOUS:
            assert len(analysis.ambiguity_witnesses) == 2
            assert (
                tuple(
                    tuple(row.source_row for row in _candidate_rows(witness, source))
                    for witness in analysis.ambiguity_witnesses
                )
                == selected
            )


def test_different_original_keys_and_secondary_evidence_are_not_search_constraints():
    rows_a, rows_b = _group()
    rows_a = (replace(rows_a[0], key=("INV",), raw_fields={"department": "Sales"}),)
    rows_b = tuple(replace(row, key=("inv",), raw_fields={"department": "Other"}) for row in rows_b)
    analysis = _analyze((rows_a, rows_b))
    assert analysis.status is S.UNIQUE_EXACT
    _assert_original_references(analysis, (rows_a, rows_b))


@pytest.mark.parametrize("source", [Source.A, Source.B])
def test_cardinality_then_source_row_order_and_second_solution_stop(monkeypatch, source):
    rows = _group("300", ("100", "100", "200", "300", "100"), source, (3, 9, 17, 25, 40))
    candidates = rows[1] if source is Source.A else rows[0]
    tested = []
    actual_sum = subset_matching.sum_decimals

    def observed_combinations(records, cardinality):
        assert records is candidates
        for subset in combinations(records, cardinality):
            tested.append(tuple(row.source_row for row in subset))
            yield subset

    tested_amounts = []

    def observed_sum(amounts):
        tested_amounts.append(tuple(amounts))
        return actual_sum(tested_amounts[-1])

    monkeypatch.setattr(subset_matching, "combinations", observed_combinations)
    monkeypatch.setattr(subset_matching, "sum_decimals", observed_sum)
    analysis = _analyze(rows)
    assert tested == [(3,), (9,), (17,), (25,), (40,), (3, 9), (3, 17)]
    assert len(tested_amounts) == analysis.examined_combinations == 7
    assert all(
        amounts == tuple(row.amount for row in candidates if row.source_row in ordinals)
        for amounts, ordinals in zip(tested_amounts, tested, strict=True)
    )
    assert analysis.planned_combinations == analysis.reserved_combinations == 31
    assert not analysis.search_complete
    assert tuple(
        tuple(row.source_row for row in _candidate_rows(witness, source))
        for witness in analysis.ambiguity_witnesses
    ) == ((25,), (3, 17))
    _assert_original_references(analysis, rows)


def test_nonconsecutive_ordinals_preserve_residual_order():
    rows = _group("300", ("50", "100", "700", "200", "900"), ordinals=(3, 8, 17, 24, 90))
    analysis = _analyze(rows)
    assert analysis.status is S.UNIQUE_EXACT
    assert tuple(row.source_row for row in analysis.accepted_solution.rows_b) == (8, 24)
    assert tuple(row.source_row for row in analysis.unassigned_rows_b) == (3, 17, 90)
    _assert_original_references(analysis, rows)


def test_pure_caller_budget_transition_is_sticky_and_charges_full_reservations():
    remaining = 10
    exhausted = False
    groups = (
        (_group("2", ("1", "1")), S.UNIQUE_EXACT, None, 7, False),
        (((), _group()[1]), S.NOT_ELIGIBLE, R.MISSING_OPPOSITE_SIDE, 7, False),
        (_group("13", ("1",) * 13), S.BOUND_EXCEEDED, R.CANDIDATE_ROW_LIMIT, 7, False),
        (_group("0", ("0", "0", "1")), S.AMBIGUOUS, None, 0, False),
        (_group(), S.BOUND_EXCEEDED, R.RUN_BUDGET_EXHAUSTED, 0, True),
        (_group("0", ("0", "0")), S.BOUND_EXCEEDED, R.RUN_BUDGET_EXHAUSTED, 0, True),
    )
    for rows, status, reason, expected_remaining, expected_exhausted in groups:
        analysis = analyze_bounded_one_to_many(
            *rows, remaining_planned_budget=remaining, run_budget_exhausted=exhausted
        )
        assert analysis.status is status
        assert analysis.reason is reason
        if analysis.reason is R.RUN_BUDGET_EXHAUSTED:
            exhausted = True
        else:
            remaining -= analysis.reserved_combinations
        assert remaining == expected_remaining
        assert exhausted is expected_exhausted
        assert remaining >= 0


def test_sticky_flag_rejects_a_smaller_group_that_could_still_fit():
    failure = analyze_bounded_one_to_many(*_group("4", ("1",) * 4), remaining_planned_budget=3)
    assert failure.reason is R.RUN_BUDGET_EXHAUSTED
    smaller = analyze_bounded_one_to_many(
        *_group("2", ("1", "1")), remaining_planned_budget=3, run_budget_exhausted=True
    )
    assert smaller.reason is R.RUN_BUDGET_EXHAUSTED
    assert smaller.planned_combinations == 3
    assert smaller.examined_combinations == smaller.reserved_combinations == 0


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize(
    "amount,amounts,status",
    [
        ("1E+1000", ("6E+999", "4E+999"), S.UNIQUE_EXACT),
        ("1E-1000", ("1E+1000", "-1E+1000", "1E-1000"), S.AMBIGUOUS),
        ("0", ("1E+1000", "-1E+1000", "1E-1000"), S.UNIQUE_EXACT),
        ("1E-1000", ("6E-1001", "4E-1001"), S.UNIQUE_EXACT),
        ("3.000000000000000000000000000001", ("1", "2", "1E-30"), S.UNIQUE_EXACT),
        ("3", ("1", "2.000000000000000000000000000001"), S.NO_EXACT_SUBSET),
    ],
)
def test_hostile_decimal_context_does_not_change_results_or_caller_flags(
    source, amount, amounts, status
):
    rows = _group(amount, amounts, source)
    expected = _analyze(rows)
    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        context.rounding = ROUND_DOWN
        context.clamp = 1
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        before = context.copy()
        analysis = _analyze(rows)
        assert analysis.status is status
        assert _model_bytes(analysis) == _model_bytes(expected)
        assert context.flags == before.flags
        assert not any(context.flags.values())
        assert context.traps == before.traps
        assert (context.prec, context.Emax, context.Emin, context.rounding, context.clamp) == (
            before.prec,
            before.Emax,
            before.Emin,
            before.rounding,
            before.clamp,
        )


@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize(
    "amount,amounts",
    [
        ("300", ("100", "700", "200")),
        ("300", ("100", "100", "200")),
        ("100", ("150", "-50", "0")),
        ("0", ("0", "0")),
        ("300", ("1", "300", "2")),
        ("99", ("40", "60")),
    ],
)
def test_repeated_calls_produce_identical_model_bytes_without_changing_records(
    source, amount, amounts
):
    rows = _group(amount, amounts, source)
    original_bytes = _model_bytes(rows)
    expected = _model_bytes(_analyze(rows))
    for _ in range(4):
        analysis = _analyze(rows)
        assert _model_bytes(analysis) == expected
        _assert_original_references(analysis, rows)
        assert _model_bytes(rows) == original_bytes


def test_primitive_remains_internal_and_modes_are_exactly_the_three_supported_modes():
    assert tuple(ReconciliationMode) == (
        ReconciliationMode.UNIQUE,
        ReconciliationMode.GROUPED_BY_KEY,
        ReconciliationMode.BOUNDED_ONE_TO_MANY,
    )
    assert not hasattr(tallydiff, "analyze_bounded_one_to_many")
    assert engine.analyze_bounded_one_to_many is analyze_bounded_one_to_many


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("comparisons", [False, True])
def test_legacy_reconcile_never_invokes_search(monkeypatch, mode, normalized, comparisons):
    monkeypatch.setattr(subset_matching, "analyze_bounded_one_to_many", _fail_search)
    monkeypatch.setattr(subset_matching, "combinations", _fail_search)
    monkeypatch.setattr(subset_matching, "sum_decimals", _fail_search)
    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", _fail_search, raising=False)
    rows_a, rows_b = _group("300", ("100", "200.01"))
    if normalized:
        rows_b = tuple(replace(row, key=("inv",)) for row in rows_b)
    normalization = (
        KeyNormalizationConfig((KeyNormalizationRules(casefold=True),)) if normalized else None
    )
    comparison_fields = (ComparisonFieldMapping("evidence", "evidence"),) if comparisons else ()
    result = reconcile(
        rows_a,
        rows_b,
        mode=mode,
        amount_tolerance=Decimal("0.01"),
        key_normalization=normalization,
        comparison_fields=comparison_fields,
    )
    assert result.one_to_many_policy is None
    assert all(finding.correspondence_analysis is None for finding in result.findings)
    assert result.findings[0].category is (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.WITHIN_TOLERANCE
    )
    assert result.control_difference == result.finding_delta_sum == Decimal("-0.01")
