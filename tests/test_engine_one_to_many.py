"""Bounded inference integrates with whole-key accounting and review semantics."""

from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
from inspect import signature

import pytest

import tallydiff.engine as engine
import tallydiff.subset_matching as subset_matching
from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ComparisonFieldMapping,
    CorrespondenceReason,
    CorrespondenceStatus,
    FieldComparisonStatus,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    NormalizationCollisionError,
    ReconciliationFinding,
    ReconciliationIntegrityError,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    reconcile,
)
from tallydiff._decimal import sum_decimals

MODE = ReconciliationMode.BOUNDED_ONE_TO_MANY
POLICY = EXACT_UNIQUE_ONE_TO_MANY_POLICY
DEPARTMENT = ComparisonFieldMapping("department", "cost_center")
S = CorrespondenceStatus
R = CorrespondenceReason
C = FindingCategory
F = FieldComparisonStatus


def _record(source, ordinal, amount, key="INV", value="Sales"):
    return SourceRecord(
        source,
        ordinal,
        (key,),
        Decimal(amount),
        {"key": key, "department": value, "cost_center": value},
    )


def _group(amounts_a, amounts_b, key="INV"):
    return tuple(
        tuple(_record(source, row, value, key) for row, value in enumerate(amounts, start=2))
        for source, amounts in ((Source.A, amounts_a), (Source.B, amounts_b))
    )


def _one_many(amount="300", amounts=("100", "200"), source=Source.A):
    return _group((amount,), amounts) if source is Source.A else _group(amounts, (amount,))


def _ledger(specifications):
    return tuple(
        tuple(
            _record(source, ordinal, amount, key)
            for ordinal, (key, amount) in enumerate(
                (
                    (key, amount)
                    for key, a, b in specifications
                    for amount in (a if source is Source.A else b)
                ),
                start=2,
            )
        )
        for source in Source
    )


def _reconcile(rows, **kwargs):
    return reconcile(*rows, mode=MODE, **kwargs)


def _assert_accounting(result, rows):
    assert result.total_a == sum_decimals(row.amount for row in rows[0])
    assert result.total_b == sum_decimals(row.amount for row in rows[1])
    assert result.control_difference == result.finding_delta_sum
    assert result.control_difference == sum_decimals(
        (sum_decimals(finding.delta for finding in result.exceptions), result.tolerated_delta_total)
    )
    retained = tuple(
        row for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)
    )
    assert Counter(map(id, retained)) == Counter(map(id, (*rows[0], *rows[1])))
    assert {id(finding) for finding in result.exceptions}.isdisjoint(
        id(finding) for finding in result.tolerated_findings
    )


def _forbidden(*args, **kwargs):
    pytest.fail("subset inference must not run before validation succeeds")


def test_exactly_three_modes_and_no_configurable_engine_policy():
    assert [(mode.name, mode.value) for mode in ReconciliationMode] == [
        ("UNIQUE", "unique"),
        ("GROUPED_BY_KEY", "grouped_by_key"),
        ("BOUNDED_ONE_TO_MANY", "bounded_one_to_many"),
    ]
    assert len(FindingCategory) == 6
    assert tuple(signature(reconcile).parameters) == (
        "records_a",
        "records_b",
        "amount_tolerance",
        "mode",
        "key_normalization",
        "comparison_fields",
    )


def test_empty_bounded_result_retains_canonical_policy():
    result = _reconcile((iter(()), iter(())))
    assert result.mode is MODE
    assert result.one_to_many_policy is POLICY
    assert result.findings == result.exceptions == result.tolerated_findings == ()
    assert (
        result.control_difference
        == result.finding_delta_sum
        == result.tolerated_delta_total
        == Decimal("0")
    )


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("policy", [POLICY, replace(POLICY, policy_id="isolated")])
def test_legacy_results_reject_any_policy_metadata(mode, policy):
    with pytest.raises(ValueError, match="legacy modes.*None"):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), mode=mode, one_to_many_policy=policy)


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_legacy_results_reject_analysis_without_policy(mode):
    finding = _reconcile(_one_many()).findings[0]
    with pytest.raises(ValueError, match="analysis requires BOUNDED_ONE_TO_MANY"):
        ReconciliationResult(Decimal("300"), Decimal("300"), (finding,), mode=mode)


