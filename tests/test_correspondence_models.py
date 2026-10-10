"""Immutable correspondence contracts, parent accounting, and mode-policy consistency."""

from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, localcontext

import pytest

import tallydiff
from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ColumnMapping,
    ComparisonFieldMapping,
    CorrespondenceAnalysis,
    CorrespondenceReason,
    CorrespondenceStatus,
    FieldComparison,
    FieldComparisonStatus,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    OneToManyPolicy,
    ReconciliationFinding,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    SubsetSolution,
    export_exceptions_csv,
    export_mapping_profile,
    load_mapping_profile,
    reconcile,
)
from tallydiff._decimal import sum_decimals
from tallydiff.presentation import configuration_id

POLICY = EXACT_UNIQUE_ONE_TO_MANY_POLICY
INTEGER_POLICY_FIELDS = (
    "max_candidate_rows",
    "minimum_accepted_subset_rows",
    "max_planned_combinations_per_run",
    "max_ambiguity_witnesses",
)
BOOLEAN_POLICY_FIELDS = (
    "exact_amounts_only",
    "physical_row_uniqueness",
    "singleton_rivals_count",
)
COUNTERS = ("planned_combinations", "examined_combinations", "reserved_combinations")


def _record(source, row, amount, key="INV", raw_fields=None):
    return SourceRecord(source, row, (key,), Decimal(amount), raw_fields or {"evidence": str(row)})


def _solution(anchor_source=Source.A, amounts=("100", "200"), amount="300"):
    candidate_source = Source.B if anchor_source is Source.A else Source.A
    anchor = (_record(anchor_source, 2, amount),)
    candidates = tuple(
        _record(candidate_source, row, value) for row, value in enumerate(amounts, start=3)
    )
    rows_a, rows_b = (anchor, candidates) if anchor_source is Source.A else (candidates, anchor)
    return SubsetSolution(rows_a, rows_b, Decimal(amount))


def _analysis(solution=None, **overrides):
    if solution is None:
        solution = _solution()
    values = {
        "policy": POLICY,
        "status": CorrespondenceStatus.UNIQUE_EXACT,
        "accepted_solution": solution,
        "unassigned_rows_a": (),
        "unassigned_rows_b": (),
        "ambiguity_witnesses": (),
        "planned_combinations": 3,
        "examined_combinations": 3,
        "reserved_combinations": 3,
        "search_complete": True,
        "reason": None,
    }
    values.update(overrides)
    return CorrespondenceAnalysis(**values)


def _unresolved(status, reason=None, *, search_complete=None):
    solution = _solution()
    rows_a, rows_b = solution.rows_a, solution.rows_b
    witnesses = ()
    if status is CorrespondenceStatus.AMBIGUOUS:
        rows_b = (
            _record(Source.B, 3, "100"),
            _record(Source.B, 4, "100"),
            _record(Source.B, 5, "200"),
        )
        witnesses = (
            SubsetSolution(rows_a, (rows_b[0], rows_b[2]), Decimal("300")),
            SubsetSolution(rows_a, rows_b[1:], Decimal("300")),
        )
    elif status is CorrespondenceStatus.SINGLETON_ONLY:
        rows_b = (_record(Source.B, 3, "300"), _record(Source.B, 4, "50"))
        witnesses = (SubsetSolution(rows_a, rows_b[:1], Decimal("300")),)
    elif status is CorrespondenceStatus.NO_EXACT_SUBSET:
        rows_b = (_record(Source.B, 3, "100"), _record(Source.B, 4, "150"))
    elif reason is CorrespondenceReason.BOTH_SIDES_MULTIPLE:
        rows_a = (_record(Source.A, 2, "100"), _record(Source.A, 3, "200"))
    elif reason is CorrespondenceReason.MISSING_OPPOSITE_SIDE:
        rows_a = ()
    elif reason is CorrespondenceReason.CANDIDATE_ROW_LIMIT:
        rows_b = tuple(_record(Source.B, row, "1") for row in range(3, 16))
    complete = status in (
        CorrespondenceStatus.NO_EXACT_SUBSET,
        CorrespondenceStatus.SINGLETON_ONLY,
    )
    if search_complete is not None:
        complete = search_complete
    searched = status not in (
        CorrespondenceStatus.BOUND_EXCEEDED,
        CorrespondenceStatus.NOT_ELIGIBLE,
    )
    planned = (1 << len(rows_b)) - 1 if searched else 0
    return _analysis(
        status=status,
        accepted_solution=None,
        unassigned_rows_a=rows_a,
        unassigned_rows_b=rows_b,
        ambiguity_witnesses=witnesses,
        planned_combinations=planned,
        examined_combinations=planned if complete else (2 if searched else 0),
        reserved_combinations=planned,
        search_complete=complete,
        reason=reason,
    )


def _finding(analysis=None, **overrides):
    if analysis is None:
        solution = _solution()
        rows_a, rows_b = solution.rows_a, solution.rows_b
        category = FindingCategory.EXACT_MATCH
    else:
        solution = analysis.accepted_solution
        rows_a = tuple(
            sorted(
                (*(solution.rows_a if solution else ()), *analysis.unassigned_rows_a),
                key=lambda row: row.source_row,
            )
        )
        rows_b = tuple(
            sorted(
                (*(solution.rows_b if solution else ()), *analysis.unassigned_rows_b),
                key=lambda row: row.source_row,
            )
        )
        category = (
            FindingCategory.DUPLICATE_AMBIGUOUS
            if solution is None
            else (
                FindingCategory.AMOUNT_MISMATCH
                if analysis.unassigned_rows_a or analysis.unassigned_rows_b
                else FindingCategory.EXACT_MATCH
            )
        )
    values = {
        "category": category,
        "key": ("INV",),
        "rows_a": rows_a,
        "rows_b": rows_b,
        "amount_a": sum_decimals(row.amount for row in rows_a),
        "amount_b": sum_decimals(row.amount for row in rows_b),
        "correspondence_analysis": analysis,
    }
    values.update(overrides)
    return ReconciliationFinding(**values)


