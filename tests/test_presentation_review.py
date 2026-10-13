"""Workflow presentation preserves canonical identity and reconciliation evidence."""

from dataclasses import replace
from decimal import Decimal
from itertools import combinations
from types import MappingProxyType

import pytest

import tallydiff
from tallydiff import (
    ComparisonFieldMapping,
    CorrespondenceStatus,
    FindingCategory,
    FindingReview,
    ReconciliationMode,
    ReviewDisposition,
    ReviewSessionError,
    Source,
    SourceRecord,
    export_exceptions_csv,
    reconcile,
)
from tallydiff.presentation import (
    REVIEW_DISPOSITION_LABELS,
    filter_review_dispositions,
    finding_rows,
    review_exceptions,
    review_finding_rows,
)

DISPOSITIONS = tuple(ReviewDisposition)
DISPOSITION_SUBSETS = tuple(
    subset for size in range(4) for subset in combinations(DISPOSITIONS, size)
)
COMPARISON = ComparisonFieldMapping("department", "cost_center")


def _record(source, row, key, amount="10", department="Sales"):
    field = "department" if source is Source.A else "cost_center"
    return SourceRecord(source, row, key, Decimal(amount), {field: department, "memo": "raw"})


def _reviews(findings):
    return {
        finding.key: FindingReview(
            finding.key, DISPOSITIONS[index % 3], f"Private note {index}: =1+1"
        )
        for index, finding in enumerate(findings)
    }


@pytest.fixture
def result():
    keys = (
        ("exact-secondary",),
        ("tolerance-secondary",),
        ("mismatch",),
        ("accepted-exact",),
        ("accepted-tolerance",),
    )
    a = [_record(Source.A, row, key) for row, key in enumerate(keys, 2)]
    b = [
        _record(Source.B, row, key, amount, department)
        for row, (key, amount, department) in enumerate(
            zip(
                keys,
                ("10", "10.01", "12", "10", "10.01"),
                ("Other", "Other", "Sales", "Sales", "Sales"),
                strict=True,
            ),
            2,
        )
    ]
    a.extend(
        (
            _record(Source.A, 10, ("a-only",)),
            _record(Source.A, 11, ("duplicate",), "5"),
            _record(Source.A, 12, ("duplicate",), "5"),
        )
    )
    b.extend((_record(Source.B, 10, ("b-only",)), _record(Source.B, 11, ("duplicate",))))
    return reconcile(a, b, amount_tolerance=Decimal("0.01"), comparison_fields=(COMPARISON,))


def test_locked_human_labels_and_order_are_nonroot_presentation_api():
    assert list(REVIEW_DISPOSITION_LABELS.items()) == [
        (ReviewDisposition.UNREVIEWED, "Unreviewed"),
        (ReviewDisposition.NEEDS_FOLLOW_UP, "Needs follow-up"),
        (ReviewDisposition.REVIEW_COMPLETE, "Review complete"),
    ]
    for name in (
        "REVIEW_DISPOSITION_LABELS",
        "review_finding_rows",
        "filter_review_dispositions",
    ):
        assert name not in tallydiff.__all__
        assert not hasattr(tallydiff, name)


def test_review_rows_append_only_disposition_to_unchanged_evidence_rows(result):
    findings = tuple(reversed(result.exceptions))
    reviews = _reviews(result.exceptions)
    original = finding_rows(findings)
    rows = review_finding_rows(findings, reviews)
    assert len(rows) == len(original)
    for finding, base, row in zip(findings, original, rows, strict=True):
        assert list(row) == [*base, "Review disposition"]
        assert {key: value for key, value in row.items() if key in base} == base
        assert (
            row["Review disposition"] == REVIEW_DISPOSITION_LABELS[reviews[finding.key].disposition]
        )
        assert "Review note" not in row and "Analyst note" not in row
        assert reviews[finding.key].note not in row.values()
    assert finding_rows(findings) == original