@pytest.mark.parametrize(
    "feature,value",
    [
        ("policy_id", "other"),
        ("max_candidate_rows", 11),
        ("minimum_accepted_subset_rows", 3),
        ("max_planned_combinations_per_run", 999_999),
        ("max_ambiguity_witnesses", 3),
        ("exact_amounts_only", False),
        ("physical_row_uniqueness", False),
        ("singleton_rivals_count", False),
    ],
)
def test_bounded_results_reject_every_noncanonical_policy_variant(feature, value):
    with pytest.raises(ValueError, match="canonical one_to_many_policy"):
        ReconciliationResult(
            Decimal("0"),
            Decimal("0"),
            (),
            mode=MODE,
            one_to_many_policy=replace(POLICY, **{feature: value}),
        )


def test_bounded_result_requires_canonical_policy_but_accepts_value_equal_snapshot():
    with pytest.raises(ValueError, match="canonical one_to_many_policy"):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), mode=MODE)
    equivalent = replace(POLICY)
    assert equivalent is not POLICY
    result = ReconciliationResult(
        Decimal("0"), Decimal("0"), (), mode=MODE, one_to_many_policy=equivalent
    )
    assert result.one_to_many_policy is equivalent


@pytest.mark.parametrize(
    "amounts_a,amounts_b,category",
    [
        (("100",), ("100",), C.EXACT_MATCH),
        (("100",), ("100.03",), C.WITHIN_TOLERANCE),
        (("100",), ("100.06",), C.AMOUNT_MISMATCH),
        (("100",), (), C.A_ONLY),
        ((), ("100",), C.B_ONLY),
    ],
)
@pytest.mark.parametrize("value_b", ["Sales", "Other"])
def test_ordinary_shapes_preserve_amount_and_secondary_rules(
    amounts_a, amounts_b, category, value_b
):
    rows_a, rows_b = _group(amounts_a, amounts_b)
    rows_b = tuple(
        replace(row, raw_fields={**row.raw_fields, "cost_center": value_b}) for row in rows_b
    )
    rows = rows_a, rows_b
    result = _reconcile(rows, amount_tolerance=Decimal("0.05"), comparison_fields=(DEPARTMENT,))
    finding = result.findings[0]
    assert finding.category is category
    assert finding.correspondence_analysis is None
    assert not finding.has_inferred_correspondence
    comparison = finding.field_comparisons[0]
    expected_secondary = (
        F.NOT_COMPARABLE
        if not rows_a or not rows_b
        else F.MATCH
        if value_b == "Sales"
        else F.MISMATCH
    )
    assert comparison.status is expected_secondary
    assert comparison.values_a == (("Sales",) if rows_a else ())
    assert comparison.values_b == ((value_b,) if rows_b else ())
    assert finding.is_exception is (
        category not in (C.EXACT_MATCH, C.WITHIN_TOLERANCE) or expected_secondary is F.MISMATCH
    )
    assert result.tolerated_findings == (
        (finding,) if category is C.WITHIN_TOLERANCE and not finding.is_exception else ()
    )
    _assert_accounting(result, rows)