def test_package_root_exposes_all_correspondence_contracts_and_supported_modes():
    for name in (
        "OneToManyPolicy",
        "EXACT_UNIQUE_ONE_TO_MANY_POLICY",
        "CorrespondenceStatus",
        "CorrespondenceReason",
        "SubsetSolution",
        "CorrespondenceAnalysis",
    ):
        assert name in tallydiff.__all__
        assert getattr(tallydiff, name) is globals()[name]
    assert [(mode.name, mode.value) for mode in ReconciliationMode] == [
        ("UNIQUE", "unique"),
        ("GROUPED_BY_KEY", "grouped_by_key"),
        ("BOUNDED_ONE_TO_MANY", "bounded_one_to_many"),
    ]


def test_canonical_policy_snapshot_and_derived_limits():
    assert POLICY == OneToManyPolicy("exact_unique_v1", 12, 2, 1_000_000, 2, True, True, True)
    assert POLICY.max_total_rows == 13
    assert POLICY.max_combinations_per_key == 4095
    assert not hasattr(POLICY, "__dict__")
    with pytest.raises(FrozenInstanceError):
        POLICY.max_candidate_rows = 13


@pytest.mark.parametrize("policy_id", [None, 1, True, CorrespondenceStatus.UNIQUE_EXACT])
def test_policy_id_requires_an_exact_string(policy_id):
    with pytest.raises(TypeError, match="policy_id.*string"):
        replace(POLICY, policy_id=policy_id)


@pytest.mark.parametrize("policy_id", ["", " \t", "\u2003"])
def test_policy_id_cannot_be_blank(policy_id):
    with pytest.raises(ValueError, match="policy_id.*nonblank"):
        replace(POLICY, policy_id=policy_id)


def test_policy_id_is_retained_without_trimming():
    assert replace(POLICY, policy_id=" exact_unique_v1 ").policy_id == " exact_unique_v1 "


@pytest.mark.parametrize("name", INTEGER_POLICY_FIELDS)
@pytest.mark.parametrize("value", [True, False, "12", Decimal("12"), 1.5, None])
def test_policy_limits_require_actual_integers(name, value):
    with pytest.raises(TypeError, match=f"{name}.*integer"):
        replace(POLICY, **{name: value})


@pytest.mark.parametrize("name", INTEGER_POLICY_FIELDS)
@pytest.mark.parametrize("value", [0, -1])
def test_policy_limits_must_be_positive(name, value):
    with pytest.raises(ValueError, match=f"{name}.*positive"):
        replace(POLICY, **{name: value})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"minimum_accepted_subset_rows": 1}, "at least 2"),
        ({"minimum_accepted_subset_rows": 13}, "must not exceed"),
        ({"max_ambiguity_witnesses": 1}, "at least 2"),
    ],
)
def test_policy_relational_limits(changes, message):
    with pytest.raises(ValueError, match=message):
        replace(POLICY, **changes)


@pytest.mark.parametrize("name", BOOLEAN_POLICY_FIELDS)
@pytest.mark.parametrize("value", [0, 1, "true", None, ()])
def test_policy_semantics_require_actual_booleans(name, value):
    with pytest.raises(TypeError, match=f"{name}.*boolean"):
        replace(POLICY, **{name: value})


def test_correspondence_enums_have_exact_members_values_and_no_aliases():
    assert {member.name: member.value for member in CorrespondenceStatus} == {
        "UNIQUE_EXACT": "unique_exact",
        "AMBIGUOUS": "ambiguous",
        "NO_EXACT_SUBSET": "no_exact_subset",
        "SINGLETON_ONLY": "singleton_only",
        "BOUND_EXCEEDED": "bound_exceeded",
        "NOT_ELIGIBLE": "not_eligible",
    }
    assert {member.name: member.value for member in CorrespondenceReason} == {
        "BOTH_SIDES_MULTIPLE": "both_sides_multiple",
        "MISSING_OPPOSITE_SIDE": "missing_opposite_side",
        "CANDIDATE_ROW_LIMIT": "candidate_row_limit",
        "RUN_BUDGET_EXHAUSTED": "run_budget_exhausted",
    }
    for enum in (CorrespondenceStatus, CorrespondenceReason):
        assert len(enum.__members__) == len(enum)
        assert isinstance(next(iter(enum)), str)


@pytest.mark.parametrize("anchor_source", list(Source))
@pytest.mark.parametrize(
    ("amounts", "amount"),
    [(("100", "200"), "300"), (("150", "-50"), "100"), (("150", "-150"), "0")],
)
def test_solution_retains_exact_original_rows_in_both_directions(anchor_source, amounts, amount):
    solution = _solution(anchor_source, amounts, amount)
    assert solution.anchor_source is anchor_source
    assert solution.candidate_source is (Source.B if anchor_source is Source.A else Source.A)
    assert solution.anchor.amount == solution.amount == Decimal(amount)
    assert solution.candidate_row_count == 2
    assert solution.is_multirow_solution
    assert solution.anchor is (solution.rows_a if anchor_source is Source.A else solution.rows_b)[0]
    assert solution.candidate_rows is (
        solution.rows_b if anchor_source is Source.A else solution.rows_a
    )
    assert not hasattr(solution, "__dict__")
    with pytest.raises(FrozenInstanceError):
        solution.amount = Decimal("0")