@pytest.mark.parametrize("disposition", DISPOSITIONS)
@pytest.mark.parametrize("category", tuple(FindingCategory))
def test_every_primary_category_can_have_every_workflow_disposition(result, disposition, category):
    finding = next(finding for finding in result.exceptions if finding.category is category)
    reviews = {finding.key: FindingReview(finding.key, disposition)}
    assert (
        review_finding_rows((finding,), reviews)[0]["Review disposition"]
        == (REVIEW_DISPOSITION_LABELS[disposition])
    )
    assert filter_review_dispositions((finding,), reviews, (disposition,)) == (finding,)
    assert finding.is_exception


@pytest.mark.parametrize("selected", DISPOSITION_SUBSETS)
@pytest.mark.parametrize("as_set", (False, True))
def test_disposition_filter_accepts_each_combination_and_preserves_incoming_order(
    result, selected, as_set
):
    findings = tuple(reversed(result.exceptions))
    reviews = _reviews(result.exceptions)
    supplied = frozenset(selected) if as_set else selected
    visible = filter_review_dispositions(findings, reviews, supplied)
    expected = tuple(f for f in findings if reviews[f.key].disposition in selected)
    assert visible == expected
    assert all(actual is original for actual, original in zip(visible, expected, strict=True))
    assert isinstance(visible, tuple)


@pytest.mark.parametrize(
    "sort_order", ("matching_key", "absolute_delta_desc", "absolute_delta_asc")
)
def test_disposition_filter_follows_financial_filter_and_sort_without_resorting(result, sort_order):
    reviews = _reviews(result.exceptions)
    financial = review_exceptions(
        tuple(reversed(result.findings)),
        query="secondary",
        categories=(FindingCategory.EXACT_MATCH, FindingCategory.WITHIN_TOLERANCE),
        minimum_abs_delta=Decimal("0"),
        secondary_mismatches_only=True,
        sort_order=sort_order,
    )
    selected = (ReviewDisposition.NEEDS_FOLLOW_UP, ReviewDisposition.REVIEW_COMPLETE)
    expected = tuple(f for f in financial if reviews[f.key].disposition in selected)
    assert filter_review_dispositions(financial, reviews, selected) == expected
    assert result.exceptions == review_exceptions(result.findings)


@pytest.mark.parametrize("selected", DISPOSITION_SUBSETS)
def test_disposition_filter_materializes_iterator_once(result, selected):
    findings = tuple(reversed(result.exceptions))
    reviews = _reviews(result.exceptions)
    expected = filter_review_dispositions(findings, reviews, selected)
    assert filter_review_dispositions(findings, reviews, iter(selected)) == expected


@pytest.mark.parametrize("string_first", (False, True))
def test_iterator_type_validation_precedes_enum_and_string_deduplication(result, string_first):
    enum = ReviewDisposition.UNREVIEWED
    choices = (enum.value, enum) if string_first else (enum, enum.value)
    with pytest.raises(TypeError, match="ReviewDisposition enum members"):
        filter_review_dispositions(result.exceptions, _reviews(result.exceptions), iter(choices))


@pytest.mark.parametrize("mode", tuple(ReconciliationMode))
def test_workflow_helpers_support_every_mode_without_affecting_financial_membership(mode):
    a = (_record(Source.A, 2, ("INV",), "10"),)
    b = (_record(Source.B, 2, ("INV",), "11"),)
    result = reconcile(a, b, mode=mode)
    finding = result.exceptions[0]
    reviews = {finding.key: FindingReview(finding.key, ReviewDisposition.REVIEW_COMPLETE)}
    assert filter_review_dispositions(result.exceptions, reviews, DISPOSITIONS) == (finding,)
    assert review_finding_rows(result.exceptions, reviews)[0]["Category"] == "Amount mismatch"
    assert result.exceptions == (finding,)