@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize(
    "amount,amounts,residual",
    [
        ("300", ("100", "200"), False),
        ("-100", ("-150", "50"), False),
        ("0", ("150", "-150"), False),
        ("300", ("100", "200", "0.01"), True),
    ],
)
def test_unique_inference_preserves_whole_group_and_requires_review(
    source, amount, amounts, residual
):
    rows = _one_many(amount, amounts, source)
    result = _reconcile(rows, amount_tolerance=Decimal("0.05"), comparison_fields=(DEPARTMENT,))
    finding = result.findings[0]
    analysis = finding.correspondence_analysis
    assert analysis.status is S.UNIQUE_EXACT
    assert analysis.policy is result.one_to_many_policy is POLICY
    assert finding.category is (C.AMOUNT_MISMATCH if residual else C.EXACT_MATCH)
    assert finding.is_exception and finding.has_inferred_correspondence
    assert result.exceptions == (finding,)
    assert result.tolerated_findings == ()
    assert finding.inferred_solution is analysis.accepted_solution
    solution = finding.inferred_solution
    assert solution.amount == Decimal(amount)
    assert solution.anchor is (rows[0][0] if source is Source.A else rows[1][0])
    candidates = rows[1] if source is Source.A else rows[0]
    assert all(
        actual is original
        for actual, original in zip(solution.candidate_rows, candidates[:2], strict=True)
    )
    unassigned = analysis.unassigned_rows_b if source is Source.A else analysis.unassigned_rows_a
    assert unassigned == (candidates[2:] if residual else ())
    assert all(
        actual is original for actual, original in zip(unassigned, candidates[2:], strict=True)
    )
    assert (
        analysis.planned_combinations
        == analysis.examined_combinations
        == analysis.reserved_combinations
        == (7 if residual else 3)
    )
    expected_delta = Decimal("0")
    if residual:
        expected_delta = Decimal("0.01" if source is Source.B else "-0.01")
    assert finding.delta == expected_delta
    assert (
        finding.field_comparisons[0].values_a == finding.field_comparisons[0].values_b == ("Sales",)
    )
    assert finding.field_comparisons[0].status is F.NOT_COMPARABLE
    _assert_accounting(result, rows)


@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize(
    "amount,amounts,status,reason",
    [
        ("300", ("100", "100", "200"), S.AMBIGUOUS, None),
        ("300", ("300", "1"), S.SINGLETON_ONLY, None),
        ("300", ("100", "199.99"), S.NO_EXACT_SUBSET, None),
        ("13", ("1",) * 13, S.BOUND_EXCEEDED, R.CANDIDATE_ROW_LIMIT),
        ("0", ("0", "0"), S.AMBIGUOUS, None),
        ("100", ("150", "-50", "0"), S.AMBIGUOUS, None),
    ],
)
def test_unresolved_searches_remain_duplicate_ambiguous(source, amount, amounts, status, reason):
    rows = _one_many(amount, amounts, source)
    result = _reconcile(rows, amount_tolerance=Decimal("1000"), comparison_fields=(DEPARTMENT,))
    finding = result.findings[0]
    analysis = finding.correspondence_analysis
    assert finding.category is C.DUPLICATE_AMBIGUOUS
    assert analysis.status is status
    assert analysis.reason is reason
    assert analysis.unassigned_rows_a is finding.rows_a
    assert analysis.unassigned_rows_b is finding.rows_b
    assert finding.inferred_solution is None
    assert not finding.has_inferred_correspondence
    assert finding.is_exception
    assert finding.field_comparisons[0].status is F.NOT_COMPARABLE
    _assert_accounting(result, rows)


@pytest.mark.parametrize(
    "amounts_a,amounts_b,reason",
    [
        (("100", "200"), (), R.MISSING_OPPOSITE_SIDE),
        ((), ("100", "200"), R.MISSING_OPPOSITE_SIDE),
        (("100", "200"), ("150", "150"), R.BOTH_SIDES_MULTIPLE),
    ],
)
def test_structural_duplicate_groups_are_not_grouped_by_totals(amounts_a, amounts_b, reason):
    rows = _group(amounts_a, amounts_b)
    result = _reconcile(rows, comparison_fields=(DEPARTMENT,))
    finding = result.findings[0]
    assert finding.category is C.DUPLICATE_AMBIGUOUS
    assert finding.correspondence_status is S.NOT_ELIGIBLE
    assert finding.correspondence_analysis.reason is reason
    assert finding.correspondence_analysis.reserved_combinations == 0
    assert finding.field_comparisons[0].status is F.NOT_COMPARABLE
    _assert_accounting(result, rows)


@pytest.mark.parametrize(
    "mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY, MODE]
)
def test_each_mode_has_explicit_duplicate_classification(mode):
    rows = _group(("300",), ("100", "199.99"))
    result = reconcile(*rows, mode=mode, amount_tolerance=Decimal("0.05"))
    assert result.findings[0].category is (
        C.WITHIN_TOLERANCE if mode is ReconciliationMode.GROUPED_BY_KEY else C.DUPLICATE_AMBIGUOUS
    )
    assert (result.one_to_many_policy is not None) is (mode is MODE)