def test_singleton_witness_is_allowed_and_uses_documented_accessor_tie():
    solution = _solution(amounts=("300",))
    assert not solution.is_multirow_solution
    assert solution.candidate_row_count == 1
    assert solution.anchor is solution.rows_a[0]
    assert solution.anchor_source is Source.A
    assert solution.candidate_source is Source.B


def test_solution_preserves_input_tuple_and_raw_evidence_identity():
    original = _solution()
    solution = SubsetSolution(original.rows_a, original.rows_b, original.amount)
    assert solution.rows_a is original.rows_a
    assert solution.rows_b is original.rows_b
    for row, expected in zip(solution.rows_b, original.rows_b, strict=True):
        assert row is expected
        assert row.raw_fields is expected.raw_fields


def test_solution_sums_are_exact_under_hostile_decimal_context():
    rows_a = (_record(Source.A, 2, "0.0001"),)
    rows_b = (
        _record(Source.B, 3, "1E+1000"),
        _record(Source.B, 4, "-1E+1000"),
        _record(Source.B, 5, "0.0001"),
    )
    with localcontext() as context:
        context.prec = 1
        context.Emin = -2
        context.Emax = 2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        solution = SubsetSolution(rows_a, rows_b, Decimal("0.0001"))
        finding = _finding(_analysis(solution))
        assert solution.amount == finding.amount_a == finding.amount_b == Decimal("0.0001")
        assert finding.delta == Decimal("0")
        assert not any(context.flags.values())


@pytest.mark.parametrize("side", ["rows_a", "rows_b"])
@pytest.mark.parametrize("container", [None, [], ["row"], (None,)])
def test_solution_requires_record_tuples(side, container):
    with pytest.raises(TypeError, match=f"{side}.*tuple.*SourceRecord"):
        replace(_solution(), **{side: container})


@pytest.mark.parametrize("side", ["rows_a", "rows_b"])
def test_solution_requires_both_sides_nonempty(side):
    with pytest.raises(ValueError, match="both sides"):
        replace(_solution(), **{side: ()})


def test_solution_rejects_both_sides_multiple():
    solution = _solution()
    with pytest.raises(ValueError, match="one anchor"):
        replace(solution, rows_a=(_record(Source.A, 1, "100"), _record(Source.A, 2, "200")))


@pytest.mark.parametrize("side", ["rows_a", "rows_b"])
def test_solution_rejects_wrong_source(side):
    source = Source.B if side == "rows_a" else Source.A
    with pytest.raises(ValueError, match=f"{side}.*only File"):
        replace(_solution(), **{side: (_record(source, 1, "300"),)})


@pytest.mark.parametrize("anchor_source", list(Source))
@pytest.mark.parametrize("duplicate", [False, True])
def test_solution_rejects_unsorted_or_repeated_candidate_ordinals(anchor_source, duplicate):
    solution = _solution(anchor_source)
    side = "rows_b" if anchor_source is Source.A else "rows_a"
    candidates = solution.candidate_rows
    invalid = (candidates[0], candidates[0]) if duplicate else tuple(reversed(candidates))
    with pytest.raises(ValueError, match="ordered.*duplicates"):
        replace(solution, **{side: invalid})


def test_solution_rejects_distinct_objects_with_the_same_source_ordinal():
    solution = _solution()
    with pytest.raises(ValueError, match="duplicates"):
        replace(
            solution,
            rows_b=(solution.rows_b[0], replace(solution.rows_b[0], amount=Decimal("200"))),
        )


@pytest.mark.parametrize("amount", [None, True, "300", 300, 0.1])
def test_solution_amount_requires_decimal(amount):
    with pytest.raises(TypeError, match="amount.*Decimal"):
        replace(_solution(), amount=amount)


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_solution_amount_must_be_finite(amount):
    with pytest.raises(ValueError, match="amount.*finite"):
        replace(_solution(), amount=Decimal(amount))


@pytest.mark.parametrize("side", ["rows_a", "rows_b"])
def test_solution_rejects_amount_disagreement_on_either_side(side):
    solution = _solution()
    rows = getattr(solution, side)
    with pytest.raises(ValueError, match=f"{side} amounts.*exactly"):
        replace(solution, **{side: (replace(rows[0], amount=Decimal("99")), *rows[1:])})


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (CorrespondenceStatus.AMBIGUOUS, None),
        (CorrespondenceStatus.NO_EXACT_SUBSET, None),
        (CorrespondenceStatus.SINGLETON_ONLY, None),
        (CorrespondenceStatus.BOUND_EXCEEDED, CorrespondenceReason.CANDIDATE_ROW_LIMIT),
        (CorrespondenceStatus.BOUND_EXCEEDED, CorrespondenceReason.RUN_BUDGET_EXHAUSTED),
        (CorrespondenceStatus.NOT_ELIGIBLE, CorrespondenceReason.BOTH_SIDES_MULTIPLE),
        (CorrespondenceStatus.NOT_ELIGIBLE, CorrespondenceReason.MISSING_OPPOSITE_SIDE),
    ],
)
def test_analysis_valid_unresolved_shapes(status, reason):
    analysis = _unresolved(status, reason)
    assert analysis.status is status
    assert analysis.reason is reason
    assert analysis.accepted_solution is None
    assert not hasattr(analysis, "__dict__")
    with pytest.raises(FrozenInstanceError):
        analysis.status = CorrespondenceStatus.UNIQUE_EXACT


