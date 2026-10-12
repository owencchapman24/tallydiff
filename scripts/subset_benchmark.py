"""Product execution and exhaustive checks against independent subset construction.

Expected classifications, searches, row references and CSV are never derived here.
"""

import time
from collections import Counter
from dataclasses import fields
from decimal import Decimal, localcontext

from scripts.subset_oracle import (
    HEADERS_A,
    HEADERS_B,
    MAPPINGS,
    POLICY,
    amount,
    calibration_pair,
    expected_subset_export,
    generate_subset_pair,
)
from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    Source,
    export_exceptions_csv,
    ingest_csv,
    reconcile,
)


def subset_mapping():
    return ColumnMapping(
        tuple(zip(HEADERS_A[:2], HEADERS_B[:2], strict=True)),
        HEADERS_A[-1],
        HEADERS_B[-1],
        comparison_fields=tuple(ComparisonFieldMapping(*names) for names in MAPPINGS),
    )


def subset_normalization(enabled):
    return (
        KeyNormalizationConfig(
            (
                KeyNormalizationRules(
                    casefold=True, collapse_whitespace=True, remove_punctuation=True
                ),
                KeyNormalizationRules(strip_leading_zeros=True),
            )
        )
        if enabled
        else None
    )


def ingest_subset_pair(pair):
    mapping = subset_mapping()
    return tuple(
        ingest_csv(
            text,
            source=source,
            key_columns=keys,
            amount_column=column,
            comparison_columns=comparisons,
        )
        for text, source, keys, column, comparisons in (
            (pair.csv_a, Source.A, mapping.keys_a, mapping.amount_a, mapping.comparisons_a),
            (pair.csv_b, Source.B, mapping.keys_b, mapping.amount_b, mapping.comparisons_b),
        )
    )


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _policy(policy):
    return (
        None
        if policy is None
        else {field.name: getattr(policy, field.name) for field in fields(policy)}
    )


def verify_subset_result(a, b, result, report, truth, *, key_normalization=None):
    """Check all financial parents, original objects, nested evidence and exact bytes."""
    summary = truth.summary()
    _require(result.mode.value == truth.mode, "mode differs")
    _require(result.amount_tolerance == truth.tolerance, "tolerance differs")
    _require(result.key_normalization is key_normalization, "normalization configuration differs")
    _require(
        _policy(result.one_to_many_policy)
        == (POLICY if truth.mode == "bounded_one_to_many" else None),
        "policy differs",
    )
    _require(
        tuple((field.file_a, field.file_b) for field in result.comparison_fields) == MAPPINGS,
        "mapping differs",
    )
    _require((len(a), len(b)) == (summary["records_a"], summary["records_b"]), "row counts differ")
    _require(len(result.findings) == len(truth.groups), "group count differs")
    _require(
        Counter(finding.category.value for finding in result.findings)
        == Counter(summary["categories"]),
        "category counts differ",
    )
    _require(
        (result.total_a, result.total_b, result.control_difference, result.tolerated_delta_total)
        == tuple(
            Decimal(summary[name])
            for name in ("total_a", "total_b", "control_difference", "tolerated_delta_total")
        ),
        "totals differ",
    )
    _require(
        tuple(finding.key for finding in result.exceptions) == truth.exception_keys,
        "exception order differs",
    )
    _require(
        tuple(finding.key for finding in result.tolerated_findings) == truth.tolerated_keys,
        "tolerated order differs",
    )
    _require(
        tuple(finding.key for finding in result.correspondence_analysis_findings)
        == tuple(group.key for group in truth.groups if group.analysis),
        "analyzed findings differ",
    )
    _require(
        tuple(finding.key for finding in result.inferred_correspondence_findings)
        == tuple(group.key for group in truth.groups if group.inferred),
        "inferred findings differ",
    )
    _require(
        tuple(finding.key for finding in result.secondary_mismatch_findings)
        == tuple(group.key for group in truth.groups if group.has_secondary_mismatch),
        "secondary mismatches differ",
    )
    originals = {}
    for observed, attribute in ((a, "records_a"), (b, "records_b")):
        expected_rows = sorted(
            (row for group in truth.groups for row in getattr(group, attribute)),
            key=lambda row: row.source_row,
        )
        for row, expected in zip(observed, expected_rows, strict=True):
            identity = (row.source.value, row.source_row)
            _require(
                identity == (expected.source, expected.source_row)
                and row.key == expected.key
                and row.amount == amount(expected.units)
                and dict(row.raw_fields) == expected.raw_fields,
                "ingested evidence differs",
            )
            _require(identity not in originals, "duplicate physical identity")
            originals[identity] = row
    accounted = Counter()

    def check_rows(observed, expected, *, parent=False):
        _require(len(observed) == len(expected), "physical row count differs")
        for row, plan in zip(observed, expected, strict=True):
            identity = (row.source.value, row.source_row)
            _require(
                identity == (plan.source, plan.source_row)
                and row.key == plan.key
                and row.amount == amount(plan.units)
                and dict(row.raw_fields) == plan.raw_fields,
                "physical row evidence/order differs",
            )
            _require(row is originals.get(identity), "original source object replaced")
            if parent:
                accounted[identity] += 1

    def check_solution(observed, expected):
        _require((observed is None) == (expected is None), "solution presence differs")
        if expected is not None:
            _require(observed.amount == amount(expected.units), "solution amount differs")
            check_rows(observed.rows_a, expected.rows_a)
            check_rows(observed.rows_b, expected.rows_b)

    status_counts = Counter()
    for finding, group in zip(result.findings, truth.groups, strict=True):
        _require(
            (
                finding.key,
                finding.category.value,
                finding.amount_a,
                finding.amount_b,
                finding.delta,
                finding.has_inferred_correspondence,
                finding.has_secondary_mismatch,
                finding.is_exception,
            )
            == (
                group.key,
                group.category,
                amount(group.units_a),
                amount(group.units_b),
                amount(group.delta_units),
                group.inferred,
                group.has_secondary_mismatch,
                group.is_exception,
            ),
            "finding differs",
        )
        check_rows(finding.rows_a, group.records_a, parent=True)
        check_rows(finding.rows_b, group.records_b, parent=True)
        _require(
            len(finding.field_comparisons) == len(group.field_comparisons),
            "secondary count differs",
        )
        for actual, expected in zip(
            finding.field_comparisons, group.field_comparisons, strict=True
        ):
            _require(
                (
                    (actual.mapping.file_a, actual.mapping.file_b),
                    actual.values_a,
                    actual.values_b,
                    actual.status.value,
                )
                == (expected.mapping, expected.values_a, expected.values_b, expected.status),
                "secondary evidence differs",
            )
        actual, expected = finding.correspondence_analysis, group.analysis
        _require((actual is None) == (expected is None), "analysis presence differs")
        if expected is None:
            _require(
                finding.correspondence_status is None and finding.inferred_solution is None,
                "unexpected derived analysis",
            )
            continue
        status_counts[actual.status.value] += 1
        _require(_policy(actual.policy) == POLICY, "analysis policy differs")
        _require(
            (
                actual.status.value,
                actual.reason.value if actual.reason else None,
                actual.planned_combinations,
                actual.examined_combinations,
                actual.reserved_combinations,
                actual.search_complete,
            )
            == (
                expected.status,
                expected.reason,
                expected.planned_combinations,
                expected.examined_combinations,
                expected.reserved_combinations,
                expected.search_complete,
            ),
            "analysis status/counters differ",
        )
        _require(
            finding.correspondence_status is actual.status
            and finding.inferred_solution is actual.accepted_solution,
            "derived analysis differs",
        )
        check_solution(actual.accepted_solution, expected.accepted_solution)
        check_rows(actual.unassigned_rows_a, expected.unassigned_rows_a)
        check_rows(actual.unassigned_rows_b, expected.unassigned_rows_b)
        _require(
            len(actual.ambiguity_witnesses) == len(expected.ambiguity_witnesses),
            "witness count differs",
        )
        for witness, plan in zip(
            actual.ambiguity_witnesses, expected.ambiguity_witnesses, strict=True
        ):
            check_solution(witness, plan)
    _require(
        accounted == Counter({identity: 1 for identity in originals}),
        "physical parent accounting differs",
    )
    _require(status_counts == Counter(summary["statuses"]), "status counts differ")
    with localcontext() as context:
        context.prec = 100 + len(str(len(truth.groups)))
        _require(
            result.control_difference
            == result.finding_delta_sum
            == sum((finding.delta for finding in result.findings), Decimal(0)),
            "financial accounting differs",
        )
        _require(
            result.control_difference
            == sum((finding.delta for finding in result.exceptions), Decimal(0))
            + result.tolerated_delta_total,
            "review accounting differs",
        )
    _require(report == expected_subset_export(truth), "complete CSV bytes differ")
    _require(export_exceptions_csv(result) == report, "repeat CSV differs")