def test_unknown_classifier_mode_cannot_fall_through_to_grouped_logic():
    rows = _one_many()
    with pytest.raises(ReconciliationIntegrityError, match="unsupported reconciliation mode"):
        engine._classify(*rows, Decimal("300"), Decimal("300"), Decimal("0"), "future_mode")


@pytest.mark.parametrize("source", list(Source))
def test_secondary_summaries_include_residuals_and_never_disambiguate(source):
    rows = _one_many("300", ("100", "200", "0.01"), source)
    candidate_side = 1 if source is Source.A else 0
    changed = list(rows)
    changed[candidate_side] = tuple(
        replace(row, raw_fields={**row.raw_fields, "department": value, "cost_center": value})
        for row, value in zip(
            rows[candidate_side], ("Sales", "Sales", "Residual only"), strict=True
        )
    )
    initial = _reconcile(rows, comparison_fields=(DEPARTMENT,)).findings[0]
    finding = _reconcile(changed, comparison_fields=(DEPARTMENT,)).findings[0]
    assert initial.correspondence_status is finding.correspondence_status is S.UNIQUE_EXACT
    assert tuple(row.source_row for row in initial.inferred_solution.candidate_rows) == tuple(
        row.source_row for row in finding.inferred_solution.candidate_rows
    )
    comparison = finding.field_comparisons[0]
    assert comparison.status is F.NOT_COMPARABLE
    assert (comparison.values_b if source is Source.A else comparison.values_a) == (
        "Residual only",
        "Sales",
    )
    assert (comparison.values_a if source is Source.A else comparison.values_b) == ("Sales",)
    assert all(row.raw_fields["department"] == "Sales" for row in rows[candidate_side])


def test_secondary_evidence_cannot_break_financial_ambiguity():
    rows_a, rows_b = _one_many("300", ("100", "100", "200"))
    rows_b = tuple(
        replace(row, raw_fields={**row.raw_fields, "cost_center": value})
        for row, value in zip(rows_b, ("Sales", "Other", "Sales"), strict=True)
    )
    finding = _reconcile((rows_a, rows_b), comparison_fields=(DEPARTMENT,)).findings[0]
    assert finding.correspondence_status is S.AMBIGUOUS
    assert tuple(
        tuple(row.source_row for row in witness.rows_b)
        for witness in finding.correspondence_analysis.ambiguity_witnesses
    ) == ((2, 4), (3, 4))
    assert finding.field_comparisons[0].status is F.NOT_COMPARABLE
    assert finding.field_comparisons[0].values_b == ("Other", "Sales")


def test_review_partition_includes_zero_delta_inference_and_excludes_reviewed_tolerance():
    rows_a, rows_b = _ledger(
        [
            ("EXACT", ("100",), ("100",)),
            ("INFERRED", ("300",), ("100", "200")),
            ("RESIDUAL", ("300",), ("100", "200", "0.01")),
            ("TOL_OK", ("100",), ("100.03",)),
            ("TOL_REVIEW", ("100",), ("100.04",)),
            ("ORPHAN", ("0.5",), ()),
        ]
    )
    rows_b = tuple(
        replace(row, raw_fields={**row.raw_fields, "cost_center": "Other"})
        if row.key == ("TOL_REVIEW",)
        else row
        for row in rows_b
    )
    rows = rows_a, rows_b
    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        result = _reconcile(rows, amount_tolerance=Decimal("0.05"), comparison_fields=(DEPARTMENT,))
        _assert_accounting(result, rows)
        assert result.control_difference == Decimal("0.42")
        assert result.tolerated_delta_total == Decimal("-0.03")
        assert [finding.key[0] for finding in result.exceptions] == [
            "INFERRED",
            "ORPHAN",
            "RESIDUAL",
            "TOL_REVIEW",
        ]
        assert [finding.key[0] for finding in result.tolerated_findings] == ["TOL_OK"]
        inferred = next(finding for finding in result.findings if finding.key == ("INFERRED",))
        assert inferred.category is C.EXACT_MATCH
        assert inferred.delta == Decimal("0")
        assert inferred in result.exceptions
        assert not any(context.flags.values())