def test_unique_analysis_is_frozen_and_retains_accepted_solution_identity():
    solution = _solution()
    analysis = _analysis(solution)
    assert analysis.accepted_solution is solution
    assert analysis.policy is POLICY
    assert analysis.search_complete
    assert analysis.ambiguity_witnesses == ()
    with pytest.raises(FrozenInstanceError):
        analysis.accepted_solution = None


@pytest.mark.parametrize("solution", [None, _solution(amounts=("300",))])
def test_unique_analysis_requires_an_accepted_multirow_solution(solution):
    with pytest.raises(ValueError, match="accepted multirow"):
        _analysis(accepted_solution=solution)


@pytest.mark.parametrize(
    "status", [s for s in CorrespondenceStatus if s is not CorrespondenceStatus.UNIQUE_EXACT]
)
def test_unresolved_analysis_rejects_accepted_solution(status):
    with pytest.raises(ValueError, match="unresolved statuses"):
        _analysis(status=status)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_canonical_ambiguous_analysis_requires_exactly_two_witnesses(count):
    analysis = _unresolved(CorrespondenceStatus.AMBIGUOUS)
    witnesses = analysis.ambiguity_witnesses[:count]
    if count == 3:
        extra = SubsetSolution(
            analysis.unassigned_rows_a,
            (_record(Source.B, 6, "100"), _record(Source.B, 7, "200")),
            Decimal("300"),
        )
        witnesses += (extra,)
    with pytest.raises(ValueError, match="AMBIGUOUS requires"):
        replace(analysis, ambiguity_witnesses=witnesses)


@pytest.mark.parametrize("shape", ["empty", "multirow", "two"])
def test_singleton_analysis_requires_exactly_one_singleton_witness(shape):
    analysis = _unresolved(CorrespondenceStatus.SINGLETON_ONLY)
    witnesses = {
        "empty": (),
        "multirow": (_solution(),),
        "two": analysis.ambiguity_witnesses + (_solution(),),
    }[shape]
    with pytest.raises(ValueError, match="SINGLETON_ONLY requires"):
        replace(analysis, ambiguity_witnesses=witnesses)


@pytest.mark.parametrize("complete", [False, True])
def test_ambiguity_can_end_early_with_full_planned_reservation(complete):
    analysis = _unresolved(CorrespondenceStatus.AMBIGUOUS, search_complete=complete)
    assert analysis.reserved_combinations == analysis.planned_combinations == 7
    assert analysis.examined_combinations == (7 if complete else 2)
    assert analysis.search_complete is complete


@pytest.mark.parametrize("invalid", ["reversed", "duplicate"])
def test_ambiguity_witnesses_are_ordered_and_physically_distinct(invalid):
    analysis = _unresolved(CorrespondenceStatus.AMBIGUOUS)
    witnesses = analysis.ambiguity_witnesses
    invalid_witnesses = (
        tuple(reversed(witnesses)) if invalid == "reversed" else (witnesses[0], witnesses[0])
    )
    with pytest.raises(ValueError, match="ordered and physically distinct"):
        replace(analysis, ambiguity_witnesses=invalid_witnesses)


@pytest.mark.parametrize(
    "status",
    [
        CorrespondenceStatus.UNIQUE_EXACT,
        CorrespondenceStatus.NO_EXACT_SUBSET,
        CorrespondenceStatus.SINGLETON_ONLY,
    ],
)
def test_exhaustive_outcomes_require_complete_search(status):
    analysis = _analysis() if status is CorrespondenceStatus.UNIQUE_EXACT else _unresolved(status)
    with pytest.raises(ValueError, match="complete search"):
        replace(analysis, search_complete=False)


@pytest.mark.parametrize(
    "status", [CorrespondenceStatus.BOUND_EXCEEDED, CorrespondenceStatus.NOT_ELIGIBLE]
)
def test_unsearched_outcomes_reject_complete_search(status):
    reason = (
        CorrespondenceReason.CANDIDATE_ROW_LIMIT
        if status is CorrespondenceStatus.BOUND_EXCEEDED
        else CorrespondenceReason.BOTH_SIDES_MULTIPLE
    )
    with pytest.raises(ValueError, match="unsearched statuses"):
        replace(_unresolved(status, reason), search_complete=True)


@pytest.mark.parametrize("status", list(CorrespondenceStatus))
@pytest.mark.parametrize("reason", [None, *CorrespondenceReason])
def test_analysis_status_reason_contract(status, reason):
    allowed = {
        CorrespondenceStatus.BOUND_EXCEEDED: {
            CorrespondenceReason.CANDIDATE_ROW_LIMIT,
            CorrespondenceReason.RUN_BUDGET_EXHAUSTED,
        },
        CorrespondenceStatus.NOT_ELIGIBLE: {
            CorrespondenceReason.BOTH_SIDES_MULTIPLE,
            CorrespondenceReason.MISSING_OPPOSITE_SIDE,
        },
    }.get(status, {None})
    base_reason = next(iter(allowed))
    analysis = (
        _analysis()
        if status is CorrespondenceStatus.UNIQUE_EXACT
        else _unresolved(status, base_reason)
    )
    if reason in allowed:
        assert replace(analysis, reason=reason).reason is reason
    else:
        with pytest.raises(ValueError, match="reason.*incompatible"):
            replace(analysis, reason=reason)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("policy", "exact_unique_v1", "policy.*OneToManyPolicy"),
        ("status", "unique_exact", "status.*CorrespondenceStatus"),
        ("reason", "candidate_row_limit", "reason.*CorrespondenceReason"),
        ("accepted_solution", {}, "accepted_solution.*SubsetSolution"),
        ("ambiguity_witnesses", [], "ambiguity_witnesses.*tuple"),
        ("ambiguity_witnesses", (None,), "ambiguity_witnesses.*SubsetSolution"),
        ("search_complete", 1, "search_complete.*boolean"),
        ("search_complete", None, "search_complete.*boolean"),
    ],
)
def test_analysis_strict_types(name, value, message):
    with pytest.raises(TypeError, match=message):
        replace(_analysis(), **{name: value})


