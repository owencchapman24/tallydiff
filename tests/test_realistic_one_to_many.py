"""Independent mixed-format acceptance, CI stress and concentrated operation bounds."""

import csv
from decimal import Decimal
from io import StringIO

import pytest

import tallydiff.engine as engine
from scripts.subset_benchmark import (
    ingest_subset_pair,
    subset_mapping,
    subset_normalization,
    verify_subset_result,
)
from scripts.subset_oracle import (
    RECIPES,
    SubsetScenario,
    calibration_pair,
    generate_subset_pair,
    pair_from_scenarios,
)
from tallydiff import (
    IngestionError,
    ReconciliationMode,
    Source,
    export_exceptions_csv,
    ingest_csv,
    ingest_xlsx,
    reconcile,
)

TOLERANCE = Decimal("0.01")


def run(pair, inputs=None, mode=ReconciliationMode.BOUNDED_ONE_TO_MANY):
    truth = pair.expected(TOLERANCE, mode=mode.value)
    a, b = inputs or ingest_subset_pair(pair)
    config = subset_normalization(pair.normalized)
    result = reconcile(
        a,
        b,
        mode=mode,
        amount_tolerance=TOLERANCE,
        comparison_fields=subset_mapping().comparison_fields,
        key_normalization=config,
    )
    report = export_exceptions_csv(result)
    verify_subset_result(a, b, result, report, truth, key_normalization=config)
    return result, report, truth


@pytest.mark.parametrize(
    "formats", (("csv", "csv"), ("csv", "xlsx"), ("xlsx", "csv"), ("xlsx", "xlsx"))
)
@pytest.mark.parametrize("normalized", (False, True))
def test_all_scenarios_across_formats_with_identical_evidence(formats, normalized, xlsx_bytes):
    pair = generate_subset_pair(257, seed=2026, shuffle_seed=77, normalized=normalized)
    mapping = subset_mapping()
    inputs = []
    for kind, text, source, keys, column, secondary in (
        (formats[0], pair.csv_a, Source.A, mapping.keys_a, mapping.amount_a, mapping.comparisons_a),
        (formats[1], pair.csv_b, Source.B, mapping.keys_b, mapping.amount_b, mapping.comparisons_b),
    ):
        options = dict(
            source=source, key_columns=keys, amount_column=column, comparison_columns=secondary
        )
        if kind == "csv":
            records = ingest_csv(text, **options)
        else:
            # Decimal amounts are literal text, preserving subcent and huge precision.
            data = xlsx_bytes({"Ledger": list(csv.reader(StringIO(text, newline="")))})
            records = ingest_xlsx(data, worksheet="Ledger", **options)
        inputs.append(records)
    result, report, truth = run(pair, tuple(inputs))
    csv_result, csv_report, _ = run(pair)
    assert report == csv_report
    assert result.findings == csv_result.findings
    assert truth.summary()["analyzed"] > 150
    assert {scenario.kind for scenario in pair.scenarios} == {kind for kind, _, _ in RECIPES} | {
        "candidate_12",
        "candidate_13",
    }


def test_ci_20000_groups_full_independent_truth_and_export():
    pair = generate_subset_pair(20_000, seed=42, shuffle_seed=77)
    result, report, truth = run(pair)
    assert len(result.findings) == 20_000
    summary = truth.summary()
    assert summary["analyzed"] == 13795
    assert summary["inferred"] == 7516
    assert (summary["planned"], summary["reserved"], summary["examined"]) == (
        377034,
        377034,
        370624,
    )
    assert report


@pytest.mark.parametrize("early", (False, True))
def test_completed_million_and_early_ambiguity_reserve_identical_budget(early):
    _, _, truth = run(calibration_pair(early_ambiguity=early))
    summary = truth.summary()
    assert summary["reserved"] == 1_000_000
    assert summary["examined"] == (696 if early else 1_000_000)
    assert summary["statuses"]["bound_exceeded"] == 3
    assert summary["planned"] == 1_000_006


@pytest.mark.parametrize("mode", (ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY))
def test_feature_disabled_has_no_policy_analysis_or_subset_calls(mode, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled modes called subset search")

    monkeypatch.setattr(engine, "analyze_bounded_one_to_many", forbidden)
    result, _, truth = run(generate_subset_pair(1000), mode=mode)
    assert result.one_to_many_policy is None
    assert not result.correspondence_analysis_findings
    assert truth.summary()["examined"] == truth.summary()["reserved"] == 0


@pytest.mark.parametrize("count", (12, 13))
@pytest.mark.parametrize("unique", (False, True))
def test_candidate_limit_exhaustive_unique_and_no_exact(count, unique):
    candidates = tuple(1 << index for index in range(count))
    pair = pair_from_scenarios(
        (SubsetScenario(("V", "1"), "cap", (sum(candidates) if unique else -1,), candidates),)
    )
    _, _, truth = run(pair)
    analysis = truth.groups[0].analysis
    assert (
        analysis.examined_combinations
        == analysis.reserved_combinations
        == (4095 if count == 12 else 0)
    )
    assert (
        analysis.status == ("unique_exact" if unique else "no_exact_subset")
        if count == 12
        else analysis.reason == "candidate_row_limit"
    )


@pytest.mark.parametrize("bad", ("=1+2", "#DIV/0!"))
@pytest.mark.parametrize("column", (0, 2, 5))
def test_selected_xlsx_formula_and_error_remain_blocking(bad, column, xlsx_bytes):
    pair = generate_subset_pair(1)
    rows = list(csv.reader(StringIO(pair.csv_a)))
    rows[1][column] = bad
    data = xlsx_bytes({"Ledger": rows})
    mapping = subset_mapping()
    with pytest.raises(IngestionError):
        ingest_xlsx(
            data,
            worksheet="Ledger",
            source=Source.A,
            key_columns=mapping.keys_a,
            amount_column=mapping.amount_a,
            comparison_columns=mapping.comparisons_a,
        )