def test_tolerated_membership_uses_the_complete_review_predicate(monkeypatch):
    result = _reconcile(_group(("100",), ("100.01",)), amount_tolerance=Decimal("0.05"))
    finding = result.findings[0]
    assert finding.category is C.WITHIN_TOLERANCE
    assert not finding.has_secondary_mismatch
    assert result.tolerated_findings == (finding,)
    monkeypatch.setattr(ReconciliationFinding, "is_exception", property(lambda self: True))
    assert result.exceptions == (finding,)
    assert result.tolerated_findings == ()
    assert result.tolerated_delta_total == Decimal("0")


def test_runtime_integrity_rejects_an_incomplete_review_partition():
    rows = _group(("100",), ("99",))
    finding = ReconciliationFinding(C.EXACT_MATCH, ("INV",), *rows, Decimal("100"), Decimal("99"))
    result = ReconciliationResult(Decimal("100"), Decimal("99"), (finding,))
    assert result.control_difference == result.finding_delta_sum == Decimal("1")
    with pytest.raises(
        ReconciliationIntegrityError, match="review exceptions and tolerated deltas"
    ):
        engine._assert_integrity(result, *rows)


def test_bounded_duplicate_shape_cannot_silently_skip_analysis(monkeypatch):
    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", lambda *args, **kwargs: None)
    with pytest.raises(ReconciliationIntegrityError, match="duplicate group requires analysis"):
        _reconcile(_one_many())


def test_runtime_integrity_rejects_analysis_on_an_ordinary_shape(monkeypatch):
    rows = _group(("300",), ("300",))
    template = _reconcile(_one_many("300", ("100", "199"))).findings[0].correspondence_analysis
    analysis = replace(template, unassigned_rows_a=rows[0], unassigned_rows_b=rows[1])
    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", lambda *args, **kwargs: analysis)
    with pytest.raises(ReconciliationIntegrityError, match="exactly on bounded duplicate-shaped"):
        _reconcile(rows)


def test_engine_protects_against_negative_remaining_budget(monkeypatch):
    rows = _one_many("300", ("100", "199"))
    analysis = _reconcile(rows).findings[0].correspondence_analysis
    over_reserved = replace(
        analysis, reserved_combinations=POLICY.max_planned_combinations_per_run + 1
    )
    monkeypatch.setattr(
        engine, "analyze_bounded_one_to_many", lambda *args, **kwargs: over_reserved
    )
    with pytest.raises(ReconciliationIntegrityError, match="budget became negative"):
        _reconcile(rows)


@pytest.mark.parametrize("sources", [(Source.A,), (Source.B,), (Source.A, Source.B)])
def test_normalization_collisions_block_before_any_subset_inference(monkeypatch, sources):
    rows = tuple(
        tuple(
            _record(source, ordinal, "100", key)
            for ordinal, key in enumerate(
                ("ACME-01", "ACME01") if source in sources else ("ACME01",), start=2
            )
        )
        for source in Source
    )
    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", _forbidden)
    with pytest.raises(NormalizationCollisionError) as error:
        _reconcile(
            rows,
            key_normalization=KeyNormalizationConfig(
                (KeyNormalizationRules(remove_punctuation=True),)
            ),
        )
    assert bool(error.value.collisions_a) is (Source.A in sources)
    assert bool(error.value.collisions_b) is (Source.B in sources)