@pytest.mark.parametrize("name", COUNTERS)
@pytest.mark.parametrize("value", [True, False, "3", Decimal("3"), 0.5, None])
def test_analysis_counters_require_actual_integers(name, value):
    with pytest.raises(TypeError, match=f"{name}.*integer"):
        replace(_analysis(), **{name: value})


@pytest.mark.parametrize("name", COUNTERS)
def test_analysis_counters_cannot_be_negative(name):
    with pytest.raises(ValueError, match=f"{name}.*nonnegative"):
        replace(_analysis(), **{name: -1})


def test_analysis_cannot_examine_more_than_planned():
    with pytest.raises(ValueError, match="examined_combinations.*planned_combinations"):
        replace(_analysis(), examined_combinations=4)


@pytest.mark.parametrize("side", ["unassigned_rows_a", "unassigned_rows_b"])
@pytest.mark.parametrize("invalid", ["list", "wrong_source", "reversed", "duplicate", "not_record"])
def test_unassigned_rows_require_sorted_source_owned_record_tuples(side, invalid):
    source = Source.A if side.endswith("a") else Source.B
    rows = (_record(source, 3, "100"), _record(source, 4, "200"))
    values = {
        "list": list(rows),
        "wrong_source": (_record(Source.B if source is Source.A else Source.A, 3, "100"),),
        "reversed": tuple(reversed(rows)),
        "duplicate": (rows[0], rows[0]),
        "not_record": (None,),
    }
    with pytest.raises(TypeError if invalid in ("list", "not_record") else ValueError):
        replace(_analysis(), **{side: values[invalid]})


@pytest.mark.parametrize(
    "status",
    [
        CorrespondenceStatus.UNIQUE_EXACT,
        CorrespondenceStatus.NO_EXACT_SUBSET,
        CorrespondenceStatus.BOUND_EXCEEDED,
        CorrespondenceStatus.NOT_ELIGIBLE,
    ],
)
def test_nonwitness_statuses_reject_witnesses(status):
    reasons = {
        CorrespondenceStatus.BOUND_EXCEEDED: CorrespondenceReason.CANDIDATE_ROW_LIMIT,
        CorrespondenceStatus.NOT_ELIGIBLE: CorrespondenceReason.BOTH_SIDES_MULTIPLE,
    }
    analysis = (
        _analysis()
        if status is CorrespondenceStatus.UNIQUE_EXACT
        else _unresolved(status, reasons.get(status))
    )
    with pytest.raises(ValueError, match="must not contain ambiguity witnesses"):
        replace(analysis, ambiguity_witnesses=(_solution(),))


def test_finding_legacy_positions_defaults_and_keyword_only_analysis():
    original = _finding()
    positional = ReconciliationFinding(
        original.category,
        original.key,
        original.rows_a,
        original.rows_b,
        original.amount_a,
        original.amount_b,
    )
    assert original == positional == replace(positional, correspondence_analysis=None)
    assert positional.correspondence_analysis is None
    assert not positional.has_inferred_correspondence
    assert positional.inferred_solution is positional.correspondence_status is None
    assert next(
        f for f in fields(ReconciliationFinding) if f.name == "correspondence_analysis"
    ).kw_only
    with pytest.raises(TypeError):
        ReconciliationFinding(
            original.category,
            original.key,
            original.rows_a,
            original.rows_b,
            original.amount_a,
            original.amount_b,
            _analysis(),
        )


@pytest.mark.parametrize("anchor_source", list(Source))
@pytest.mark.parametrize("residual", [False, True])
def test_finding_accepts_full_or_residual_partition_in_both_directions(anchor_source, residual):
    solution = _solution(anchor_source)
    candidate_source = solution.candidate_source
    unassigned = (_record(candidate_source, 5, "50"),) if residual else ()
    analysis = _analysis(
        solution,
        unassigned_rows_a=unassigned if candidate_source is Source.A else (),
        unassigned_rows_b=unassigned if candidate_source is Source.B else (),
    )
    finding = _finding(analysis)
    assert finding.correspondence_analysis is analysis
    assert finding.has_inferred_correspondence
    assert finding.inferred_solution is solution
    assert finding.correspondence_status is CorrespondenceStatus.UNIQUE_EXACT
    assert finding.category is (
        FindingCategory.AMOUNT_MISMATCH if residual else FindingCategory.EXACT_MATCH
    )
    assert finding.delta == Decimal(
        "50" if residual and anchor_source is Source.B else "-50" if residual else "0"
    )
    assert finding.is_exception
    assert solution.amount == Decimal("300")
    with pytest.raises(FrozenInstanceError):
        finding.correspondence_analysis = None


def test_inference_does_not_override_existing_secondary_review():
    analysis = _analysis()
    comparison = FieldComparison(
        ComparisonFieldMapping("department", "cost_center"),
        ("Sales",),
        ("Marketing",),
        FieldComparisonStatus.MISMATCH,
    )
    finding = _finding(analysis, field_comparisons=(comparison,))
    assert finding.category is FindingCategory.EXACT_MATCH
    assert finding.has_inferred_correspondence
    assert finding.is_exception