@pytest.mark.parametrize(
    "amounts,status",
    [
        (("4", "6"), CorrespondenceStatus.UNIQUE_EXACT),
        (("4", "6", "2"), CorrespondenceStatus.UNIQUE_EXACT),
        (("4", "6", "3", "7"), CorrespondenceStatus.AMBIGUOUS),
        (("10", "3"), CorrespondenceStatus.SINGLETON_ONLY),
        (("4", "5"), CorrespondenceStatus.NO_EXACT_SUBSET),
    ],
)
def test_correspondence_cells_and_exception_truth_survive_completed_review(amounts, status):
    a = (_record(Source.A, 2, ("INV",)), _record(Source.A, 3, ("ordinary",)))
    b = tuple(_record(Source.B, row, ("INV",), amount) for row, amount in enumerate(amounts, 2))
    result = reconcile(a, b, mode=ReconciliationMode.BOUNDED_ONE_TO_MANY)
    finding = next(f for f in result.exceptions if f.key == ("INV",))
    assert finding.correspondence_status is status
    reviews = {
        f.key: FindingReview(f.key, ReviewDisposition.REVIEW_COMPLETE) for f in result.exceptions
    }
    original = finding_rows(result.exceptions)
    rows = review_finding_rows(result.exceptions, reviews)
    assert [{key: row[key] for key in base} for row, base in zip(rows, original, strict=True)] == (
        original
    )
    assert filter_review_dispositions(
        result.exceptions, reviews, (ReviewDisposition.REVIEW_COMPLETE,)
    ) == (result.exceptions)
    assert finding.is_exception
    if amounts == ("4", "6"):
        assert finding.category is FindingCategory.EXACT_MATCH
        assert finding.has_inferred_correspondence


def test_colliding_display_keys_are_distinct_canonical_review_identities():
    keys = (("a / b", "c"), ("a", "b / c"))
    a = tuple(_record(Source.A, row, key) for row, key in enumerate(keys, 2))
    findings = reconcile(a, ()).exceptions
    reviews = {
        keys[0]: FindingReview(keys[0], ReviewDisposition.REVIEW_COMPLETE, "first"),
        keys[1]: FindingReview(keys[1], ReviewDisposition.NEEDS_FOLLOW_UP, "second"),
    }
    rows = review_finding_rows(findings, reviews)
    assert rows[0]["Matching key"] == rows[1]["Matching key"] == "a / b / c"
    assert [row["Review disposition"] for row in rows] == ["Needs follow-up", "Review complete"]
    for key, review in reviews.items():
        visible = filter_review_dispositions(findings, reviews, (review.disposition,))
        assert tuple(f.key for f in visible) == (key,)


@pytest.mark.parametrize("helper", (review_finding_rows, filter_review_dispositions))
def test_view_may_use_full_mapping_with_unselected_entries(result, helper):
    reviews = MappingProxyType(_reviews(result.exceptions))
    subset = result.exceptions[2:4]
    args = (subset, reviews) if helper is review_finding_rows else (subset, reviews, DISPOSITIONS)
    output = helper(*args)
    assert len(output) == len(subset)


@pytest.mark.parametrize("helper", (review_finding_rows, filter_review_dispositions))
def test_empty_view_and_empty_population_are_valid(helper):
    args = ((), {}) if helper is review_finding_rows else ((), {}, ())
    assert helper(*args) == ([] if helper is review_finding_rows else ())


@pytest.mark.parametrize("helper", (review_finding_rows, filter_review_dispositions))
@pytest.mark.parametrize(
    "corruption", ("missing", "wrong-key", "string-key", "wrong-value", "not-mapping")
)
def test_missing_or_wrong_population_fails_without_default_annotations(result, helper, corruption):
    findings = result.exceptions[:1]
    reviews = _reviews(findings)
    if corruption == "missing":
        reviews.clear()
    elif corruption == "wrong-key":
        reviews[findings[0].key] = FindingReview(("unrelated",), ReviewDisposition.UNREVIEWED)
    elif corruption == "string-key":
        reviews = {" / ".join(findings[0].key): reviews[findings[0].key]}
    elif corruption == "wrong-value":
        reviews[findings[0].key] = "unreviewed"
    else:
        reviews = list(reviews.items())
    error = ReviewSessionError if corruption in ("missing", "wrong-key") else TypeError
    args = (findings, reviews) if helper is review_finding_rows else (findings, reviews, ())
    with pytest.raises(error):
        helper(*args)