def measure_subset(groups, seed, tolerance, *, mode, workload):
    """Untraced phases; disabled baseline uses the exact same ingested objects."""
    started = time.perf_counter()
    normalized = workload == "subset_normalization"
    pair = (
        calibration_pair(early_ambiguity=workload == "subset_early_ambiguity")
        if workload in ("subset_exhaustive", "subset_early_ambiguity")
        else generate_subset_pair(groups, seed=seed, normalized=normalized)
    )
    generated = time.perf_counter()
    a, b = ingest_subset_pair(pair)
    ingested = time.perf_counter()
    config = subset_normalization(normalized)
    mappings = subset_mapping().comparison_fields
    result = reconcile(
        a,
        b,
        amount_tolerance=tolerance,
        mode=mode,
        key_normalization=config,
        comparison_fields=mappings,
    )
    reconciled = time.perf_counter()
    report = export_exceptions_csv(result)
    exported = time.perf_counter()
    truth = pair.expected(tolerance, mode=mode.value)
    verify_subset_result(a, b, result, report, truth, key_normalization=config)
    summary = truth.summary()
    finished = time.perf_counter()
    times = (
        generated - started,
        ingested - generated,
        reconciled - ingested,
        exported - reconciled,
        finished - exported,
        finished - started,
    )
    # Release large bounded result/evidence before measuring the same-input baseline.
    del result, report, truth
    if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        baseline_started = time.perf_counter()
        result = reconcile(
            a,
            b,
            amount_tolerance=tolerance,
            mode=ReconciliationMode.UNIQUE,
            key_normalization=config,
            comparison_fields=mappings,
        )
        baseline_reconciled = time.perf_counter()
        report = export_exceptions_csv(result)
        baseline_exported = time.perf_counter()
        truth = pair.expected(tolerance, mode="unique")
        verify_subset_result(a, b, result, report, truth, key_normalization=config)
        summary["same_input_unique_baseline"] = {
            "reconcile_s": round(baseline_reconciled - baseline_started, 3),
            "export_s": round(baseline_exported - baseline_reconciled, 3),
            "verify_s": round(time.perf_counter() - baseline_exported, 3),
            "summary": truth.summary(),
        }
    return times, summary