@pytest.mark.parametrize("reference", ["selected", "unassigned", "witness"])
def test_finding_rejects_equal_replacement_objects_even_when_dataclass_equal(reference):
    if reference == "witness":
        analysis = _unresolved(CorrespondenceStatus.AMBIGUOUS)
    else:
        solution = _solution()
        analysis = _analysis(solution, unassigned_rows_b=(_record(Source.B, 5, "50"),))
    finding = _finding(analysis)
    if reference == "selected":
        original = analysis.accepted_solution.rows_b[0]
    elif reference == "unassigned":
        original = analysis.unassigned_rows_b[0]
    else:
        original = analysis.ambiguity_witnesses[0].rows_b[0]
    replacement = replace(original, raw_fields={"evidence": "different"})
    assert replacement == original and replacement is not original
    new_parent = tuple(replacement if row is original else row for row in finding.rows_b)
    with pytest.raises(ValueError, match="object identity"):
        replace(finding, rows_b=new_parent)


@pytest.mark.parametrize("invalid", ["overlap", "omitted", "foreign"])
def test_finding_rejects_invalid_accepted_partition(invalid):
    solution = _solution()
    residual = _record(Source.B, 5, "50")
    finding = _finding(_analysis(solution, unassigned_rows_b=(residual,)))
    rows = {
        "overlap": (solution.rows_b[0], residual),
        "omitted": (),
        "foreign": (_record(Source.B, 6, "50"),),
    }[invalid]
    analysis = replace(finding.correspondence_analysis, unassigned_rows_b=rows)
    with pytest.raises(
        ValueError, match="object identity" if invalid == "foreign" else "partition"
    ):
        replace(finding, correspondence_analysis=analysis)


def test_accepted_anchor_must_be_sole_complete_parent_row():
    solution = _solution()
    analysis = _analysis(solution, unassigned_rows_a=(_record(Source.A, 5, "50"),))
    with pytest.raises(ValueError, match="sole row"):
        _finding(analysis)


@pytest.mark.parametrize("limit", ["minimum", "maximum", "parent"])
def test_finding_enforces_accepted_policy_cardinality(limit):
    solution = _solution()
    if limit == "minimum":
        policy = replace(POLICY, minimum_accepted_subset_rows=3)
        analysis = _analysis(solution, policy=policy)
    elif limit == "maximum":
        solution = _solution(amounts=("100", "100", "100"))
        analysis = _analysis(solution, policy=replace(POLICY, max_candidate_rows=2))
    else:
        analysis = _analysis(
            solution,
            policy=replace(POLICY, max_candidate_rows=2),
            unassigned_rows_b=(_record(Source.B, 5, "50"),),
        )
    with pytest.raises(ValueError, match="cardinality.*policy"):
        _finding(analysis)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (CorrespondenceStatus.AMBIGUOUS, None),
        (CorrespondenceStatus.NO_EXACT_SUBSET, None),
        (CorrespondenceStatus.SINGLETON_ONLY, None),
        (CorrespondenceStatus.BOUND_EXCEEDED, CorrespondenceReason.RUN_BUDGET_EXHAUSTED),
        (CorrespondenceStatus.NOT_ELIGIBLE, CorrespondenceReason.BOTH_SIDES_MULTIPLE),
    ],
)
def test_unresolved_finding_keeps_every_parent_row_unassigned(status, reason):
    analysis = _unresolved(status, reason)
    finding = _finding(analysis)
    assert finding.rows_a == analysis.unassigned_rows_a
    assert finding.rows_b == analysis.unassigned_rows_b
    assert not finding.has_inferred_correspondence
    assert finding.inferred_solution is None
    assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert finding.is_exception
    with pytest.raises(ValueError, match="partition"):
        replace(
            finding,
            correspondence_analysis=replace(
                analysis, unassigned_rows_b=analysis.unassigned_rows_b[:-1]
            ),
        )


def test_alternative_witnesses_may_overlap_without_removing_parent_rows():
    analysis = _unresolved(CorrespondenceStatus.AMBIGUOUS)
    finding = _finding(analysis)
    first, second = analysis.ambiguity_witnesses
    assert first.rows_a[0] is second.rows_a[0] is finding.rows_a[0]
    assert first.rows_b[-1] is second.rows_b[-1] is finding.rows_b[-1]
    assert len(finding.rows_b) == 3
    assert finding.amount_a == Decimal("300")
    assert finding.amount_b == Decimal("400")
    assert finding.delta == Decimal("-100")


@pytest.mark.parametrize("anchor_source", list(Source))
def test_singleton_and_multirow_witnesses_are_alternatives_in_both_directions(anchor_source):
    solution = _solution(anchor_source)
    candidate_source = solution.candidate_source
    singleton_row = _record(candidate_source, 1, "300")
    singleton = SubsetSolution(
        solution.rows_a if anchor_source is Source.A else (singleton_row,),
        (singleton_row,) if anchor_source is Source.A else solution.rows_b,
        Decimal("300"),
    )
    candidates = (singleton_row, *solution.candidate_rows)
    analysis = _analysis(
        status=CorrespondenceStatus.AMBIGUOUS,
        accepted_solution=None,
        unassigned_rows_a=solution.rows_a if anchor_source is Source.A else candidates,
        unassigned_rows_b=candidates if anchor_source is Source.A else solution.rows_b,
        ambiguity_witnesses=(singleton, solution),
        planned_combinations=7,
        examined_combinations=4,
        reserved_combinations=7,
        search_complete=False,
    )
    finding = _finding(analysis)
    assert finding.correspondence_analysis.ambiguity_witnesses == (singleton, solution)
    assert len(finding.rows_a if anchor_source is Source.A else finding.rows_b) == 1
    assert not finding.has_inferred_correspondence