@pytest.mark.parametrize("source", list(Source))
def test_normalized_cross_source_convergence_preserves_original_solution_evidence(source):
    rows = _one_many(source=source)
    rows = tuple(
        tuple(
            replace(
                row,
                key=("ACME-01" if row.source is source else "ACME01",),
                raw_fields={
                    **row.raw_fields,
                    "key": "ACME-01" if row.source is source else "ACME01",
                },
            )
            for row in side
        )
        for side in rows
    )
    result = _reconcile(
        rows,
        key_normalization=KeyNormalizationConfig((KeyNormalizationRules(remove_punctuation=True),)),
    )
    finding = result.findings[0]
    assert finding.key == ("ACME01",)
    assert finding.correspondence_status is S.UNIQUE_EXACT
    solution = finding.inferred_solution
    for name, parents in (("rows_a", rows[0]), ("rows_b", rows[1])):
        assert all(
            actual is parent
            for actual, parent in zip(getattr(solution, name), parents, strict=True)
        )
    assert solution.anchor.key == ("ACME-01",)
    assert solution.anchor.raw_fields["key"] == "ACME-01"
    assert all(
        row.key == ("ACME01",) and row.raw_fields["key"] == "ACME01"
        for row in solution.candidate_rows
    )
    _assert_accounting(result, rows)


@pytest.mark.parametrize("invalid", ["wrong_source", "duplicate_ordinal", "missing_secondary"])
def test_source_and_secondary_validation_still_precede_subset_search(monkeypatch, invalid):
    rows_a, rows_b = _one_many()
    options = {}
    if invalid == "wrong_source":
        rows_b = (replace(rows_b[0], source=Source.A), rows_b[1])
    elif invalid == "duplicate_ordinal":
        rows_b = (rows_b[0], replace(rows_b[1], source_row=rows_b[0].source_row))
    else:
        rows_b = (rows_b[0], replace(rows_b[1], raw_fields={}))
        options["comparison_fields"] = (DEPARTMENT,)
    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", _forbidden)
    with pytest.raises(ValueError):
        _reconcile((rows_a, rows_b), **options)


class _Once:
    def __init__(self, rows):
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        assert self.iterations == 1
        yield from self.rows


def _observe_search(monkeypatch):
    calls = []
    tested_subsets = []
    actual_analyze = engine.analyze_bounded_one_to_many
    actual_sum = subset_matching.sum_decimals

    def observed(rows_a, rows_b, **kwargs):
        analysis = actual_analyze(rows_a, rows_b, **kwargs)
        calls.append(((rows_a or rows_b)[0].key[0], kwargs, analysis))
        return analysis

    def observed_sum(amounts):
        amounts = tuple(amounts)
        tested_subsets.append(amounts)
        return actual_sum(amounts)

    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", observed)
    monkeypatch.setattr(subset_matching, "sum_decimals", observed_sum)
    return calls, tested_subsets


def test_new_mode_snapshots_once_and_analyzes_only_its_sorted_key_loop(monkeypatch):
    rows = _ledger(
        [
            ("Z_INFERRED", ("300",), ("100", "200")),
            ("A_ORDINARY", ("100",), ("100",)),
            ("M_ONE_SIDE", (), ("5",)),
        ]
    )
    inputs = tuple(_Once(tuple(reversed(side))) for side in rows)
    calls, tested = _observe_search(monkeypatch)
    result = _reconcile(inputs)
    assert all(source.iterations == 1 for source in inputs)
    assert [key for key, _, _ in calls] == ["A_ORDINARY", "M_ONE_SIDE", "Z_INFERRED"]
    assert [analysis is None for _, _, analysis in calls] == [True, True, False]
    assert all(options["policy"] is POLICY for _, options, _ in calls)
    assert len(tested) == 3
    _assert_accounting(result, rows)