@pytest.mark.parametrize("helper", (review_finding_rows, filter_review_dispositions))
@pytest.mark.parametrize("field,value", (("disposition", "unreviewed"), ("note", "bad\x00")))
def test_corrupted_extra_annotation_is_rejected_even_outside_visible_view(
    result, helper, field, value
):
    reviews = _reviews(result.exceptions)
    corrupt = replace(reviews[result.exceptions[-1].key])
    object.__setattr__(corrupt, field, value)
    reviews[result.exceptions[-1].key] = corrupt
    subset = result.exceptions[:1]
    args = (subset, reviews) if helper is review_finding_rows else (subset, reviews, DISPOSITIONS)
    with pytest.raises(ReviewSessionError, match="invariants"):
        helper(*args)


@pytest.mark.parametrize(
    "bad",
    ("unreviewed", "needs_follow_up", "review_complete", None, 0, True, FindingCategory.A_ONLY),
)
def test_disposition_filter_requires_actual_enum_values(result, bad):
    with pytest.raises(TypeError, match="ReviewDisposition enum members"):
        filter_review_dispositions(result.exceptions, _reviews(result.exceptions), (bad,))


@pytest.mark.parametrize("helper", (review_finding_rows, filter_review_dispositions))
def test_finding_type_validation_is_explicit(helper):
    args = (
        (("not a finding",), {}) if helper is review_finding_rows else (("not a finding",), {}, ())
    )
    with pytest.raises(TypeError, match="ReconciliationFinding"):
        helper(*args)


def test_presentation_does_not_mutate_reviews_result_evidence_or_old_export(result):
    findings = result.findings
    exceptions = result.exceptions
    tolerated = result.tolerated_findings
    rows = tuple(
        (f.rows_a, f.rows_b, f.field_comparisons, f.correspondence_analysis) for f in findings
    )
    raw = tuple(
        tuple(record.raw_fields.items()) for f in findings for record in (*f.rows_a, *f.rows_b)
    )
    reviews = _reviews(exceptions)
    annotations = tuple(reviews.items())
    before_csv = export_exceptions_csv(result)
    before_totals = (
        result.control_difference,
        result.finding_delta_sum,
        result.tolerated_delta_total,
    )
    for selected in DISPOSITION_SUBSETS:
        visible = filter_review_dispositions(tuple(reversed(exceptions)), reviews, selected)
        review_finding_rows(visible, reviews)
    assert result.findings is findings
    assert result.exceptions == exceptions
    assert result.tolerated_findings == tolerated
    assert rows == tuple(
        (f.rows_a, f.rows_b, f.field_comparisons, f.correspondence_analysis) for f in findings
    )
    assert raw == tuple(
        tuple(record.raw_fields.items()) for f in findings for record in (*f.rows_a, *f.rows_b)
    )
    assert tuple(reviews.items()) == annotations
    assert all(reviews[key] is review for key, review in annotations)
    assert before_totals == (
        result.control_difference,
        result.finding_delta_sum,
        result.tolerated_delta_total,
    )
    assert export_exceptions_csv(result) == before_csv
    assert all("Review disposition" not in row for row in finding_rows(tolerated))
    accepted_exact = tuple(
        f for f in findings if not f.is_exception and f.category is FindingCategory.EXACT_MATCH
    )
    assert all("Review disposition" not in row for row in finding_rows(accepted_exact))