def test_finding_rejects_foreign_witness_without_treating_it_as_an_allocation():
    analysis = _unresolved(CorrespondenceStatus.AMBIGUOUS)
    finding = _finding(analysis)
    foreign = _record(Source.B, 6, "200")
    first, second = analysis.ambiguity_witnesses
    changed = replace(second, rows_b=(second.rows_b[0], foreign))
    analysis = replace(analysis, ambiguity_witnesses=(first, changed))
    with pytest.raises(ValueError, match="object identity"):
        replace(finding, correspondence_analysis=analysis)


@pytest.mark.parametrize("side", ["rows_a", "rows_b"])
@pytest.mark.parametrize("invalid", ["list", "duplicate", "wrong_source"])
def test_analysis_finding_requires_valid_complete_parent_rows(side, invalid):
    finding = _finding(_analysis())
    rows = getattr(finding, side)
    values = {
        "list": list(rows),
        "duplicate": (rows[0], rows[0]),
        "wrong_source": (_record(Source.B if side == "rows_a" else Source.A, 1, "300"),),
    }
    with pytest.raises(TypeError if invalid == "list" else ValueError):
        replace(finding, **{side: values[invalid]})


@pytest.mark.parametrize("residual", [False, True])
@pytest.mark.parametrize("category", list(FindingCategory))
def test_accepted_finding_requires_correct_financial_category(residual, category):
    analysis = _analysis(unassigned_rows_b=(_record(Source.B, 5, "50"),) if residual else ())
    expected = FindingCategory.AMOUNT_MISMATCH if residual else FindingCategory.EXACT_MATCH
    if category is expected:
        assert _finding(analysis, category=category).category is category
    else:
        with pytest.raises(ValueError, match="category.*incompatible"):
            _finding(analysis, category=category)


@pytest.mark.parametrize(
    "category", [c for c in FindingCategory if c is not FindingCategory.DUPLICATE_AMBIGUOUS]
)
def test_unresolved_finding_requires_duplicate_ambiguous(category):
    with pytest.raises(ValueError, match="category.*incompatible"):
        _finding(_unresolved(CorrespondenceStatus.NO_EXACT_SUBSET), category=category)


@pytest.mark.parametrize("side", ["amount_a", "amount_b"])
def test_finding_analysis_requires_full_parent_amounts(side):
    with pytest.raises(ValueError, match="complete parent row totals"):
        _finding(_analysis(), **{side: Decimal("0")})


@pytest.mark.parametrize("side", ["amount_a", "amount_b"])
@pytest.mark.parametrize("amount", [None, Decimal("NaN"), Decimal("Infinity")])
def test_analysis_finding_parent_totals_must_be_finite_decimals(side, amount):
    with pytest.raises(TypeError if amount is None else ValueError):
        _finding(_analysis(), **{side: amount})


def test_finding_rejects_wrong_analysis_type():
    with pytest.raises(TypeError, match="correspondence_analysis.*CorrespondenceAnalysis"):
        _finding(correspondence_analysis={})


def test_result_legacy_positions_defaults_and_keyword_only_policy():
    finding = _finding()
    normalization = KeyNormalizationConfig((KeyNormalizationRules(casefold=True),))
    result = ReconciliationResult(
        Decimal("300"),
        Decimal("300"),
        (finding,),
        Decimal("0.01"),
        ReconciliationMode.GROUPED_BY_KEY,
        normalization,
    )
    assert result == replace(result, one_to_many_policy=None)
    assert result.one_to_many_policy is None
    assert result.correspondence_analysis_findings == result.inferred_correspondence_findings == ()
    assert next(f for f in fields(ReconciliationResult) if f.name == "one_to_many_policy").kw_only
    with pytest.raises(TypeError):
        ReconciliationResult(
            Decimal("300"),
            Decimal("300"),
            (finding,),
            Decimal("0.01"),
            ReconciliationMode.GROUPED_BY_KEY,
            normalization,
            POLICY,
        )


def test_result_requires_policy_when_any_finding_has_analysis():
    ordinary = _finding()
    inferred = _finding(_analysis())
    with pytest.raises(ValueError, match="canonical one_to_many_policy"):
        ReconciliationResult(
            Decimal("600"),
            Decimal("600"),
            (ordinary, inferred),
            mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
        )


def test_result_checks_every_analysis_policy_by_value():
    inferred = _finding(_analysis())
    different = _finding(_analysis(policy=replace(POLICY, policy_id="other")))
    with pytest.raises(ValueError, match="analysis policy.*equal"):
        ReconciliationResult(
            Decimal("600"),
            Decimal("600"),
            (inferred, different),
            mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
            one_to_many_policy=POLICY,
        )
    equivalent = replace(POLICY)
    assert equivalent == POLICY and equivalent is not POLICY
    result = ReconciliationResult(
        Decimal("300"),
        Decimal("300"),
        (inferred,),
        mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
        one_to_many_policy=equivalent,
    )
    assert result.one_to_many_policy is equivalent


@pytest.mark.parametrize("findings", [(), (_finding(),)])
def test_result_policy_can_be_retained_without_analysis(findings):
    result = ReconciliationResult(
        Decimal("0"),
        Decimal("0"),
        findings,
        mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
        one_to_many_policy=POLICY,
    )
    assert result.one_to_many_policy is POLICY
    assert result.correspondence_analysis_findings == result.inferred_correspondence_findings == ()
    with pytest.raises(FrozenInstanceError):
        result.one_to_many_policy = None


