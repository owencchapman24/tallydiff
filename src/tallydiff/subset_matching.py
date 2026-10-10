"""Deterministic bounded subset search, independent of reconciliation classification."""

from itertools import combinations

from tallydiff._decimal import sum_decimals
from tallydiff.models import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    CorrespondenceAnalysis,
    CorrespondenceReason,
    CorrespondenceStatus,
    OneToManyPolicy,
    Source,
    SourceRecord,
    SubsetSolution,
    _validate_correspondence_rows,
)


def analyze_bounded_one_to_many(
    rows_a: tuple[SourceRecord, ...],
    rows_b: tuple[SourceRecord, ...],
    *,
    policy: OneToManyPolicy = EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    remaining_planned_budget: int,
    run_budget_exhausted: bool = False,
) -> CorrespondenceAnalysis | None:
    """Analyze an entire parent group using exact, physically distinct subsets.

    Inputs must already be source-row ordered. Original keys and secondary fields
    are not inspected; the caller establishes the matching-key group. Ordinary
    singleton groups need no analysis. Duplicate-shaped groups always get evidence.

    Candidate-limit failures record zero planned work. Otherwise, the full planned
    cost must fit before any subset is tested, even when two solutions allow an
    early stop. The caller subtracts ``reserved_combinations`` from its remaining
    budget and makes ``run_budget_exhausted`` sticky after RUN_BUDGET_EXHAUSTED.
    This function neither changes nor retains run state.
    """
    _validate_correspondence_rows(rows_a, Source.A, "rows_a")
    _validate_correspondence_rows(rows_b, Source.B, "rows_b")
    if not isinstance(policy, OneToManyPolicy):
        raise TypeError("policy must be a OneToManyPolicy")
    if policy.minimum_accepted_subset_rows != 2:
        raise ValueError("minimum_accepted_subset_rows must be 2 for bounded one-to-many search")
    for feature in ("exact_amounts_only", "physical_row_uniqueness", "singleton_rivals_count"):
        if getattr(policy, feature) is not True:
            raise ValueError(f"{feature} must be True for bounded one-to-many search")
    if type(remaining_planned_budget) is not int:
        raise TypeError("remaining_planned_budget must be an integer")
    if remaining_planned_budget < 0:
        raise ValueError("remaining_planned_budget must be nonnegative")
    if type(run_budget_exhausted) is not bool:
        raise TypeError("run_budget_exhausted must be a boolean")
    if not rows_a and not rows_b:
        raise ValueError("a parent group must contain at least one record")
    if len(rows_a) <= 1 and len(rows_b) <= 1:
        return None

    if not rows_a or not rows_b:
        return _unresolved_analysis(
            rows_a,
            rows_b,
            policy=policy,
            status=CorrespondenceStatus.NOT_ELIGIBLE,
            reason=CorrespondenceReason.MISSING_OPPOSITE_SIDE,
        )
    if len(rows_a) > 1 and len(rows_b) > 1:
        return _unresolved_analysis(
            rows_a,
            rows_b,
            policy=policy,
            status=CorrespondenceStatus.NOT_ELIGIBLE,
            reason=CorrespondenceReason.BOTH_SIDES_MULTIPLE,
        )

    a_is_anchor = len(rows_a) == 1
    anchor = rows_a[0] if a_is_anchor else rows_b[0]
    candidates = rows_b if a_is_anchor else rows_a
    if len(candidates) > policy.max_candidate_rows:
        return _unresolved_analysis(
            rows_a,
            rows_b,
            policy=policy,
            status=CorrespondenceStatus.BOUND_EXCEEDED,
            reason=CorrespondenceReason.CANDIDATE_ROW_LIMIT,
        )
    planned = (1 << len(candidates)) - 1
    if run_budget_exhausted or planned > remaining_planned_budget:
        return _unresolved_analysis(
            rows_a,
            rows_b,
            policy=policy,
            status=CorrespondenceStatus.BOUND_EXCEEDED,
            reason=CorrespondenceReason.RUN_BUDGET_EXHAUSTED,
            planned_combinations=planned,
        )

    examined = 0
    solutions: list[SubsetSolution] = []
    for cardinality in range(1, len(candidates) + 1):
        for subset in combinations(candidates, cardinality):
            examined += 1
            if sum_decimals(row.amount for row in subset) != anchor.amount:
                continue
            solution = SubsetSolution(
                rows_a=(anchor,) if a_is_anchor else subset,
                rows_b=subset if a_is_anchor else (anchor,),
                amount=anchor.amount,
            )
            solutions.append(solution)
            if len(solutions) == 2:
                return _unresolved_analysis(
                    rows_a,
                    rows_b,
                    policy=policy,
                    status=CorrespondenceStatus.AMBIGUOUS,
                    ambiguity_witnesses=tuple(solutions),
                    planned_combinations=planned,
                    examined_combinations=examined,
                    reserved_combinations=planned,
                    search_complete=examined == planned,
                )

    if not solutions:
        return _unresolved_analysis(
            rows_a,
            rows_b,
            policy=policy,
            status=CorrespondenceStatus.NO_EXACT_SUBSET,
            planned_combinations=planned,
            examined_combinations=examined,
            reserved_combinations=planned,
            search_complete=True,
        )

    solution = solutions[0]
    selected_candidates = solution.rows_b if a_is_anchor else solution.rows_a
    if len(selected_candidates) < policy.minimum_accepted_subset_rows:
        return _unresolved_analysis(
            rows_a,
            rows_b,
            policy=policy,
            status=CorrespondenceStatus.SINGLETON_ONLY,
            ambiguity_witnesses=(solution,),
            planned_combinations=planned,
            examined_combinations=examined,
            reserved_combinations=planned,
            search_complete=True,
        )

    selected_ids = {id(row) for row in selected_candidates}
    residuals = tuple(row for row in candidates if id(row) not in selected_ids)
    return CorrespondenceAnalysis(
        policy=policy,
        status=CorrespondenceStatus.UNIQUE_EXACT,
        accepted_solution=solution,
        unassigned_rows_a=() if a_is_anchor else residuals,
        unassigned_rows_b=residuals if a_is_anchor else (),
        ambiguity_witnesses=(),
        planned_combinations=planned,
        examined_combinations=examined,
        reserved_combinations=planned,
        search_complete=True,
        reason=None,
    )


def _unresolved_analysis(
    rows_a: tuple[SourceRecord, ...],
    rows_b: tuple[SourceRecord, ...],
    *,
    policy: OneToManyPolicy,
    status: CorrespondenceStatus,
    reason: CorrespondenceReason | None = None,
    ambiguity_witnesses: tuple[SubsetSolution, ...] = (),
    planned_combinations: int = 0,
    examined_combinations: int = 0,
    reserved_combinations: int = 0,
    search_complete: bool = False,
) -> CorrespondenceAnalysis:
    return CorrespondenceAnalysis(
        policy=policy,
        status=status,
        accepted_solution=None,
        unassigned_rows_a=rows_a,
        unassigned_rows_b=rows_b,
        ambiguity_witnesses=ambiguity_witnesses,
        planned_combinations=planned_combinations,
        examined_combinations=examined_combinations,
        reserved_combinations=reserved_combinations,
        search_complete=search_complete,
        reason=reason,
    )