def test_sorted_run_reservations_charge_early_ambiguity_and_stay_exhausted(monkeypatch):
    specifications = [(f"K{index:03d}", ("0",), ("0",) * 12) for index in range(244)]
    specifications += [
        ("L_FAILED", ("0",), ("0",) * 12),
        ("M_SMALL", ("0",), ("0", "0")),
        ("N_CANDIDATE_LIMIT", ("13",), ("1",) * 13),
        ("O_BOTH_MULTIPLE", ("100", "200"), ("150", "150")),
        ("P_ONE_SIDE_DUPLICATES", ("4", "6"), ()),
        ("Q_ORDINARY", ("300",), ("300",)),
        ("R_SINGLE_ONLY", ("8",), ()),
    ]
    rows = _ledger(specifications)
    calls, tested = _observe_search(monkeypatch)
    result = _reconcile(tuple(reversed(side) for side in rows), comparison_fields=(DEPARTMENT,))
    assert [key for key, _, _ in calls] == sorted(key for key, _, _ in specifications)
    for index, (_, options, analysis) in enumerate(calls[:244]):
        assert options["remaining_planned_budget"] == 1_000_000 - index * 4095
        assert not options["run_budget_exhausted"]
        assert analysis.status is S.AMBIGUOUS
        assert analysis.planned_combinations == analysis.reserved_combinations == 4095
        assert analysis.examined_combinations == 2
        assert not analysis.search_complete
    by_key = {key: (options, analysis) for key, options, analysis in calls}
    for key, sticky, planned in (("L_FAILED", False, 4095), ("M_SMALL", True, 3)):
        options, analysis = by_key[key]
        assert options["remaining_planned_budget"] == 820
        assert options["run_budget_exhausted"] is sticky
        assert analysis.status is S.BOUND_EXCEEDED
        assert analysis.reason is R.RUN_BUDGET_EXHAUSTED
        assert analysis.planned_combinations == planned
        assert analysis.examined_combinations == analysis.reserved_combinations == 0
    for key, reason in (
        ("N_CANDIDATE_LIMIT", R.CANDIDATE_ROW_LIMIT),
        ("O_BOTH_MULTIPLE", R.BOTH_SIDES_MULTIPLE),
        ("P_ONE_SIDE_DUPLICATES", R.MISSING_OPPOSITE_SIDE),
    ):
        options, analysis = by_key[key]
        assert options["run_budget_exhausted"]
        assert options["remaining_planned_budget"] == 820
        assert analysis.reason is reason
        assert analysis.reserved_combinations == analysis.examined_combinations == 0
    assert by_key["Q_ORDINARY"][1] is by_key["R_SINGLE_ONLY"][1] is None
    assert (
        len(tested)
        == 488
        == sum(analysis.examined_combinations for _, _, analysis in calls if analysis is not None)
    )
    assert (
        sum(analysis.reserved_combinations for _, _, analysis in calls if analysis is not None)
        == 999_180
    )
    assert all(
        finding.field_comparisons[0].status is F.NOT_COMPARABLE
        for finding in result.correspondence_analysis_findings
    )
    assert result.control_difference == Decimal("18")
    _assert_accounting(result, rows)


def test_exact_canonical_run_budget_boundary_succeeds_before_next_reservation_fails(monkeypatch):
    specifications = [(f"K{index:03d}", ("0",), ("0",) * 12) for index in range(244)]
    counts = (9, 8, 5, 3, 3, 2, 2, 2)
    assert sum((1 << count) - 1 for count in counts) == 820
    specifications += [
        (f"L{index:02d}", ("0",), ("0",) * count) for index, count in enumerate(counts)
    ]
    specifications += [("M_ORDINARY", ("1",), ("1",)), ("Z_AFTER_BOUNDARY", ("0",), ("0", "0"))]
    rows = _ledger(specifications)
    calls, tested = _observe_search(monkeypatch)
    result = _reconcile(rows)
    reserved = [
        analysis
        for _, _, analysis in calls
        if analysis is not None and analysis.reserved_combinations
    ]
    assert len(reserved) == 252
    assert sum(analysis.reserved_combinations for analysis in reserved) == 1_000_000
    _, options, last_reserved = calls[-3]
    assert options["remaining_planned_budget"] == last_reserved.reserved_combinations == 3
    assert last_reserved.status is S.AMBIGUOUS
    assert calls[-2][1]["remaining_planned_budget"] == 0
    assert not calls[-2][1]["run_budget_exhausted"]
    assert calls[-2][2] is None
    assert calls[-1][1]["remaining_planned_budget"] == 0
    assert not calls[-1][1]["run_budget_exhausted"]
    assert calls[-1][2].reason is R.RUN_BUDGET_EXHAUSTED
    assert calls[-1][2].reserved_combinations == calls[-1][2].examined_combinations == 0
    assert len(tested) == 504
    _assert_accounting(result, rows)