def test_result_rejects_wrong_policy_type():
    with pytest.raises(TypeError, match="one_to_many_policy.*OneToManyPolicy"):
        ReconciliationResult(Decimal("0"), Decimal("0"), (), one_to_many_policy="exact_unique_v1")


def test_result_analysis_subsets_are_ordered_derived_and_retain_finding_identity():
    first = _finding(_analysis(unassigned_rows_b=(_record(Source.B, 5, "50"),)))
    ordinary = _finding()
    ambiguous = _finding(_unresolved(CorrespondenceStatus.AMBIGUOUS))
    exact = _finding(_analysis())
    findings = (first, ordinary, ambiguous, exact)
    result = ReconciliationResult(
        Decimal("1200"),
        Decimal("1350"),
        findings,
        mode=ReconciliationMode.BOUNDED_ONE_TO_MANY,
        one_to_many_policy=POLICY,
    )
    assert result.findings is findings
    assert result.correspondence_analysis_findings == (first, ambiguous, exact)
    assert result.inferred_correspondence_findings == (first, exact)
    assert all(
        row is expected
        for row, expected in zip(
            result.inferred_correspondence_findings, (first, exact), strict=True
        )
    )
    assert {f.name for f in fields(ReconciliationResult)}.isdisjoint(
        {"correspondence_analysis_findings", "inferred_correspondence_findings"}
    )
    assert result.control_difference == result.finding_delta_sum == Decimal("-150")
    assert result.exceptions == (first, ambiguous, exact)
    assert result.tolerated_findings == ()
    assert result.tolerated_delta_total == Decimal("0")


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("comparisons", [False, True])
def test_current_engine_behavior_and_defaults_remain_unchanged(mode, normalized, comparisons):
    fields = (ComparisonFieldMapping("department", "cost_center"),) if comparisons else ()
    normalization = (
        KeyNormalizationConfig((KeyNormalizationRules(casefold=True),)) if normalized else None
    )
    rows_a = (_record(Source.A, 2, "300", "INV", {"department": "Sales"}),)
    rows_b = (
        _record(Source.B, 3, "100", "inv" if normalized else "INV", {"cost_center": "Sales"}),
        _record(Source.B, 4, "200.01", "inv" if normalized else "INV", {"cost_center": "Sales"}),
    )
    result = reconcile(
        rows_a,
        rows_b,
        mode=mode,
        amount_tolerance=Decimal("0.01"),
        key_normalization=normalization,
        comparison_fields=fields,
    )
    category = (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.WITHIN_TOLERANCE
    )
    finding = result.findings[0]
    assert result.one_to_many_policy is None
    assert finding.correspondence_analysis is None
    assert finding.category is category
    assert (
        finding.delta == result.control_difference == result.finding_delta_sum == Decimal("-0.01")
    )
    assert result.exceptions == ((finding,) if mode is ReconciliationMode.UNIQUE else ())
    assert result.tolerated_findings == (
        (finding,) if mode is ReconciliationMode.GROUPED_BY_KEY else ()
    )
    assert result.tolerated_delta_total == Decimal(
        "-0.01" if mode is ReconciliationMode.GROUPED_BY_KEY else "0"
    )
    if comparisons:
        assert finding.field_comparisons[0].status is (
            FieldComparisonStatus.NOT_COMPARABLE
            if mode is ReconciliationMode.UNIQUE
            else FieldComparisonStatus.MATCH
        )
    expected = ReconciliationResult(
        result.total_a,
        result.total_b,
        result.findings,
        result.amount_tolerance,
        result.mode,
        result.key_normalization,
        comparison_fields=fields,
    )
    assert result == expected == replace(expected, one_to_many_policy=None)


def test_legacy_profile_loads_and_identity_and_csv_bytes_are_unchanged():
    mapping = ColumnMapping((("id", "ref"),), "amount", "gross")
    assert configuration_id(b"a", b"b", mapping, name_a="a.csv", name_b="b.csv") == (
        "21571386499163214f24658dc0194953102a80a80ae21faf91fd98d6ad4b1f34"
    )
    expected_profile = b"""{
  "format": "tallydiff-mapping-profile",
  "version": 4,
  "key_pairs": [
    {
      "file_a": "id",
      "file_b": "ref"
    }
  ],
  "amount_columns": {
    "file_a": "amount",
    "file_b": "gross"
  },
  "amount_tolerance": "0",
  "reconciliation_mode": "unique",
  "key_normalization": [
    {
      "casefold": false,
      "collapse_whitespace": false,
      "remove_punctuation": false,
      "strip_leading_zeros": false
    }
  ],
  "comparison_fields": []
}
"""
    expected_current = expected_profile.replace(b'"version": 4', b'"version": 5').replace(
        b'"comparison_fields": []\n',
        b'"comparison_fields": [],\n  "one_to_many_policy": null\n',
    )
    assert export_mapping_profile(mapping) == expected_current
    assert load_mapping_profile(expected_profile).mapping == mapping
    result = reconcile((_record(Source.A, 2, "300"),), (_record(Source.B, 3, "301"),))
    expected_csv = (
        b"Category,Matching key,File A amount,File B amount,Delta (A - B),"
        b"File A records,File B records\r\n"
        b"Amount mismatch,INV,300,301,-1,2,3\r\n"
    )
    assert export_exceptions_csv(result) == expected_csv
