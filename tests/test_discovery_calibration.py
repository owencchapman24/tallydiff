"""Validate empirical discovery tooling without freezing a mapping ranking."""

import ast
import inspect
import json
import tomllib
from dataclasses import asdict
from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from pathlib import Path

import pytest

from scripts import discovery_benchmark, discovery_calibration
from tallydiff.discovery import (
    compare_amount_columns,
    compare_headers,
    compare_key_mapping,
    discover_csv,
    discover_xlsx,
)
from tallydiff.models import Source

REQUIRED_CATEGORIES = {
    "canonical",
    "identical_headers",
    "unrelated_key_headers",
    "lexical_decoy",
    "numeric_identifier_decoy",
    "monetary_decoy",
    "control_total_trap",
    "partial_overlap",
    "composite_duplicates",
    "invalid_candidates",
    "reordered_columns",
    "cross_format",
}


@pytest.fixture(scope="module")
def corpus():
    return discovery_calibration.build_corpus()


@pytest.fixture(scope="module")
def calibration_report():
    return discovery_calibration.build_report()


def _snapshots(case):
    result = []
    for suffix, source in (("a", Source.A), ("b", Source.B)):
        data = getattr(case, f"data_{suffix}")
        file_format = getattr(case, f"format_{suffix}")
        if file_format == "csv":
            result.append(discover_csv(data, source=source))
        else:
            assert file_format == "xlsx"
            result.append(
                discover_xlsx(data, source=source, worksheet=getattr(case, f"worksheet_{suffix}"))
            )
    return tuple(result)


def _case(report, name):
    return next(case for case in report["cases"] if case["name"] == name)


def _keys(case, pairs):
    expected = tuple(tuple(pair) for pair in pairs)
    return next(
        candidate
        for candidate in case["key_candidates"]
        if tuple(tuple(pair) for pair in candidate["key_pairs"]) == expected
    )


def _amounts(case, column_a, column_b):
    return next(
        candidate
        for candidate in case["amount_candidates"]
        if (candidate["column_a"], candidate["column_b"]) == (column_a, column_b)
    )


def _json_facts(value):
    return json.loads(json.dumps(asdict(value), default=str))


def _walk(value):
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list | tuple):
        for nested in value:
            yield from _walk(nested)


def test_calibration_corpus_is_deterministic_unique_and_covers_required_scenarios(corpus):
    names = [case.name for case in corpus]
    assert len(names) == len(set(names))
    assert all(name.strip() for name in names)
    assert REQUIRED_CATEGORIES <= {category for case in corpus for category in case.categories}
    assert corpus == discovery_calibration.build_corpus()
    assert all(not hasattr(case, "__dict__") for case in corpus)


def test_intended_mappings_reference_real_columns_and_do_not_select_first_columns(corpus):
    for case in corpus:
        snapshot_a, snapshot_b = _snapshots(case)
        headers_a = tuple(column.name for column in snapshot_a.profile.columns)
        headers_b = tuple(column.name for column in snapshot_b.profile.columns)
        assert case.intended_key_mappings
        assert case.intended_amount_pairs
        for mapping in case.intended_key_mappings:
            assert 1 <= len(mapping) <= 2
            assert len({pair[0] for pair in mapping}) == len(mapping)
            assert len({pair[1] for pair in mapping}) == len(mapping)
            for column_a, column_b in mapping:
                assert column_a in headers_a
                assert column_b in headers_b
            assert mapping != ((headers_a[0], headers_b[0]),)
        for column_a, column_b in case.intended_amount_pairs:
            assert column_a in headers_a
            assert column_b in headers_b
            assert (column_a, column_b) != (headers_a[0], headers_b[0])


def test_single_candidate_enumeration_is_the_complete_ordered_cartesian_product(corpus):
    for source_case in corpus:
        case = discovery_calibration.evaluate_case(source_case)
        expected = [(a, b) for a in case["headers_a"] for b in case["headers_b"]]
        single_keys = [
            candidate for candidate in case["key_candidates"] if candidate["cardinality"] == 1
        ]
        assert [tuple(candidate["key_pairs"][0]) for candidate in single_keys] == expected
        assert [
            (candidate["column_a"], candidate["column_b"])
            for candidate in case["amount_candidates"]
        ] == expected
        assert all(candidate["cardinality"] in (1, 2) for candidate in case["key_candidates"])
        composites = [
            candidate for candidate in case["key_candidates"] if candidate["cardinality"] == 2
        ]
        assert len(composites) <= 18
        assert case["key_candidates"] == [*single_keys, *composites]
        for candidate in composites:
            assert len(candidate["key_pairs"]) == 2
            assert len({pair[0] for pair in candidate["key_pairs"]}) == 2
            assert len({pair[1] for pair in candidate["key_pairs"]}) == 2
        assert case == discovery_calibration.evaluate_case(source_case)


def test_report_candidate_facts_are_the_unmodified_discovery_diagnostics(
    corpus, calibration_report
):
    for source_case in corpus:
        snapshot_a, snapshot_b = _snapshots(source_case)
        case = _case(calibration_report, source_case.name)
        for candidate in case["key_candidates"]:
            pairs = tuple(tuple(pair) for pair in candidate["key_pairs"])
            facts = compare_key_mapping(snapshot_a, snapshot_b, key_pairs=pairs)
            assert candidate["facts"] == _json_facts(facts)
            assert candidate["headers"] == [_json_facts(compare_headers(*pair)) for pair in pairs]
            intended = pairs in source_case.intended_key_mappings
            assert candidate["is_intended"] is intended
        for candidate in case["amount_candidates"]:
            column_a, column_b = candidate["column_a"], candidate["column_b"]
            facts = compare_amount_columns(
                snapshot_a, snapshot_b, column_a=column_a, column_b=column_b
            )
            assert candidate["facts"] == _json_facts(facts)
            assert candidate["header"] == _json_facts(compare_headers(column_a, column_b))
            assert candidate["is_intended"] is (
                (column_a, column_b) in source_case.intended_amount_pairs
            )


def test_report_ratios_are_exact_integer_pairs_with_explicit_zero_denominators(calibration_report):
    for case in calibration_report["cases"]:
        for candidate in case["key_candidates"]:
            facts = candidate["facts"]
            expected = {}
            for side in ("a", "b"):
                expected[f"eligible_coverage_{side}"] = (
                    facts[f"eligible_row_count_{side}"],
                    facts[f"row_count_{side}"],
                )
                expected[f"distinct_overlap_{side}"] = (
                    facts["overlap_distinct_count"],
                    facts[f"distinct_key_count_{side}"],
                )
                expected[f"row_overlap_{side}"] = (
                    facts[f"overlap_row_count_{side}"],
                    facts[f"row_count_{side}"],
                )
                expected[f"uniqueness_{side}"] = (
                    facts[f"distinct_key_count_{side}"],
                    facts[f"eligible_row_count_{side}"],
                )
            assert candidate["ratios"] == {
                name: {"numerator": numerator, "denominator": denominator}
                for name, (numerator, denominator) in expected.items()
            }
        for candidate in case["amount_candidates"]:
            facts = candidate["facts"]
            assert candidate["ratios"] == {
                f"parseability_{side}": {
                    "numerator": facts[f"parseable_count_{side}"],
                    "denominator": facts[f"row_count_{side}"],
                }
                for side in ("a", "b")
            }
    assert not any(isinstance(value, float) for value in _walk(calibration_report))
    empty = _case(calibration_report, "empty_tables")
    for group in empty["signal_leaders"].values():
        for name, leader in group.items():
            if name.startswith(
                (
                    "eligible_coverage",
                    "distinct_overlap",
                    "row_overlap",
                    "uniqueness",
                    "parseability",
                )
            ):
                assert leader["value"] is None
                assert leader["candidate_indices"] == []


def test_signal_leader_ratio_ties_are_exact_and_independent(calibration_report):
    for case in calibration_report["cases"]:
        for cardinality in (1, 2):
            candidates = case["key_candidates"]
            group = case["signal_leaders"][f"key_cardinality_{cardinality}"]
            for name in (
                "eligible_coverage_a",
                "distinct_overlap_a",
                "row_overlap_a",
                "uniqueness_a",
            ):
                eligible = {
                    index: Fraction(
                        candidate["ratios"][name]["numerator"],
                        candidate["ratios"][name]["denominator"],
                    )
                    for index, candidate in enumerate(candidates)
                    if candidate["cardinality"] == cardinality
                    and candidate["ratios"][name]["denominator"]
                }
                leader = group[name]
                expected_indices = (
                    [index for index, value in eligible.items() if value == max(eligible.values())]
                    if eligible
                    else []
                )
                assert leader["candidate_indices"] == expected_indices


def test_serialization_and_corpus_summary_are_deterministic_machine_readable_facts(
    calibration_report,
):
    serialized = discovery_calibration.report_json(calibration_report)
    assert serialized == discovery_calibration.report_json(calibration_report)
    assert serialized == discovery_calibration.report_json()
    assert json.loads(serialized) == calibration_report
    assert calibration_report["schema_version"] == 1
    assert calibration_report["case_count"] == len(calibration_report["cases"])
    assert calibration_report["summary"]
    assert all(
        type(value) is int and value >= 0 for value in calibration_report["summary"].values()
    )
    forbidden = {
        "score",
        "final_score",
        "overall_score",
        "confidence",
        "confidence_percentage",
        "recommendation",
        "best_mapping",
        "ranking",
        "production_ranking",
        "auto_selected_mapping",
        "severity",
        "warning",
    }
    for value in _walk(calibration_report):
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)


def test_canonical_headers_have_no_invented_vendor_supplier_relationship(calibration_report):
    case = _case(calibration_report, "canonical")
    vendor = _keys(case, (("Vendor ID", "Supplier"),))
    invoice = _keys(case, (("Invoice Number", "Invoice Ref"),))
    amount = _amounts(case, "Invoice Amount", "Gross Amount")
    assert vendor["headers"][0]["shared_tokens"] == []
    assert not vendor["headers"][0]["exact_normalized_match"]
    assert invoice["headers"][0]["shared_tokens"] == ["invoice"]
    assert amount["header"]["shared_tokens"] == ["amount"]
    assert vendor["facts"]["overlap_distinct_count"] > 0
    assert invoice["facts"]["overlap_distinct_count"] > 0
    assert amount["facts"]["parseable_count_a"] == amount["facts"]["row_count_a"]
    assert amount["facts"]["parseable_count_b"] == amount["facts"]["row_count_b"]


def test_composite_calibration_resolves_duplicate_first_components(calibration_report):
    case = _case(calibration_report, "composite_duplicates")
    intended = next(candidate for candidate in case["key_candidates"] if candidate["is_intended"])
    first = _keys(case, (tuple(intended["key_pairs"][0]),))
    second = _keys(case, (tuple(intended["key_pairs"][1]),))
    for side in ("a", "b"):
        assert first["facts"][f"duplicate_excess_count_{side}"] > 0
        assert second["facts"][f"duplicate_excess_count_{side}"] > 0
        assert intended["facts"][f"duplicate_excess_count_{side}"] == 0
        assert (
            intended["facts"][f"eligible_row_count_{side}"]
            == first["facts"][f"eligible_row_count_{side}"]
        )
        assert (
            intended["facts"][f"overlap_row_count_{side}"]
            == first["facts"][f"overlap_row_count_{side}"]
        )
    assert intended["facts"]["overlap_distinct_count"] > first["facts"]["overlap_distinct_count"]


def test_control_total_trap_is_a_wrong_pair_with_equal_totals(calibration_report):
    case = _case(calibration_report, "control_total_trap")
    trap = _amounts(case, "Batch Number", "Sequence Number")
    intended = _amounts(case, "Invoice Amount", "Gross Amount")
    assert not trap["is_intended"]
    assert trap["facts"]["total_a"] is not None
    assert Decimal(trap["facts"]["total_a"]) == Decimal(trap["facts"]["total_b"])
    assert Decimal(trap["facts"]["control_difference"]) == 0
    assert intended["is_intended"]
    assert Decimal(intended["facts"]["control_difference"]) != 0


def test_numeric_identifier_amount_decoy_is_fully_parseable(calibration_report):
    case = _case(calibration_report, "numeric_identifier_decoy")
    decoy = _amounts(case, "Identifier", "Reference")
    intended = _amounts(case, "Invoice Amount", "Gross Amount")
    assert not decoy["is_intended"]
    assert intended["is_intended"]
    for side in ("a", "b"):
        assert decoy["facts"][f"parseable_count_{side}"] == decoy["facts"][f"row_count_{side}"]
        assert (
            intended["facts"][f"parseable_count_{side}"] == intended["facts"][f"row_count_{side}"]
        )
        assert decoy["ratios"][f"parseability_{side}"] == intended["ratios"][f"parseability_{side}"]


def test_partial_overlap_retains_legitimate_directional_only_side_values(calibration_report):
    case = _case(calibration_report, "partial_overlap")
    intended = next(candidate for candidate in case["key_candidates"] if candidate["is_intended"])
    facts = intended["facts"]
    assert facts["overlap_distinct_count"] > 0
    assert facts["only_a_distinct_count"] > 0
    assert facts["only_b_distinct_count"] > 0
    assert facts["overlap_row_count_a"] < facts["eligible_row_count_a"]
    assert facts["overlap_row_count_b"] < facts["eligible_row_count_b"]


def test_lexical_decoy_can_have_matching_headers_without_value_overlap(calibration_report):
    case = _case(calibration_report, "lexical_decoy")
    lexical_decoys = [
        candidate
        for candidate in case["key_candidates"]
        if candidate["cardinality"] == 1
        and not candidate["is_intended"]
        and candidate["headers"][0]["exact_normalized_match"]
        and candidate["facts"]["overlap_distinct_count"] == 0
    ]
    assert lexical_decoys
    assert any(
        candidate["is_intended"] and candidate["facts"]["overlap_distinct_count"] > 0
        for candidate in case["key_candidates"]
    )


def test_calibration_report_is_independent_of_hostile_decimal_context(calibration_report):
    with localcontext() as context:
        context.prec = 2
        context.Emax = 3
        context.Emin = -3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        context.clear_flags()
        before = context.copy()
        actual = discovery_calibration.build_report()
        assert context.flags == before.flags
    assert actual == calibration_report


def test_calibration_cli_emits_json_to_stdout_only(monkeypatch, capsys):
    expected = {"case_count": 0, "cases": [], "summary": {}}
    monkeypatch.setattr(discovery_calibration, "build_report", lambda: expected)
    discovery_calibration.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected
    assert captured.err == ""


@pytest.mark.parametrize("row_count,column_count", [(0, 12), (8, 10), (12, 12)])
def test_benchmark_generation_is_deterministic_at_tiny_sizes(row_count, column_count):
    data_a, data_b = discovery_benchmark.generate_csv_pair(row_count, column_count=column_count)
    assert (data_a, data_b) == discovery_benchmark.generate_csv_pair(
        row_count, column_count=column_count
    )
    snapshot_a = discover_csv(data_a, source=Source.A)
    snapshot_b = discover_csv(data_b, source=Source.B)
    assert snapshot_a.profile.row_count == snapshot_b.profile.row_count == row_count
    assert len(snapshot_a.profile.columns) == len(snapshot_b.profile.columns) == column_count
    assert (
        tuple(column.name for column in snapshot_a.profile.columns)
        == discovery_benchmark.HEADERS_A[:column_count]
    )
    assert (
        tuple(column.name for column in snapshot_b.profile.columns)
        == discovery_benchmark.HEADERS_B[:column_count]
    )
    if row_count:
        assert any(
            column.amount_parseable_count == row_count for column in snapshot_a.profile.columns
        )
        assert any(column.amount_parseable_count == 0 for column in snapshot_a.profile.columns)


def test_tiny_benchmark_data_exercises_partial_overlap_duplicates_and_composite_keys():
    data_a, data_b = discovery_benchmark.generate_csv_pair(16)
    snapshot_a = discover_csv(data_a, source=Source.A)
    snapshot_b = discover_csv(data_b, source=Source.B)
    pairs = tuple(
        zip(discovery_benchmark.HEADERS_A[:2], discovery_benchmark.HEADERS_B[:2], strict=True)
    )
    first = compare_key_mapping(snapshot_a, snapshot_b, key_pairs=pairs[:1])
    composite = compare_key_mapping(snapshot_a, snapshot_b, key_pairs=pairs)
    assert first.duplicate_excess_count_a > 0
    assert first.duplicate_excess_count_b > 0
    assert composite.duplicate_excess_count_a == composite.duplicate_excess_count_b == 0
    assert composite.eligible_row_count_a == composite.eligible_row_count_b == 16
    assert 0 < composite.overlap_distinct_count < 16


def test_benchmark_composite_enumeration_is_bounded_deterministic_and_valid():
    candidates = discovery_benchmark.composite_candidates(
        discovery_benchmark.HEADERS_A, discovery_benchmark.HEADERS_B, limit=7
    )
    assert len(candidates) == 7
    assert len(set(candidates)) == 7
    assert candidates == discovery_benchmark.composite_candidates(
        discovery_benchmark.HEADERS_A, discovery_benchmark.HEADERS_B, limit=7
    )
    assert (
        discovery_benchmark.composite_candidates(
            discovery_benchmark.HEADERS_A, discovery_benchmark.HEADERS_B, limit=0
        )
        == ()
    )
    for mapping in candidates:
        assert len(mapping) == 2
        assert len({pair[0] for pair in mapping}) == 2
        assert len({pair[1] for pair in mapping}) == 2
        assert all(
            pair[0] in discovery_benchmark.HEADERS_A and pair[1] in discovery_benchmark.HEADERS_B
            for pair in mapping
        )


def test_tiny_benchmark_reports_separate_observational_phases_and_exact_candidate_counts():
    observed = discovery_benchmark.benchmark_case(8, column_count=10, composite_limit=5)
    assert observed["row_count_per_side"] == 8
    assert observed["column_count_per_side"] == 10
    assert observed["candidate_counts"] == {
        "single_key_pairs": 100,
        "amount_pairs": 100,
        "composite_key_pairs": 5,
    }
    phases = {"discover_csv_pair", "single_key_pairs", "amount_pairs", "composite_key_pairs"}
    assert set(observed["elapsed_seconds"]) == phases
    assert all(value >= 0 for value in observed["elapsed_seconds"].values())
    assert set(observed["python_memory"]["peak_mib"]) == phases
    assert all(value >= 0 for value in observed["python_memory"]["peak_mib"].values())
    assert set(observed["python_memory"]["incremental_peak_mib"]) == phases
    assert all(value >= 0 for value in observed["python_memory"]["incremental_peak_mib"].values())
    assert observed["python_memory"]["retained_snapshot_bytes"] >= 0
    assert observed["python_memory"]["retained_snapshot_mib"] >= 0
    assert all(type(value) is int for value in observed["checksums"].values())


def test_benchmark_default_sizes_are_documented_but_tests_run_only_tiny_sizes(monkeypatch):
    assert inspect.signature(discovery_benchmark.run_benchmark).parameters[
        "row_counts"
    ].default == (1000, 5000, 20000)
    calls = []

    def fake_case(row_count, column_count, composite_limit):
        calls.append((row_count, column_count, composite_limit))
        return {"row_count_per_side": row_count}

    monkeypatch.setattr(discovery_benchmark, "benchmark_case", fake_case)
    report = discovery_benchmark.run_benchmark((4, 8), column_count=10, composite_limit=3)
    assert calls == [(4, 10, 3), (8, 10, 3)]
    assert report["cases"] == [{"row_count_per_side": 4}, {"row_count_per_side": 8}]
    assert report["methodology"]


def test_developer_tooling_has_no_reconciliation_ui_or_network_imports():
    forbidden = (
        "streamlit",
        "tallydiff.app",
        "tallydiff.engine",
        "tallydiff.review",
        "requests",
        "urllib",
        "http",
        "socket",
    )
    for module in (discovery_calibration, discovery_benchmark):
        imports = []
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert all(
            not any(name == blocked or name.startswith(blocked + ".") for blocked in forbidden)
            for name in imports
        )
    assert not any(
        "rank" in name or "recommend" in name or "score" in name
        for name in vars(discovery_calibration)
        if not name.startswith("_")
    )


def test_new_developer_tools_preserve_existing_package_policies():
    project = Path(__file__).resolve().parents[1]
    configuration = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    source = configuration["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert "scripts/discovery_calibration.py" not in wheel
    assert "scripts/discovery_benchmark.py" not in wheel
    assert "/scripts/*.py" in source
    assert "/tests/*.py" in source
    assert configuration["project"]["version"] == "0.8.0"


def test_canonical_corpus_diagnostics_have_known_exact_counts_and_controls(calibration_report):
    case = _case(calibration_report, "canonical")
    vendor = _keys(case, (("Vendor ID", "Supplier"),))["facts"]
    assert vendor["distinct_key_count_a"] == vendor["distinct_key_count_b"] == 3
    assert vendor["duplicate_excess_count_a"] == vendor["duplicate_excess_count_b"] == 1
    assert vendor["overlap_distinct_count"] == 2
    assert vendor["overlap_row_count_a"] == vendor["overlap_row_count_b"] == 3
    composite = _keys(case, (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")))["facts"]
    for side in ("a", "b"):
        assert composite[f"row_count_{side}"] == 4
        assert composite[f"eligible_row_count_{side}"] == 4
        assert composite[f"distinct_key_count_{side}"] == 4
        assert composite[f"duplicate_excess_count_{side}"] == 0
        assert composite[f"overlap_row_count_{side}"] == 3
        assert composite[f"only_{side}_distinct_count"] == 1
    assert composite["overlap_distinct_count"] == 3
    amounts = _amounts(case, "Invoice Amount", "Gross Amount")["facts"]
    assert Decimal(amounts["total_a"]) == Decimal("2675")
    assert Decimal(amounts["total_b"]) == Decimal("2580")
    assert Decimal(amounts["control_difference"]) == Decimal("95")


def test_control_proximity_and_parseability_leaders_expose_empirical_decoys(calibration_report):
    trap_case = _case(calibration_report, "control_total_trap")
    trap = _amounts(trap_case, "Batch Number", "Sequence Number")
    intended = _amounts(trap_case, "Invoice Amount", "Gross Amount")
    assert Decimal(trap["facts"]["total_a"]) == Decimal("60")
    assert Decimal(intended["facts"]["control_difference"]) == Decimal("30")
    leaders = trap_case["signal_leaders"]["amount"]["absolute_control_difference"]
    assert trap_case["amount_candidates"].index(trap) in leaders["candidate_indices"]
    assert trap_case["amount_candidates"].index(intended) not in leaders["candidate_indices"]
    assert trap_case["analysis"]["decoy_in_control_proximity_leaders"]
    numeric = _case(calibration_report, "numeric_identifier_decoy")
    decoy = _amounts(numeric, "Identifier", "Reference")
    amount = _amounts(numeric, "Invoice Amount", "Gross Amount")
    for side in ("a", "b"):
        assert Decimal(decoy["facts"][f"total_{side}"]) == Decimal("3006")
        tied = numeric["signal_leaders"]["amount"][f"parseability_{side}"]["candidate_indices"]
        assert numeric["amount_candidates"].index(decoy) in tied
        assert numeric["amount_candidates"].index(amount) in tied
    assert Decimal(amount["facts"]["control_difference"]) == Decimal("-5")


@pytest.mark.parametrize(
    "name", ["unrelated_key_headers", "empty_tables", "punctuation_only_headers"]
)
def test_zero_shared_header_tokens_are_absent_evidence_in_corpus_analysis(calibration_report, name):
    case = _case(calibration_report, name)
    leader = case["signal_leaders"]["key_cardinality_1"]["shared_token_count"]
    assert leader["value"] == 0
    assert not case["analysis"]["shared_token_signal_present"]
    assert not case["analysis"]["intended_component_in_shared_token_leaders"]
    assert not case["analysis"]["decoy_in_shared_token_leaders"]
    if name == "punctuation_only_headers":
        assert case["analysis"]["exact_header_match_with_empty_normalization"]
        assert _keys(case, (("$", "€"),))["headers"][0]["exact_normalized_match"]


def test_positive_and_misleading_header_evidence_remains_independent(calibration_report):
    case = _case(calibration_report, "identical_headers")
    intended = _keys(case, (("Transaction ID", "Transaction ID"),))
    leader = case["signal_leaders"]["key_cardinality_1"]["shared_token_count"]
    assert leader["value"] == 2
    assert leader["candidate_indices"] == [case["key_candidates"].index(intended)]
    assert case["analysis"]["shared_token_signal_present"]
    assert case["analysis"]["intended_component_in_shared_token_leaders"]
    assert not case["analysis"]["decoy_in_shared_token_leaders"]
    lexical = _case(calibration_report, "lexical_decoy")
    wrong = _keys(lexical, (("Invoice Number", "Invoice Number"),))
    leader = lexical["signal_leaders"]["key_cardinality_1"]["shared_token_count"]
    assert leader["value"] == 2
    assert leader["candidate_indices"] == [lexical["key_candidates"].index(wrong)]
    assert not leader["intended_component_indices"]
    assert lexical["analysis"]["decoy_in_shared_token_leaders"]


def test_monetary_decoy_and_cross_format_cases_have_known_factual_traps(calibration_report):
    monetary = _case(calibration_report, "monetary_decoy")
    intended = _amounts(monetary, "Invoice Amount", "Gross Amount")
    decoy = _amounts(monetary, "Tax Amount", "Tax Amount")
    assert not decoy["is_intended"]
    assert decoy["header"]["exact_normalized_match"]
    assert decoy["ratios"] == intended["ratios"]
    assert Decimal(decoy["facts"]["total_a"]) == Decimal(decoy["facts"]["total_b"]) == Decimal("15")
    assert Decimal(intended["facts"]["control_difference"]) == Decimal("1")
    case = _case(calibration_report, "cross_format")
    facts = _keys(case, (("Identifier", "Reference"),))["facts"]
    assert case["formats"] == {"a": "csv", "b": "xlsx"}
    assert facts["distinct_key_count_a"] == 3
    assert facts["distinct_key_count_b"] == 2
    assert facts["duplicate_excess_count_b"] == 1
    assert facts["overlap_distinct_count"] == 2
    assert facts["overlap_row_count_a"] == 2
    assert facts["overlap_row_count_b"] == 3
    assert facts["only_a_distinct_count"] == 1
    assert _keys(case, (("Optional", "Optional"),))["facts"]["eligible_row_count_b"] == 0
    amounts = _amounts(case, "Invoice Amount", "Gross Amount")["facts"]
    assert Decimal(amounts["total_a"]) == Decimal(amounts["total_b"]) == Decimal("60.6")


def test_invalid_candidate_case_reports_missing_controls_without_dropping_rows(calibration_report):
    case = _case(calibration_report, "invalid_candidates")
    facts = _keys(case, (("Identifier", "Reference"),))["facts"]
    for side in ("a", "b"):
        assert facts[f"row_count_{side}"] == 3
        assert facts[f"eligible_row_count_{side}"] == 2
        assert facts[f"overlap_row_count_{side}"] == 1
    amount = _amounts(case, "Invoice Amount", "Gross Amount")["facts"]
    assert amount["parseable_count_a"] == amount["parseable_count_b"] == 2
    assert amount["blank_count_a"] == 0
    assert amount["blank_count_b"] == 1
    assert amount["total_a"] is None
    assert amount["total_b"] is None
    assert amount["control_difference"] is None


def test_calibration_ground_truth_labels_do_not_influence_candidate_facts_or_signal_leaders(corpus):
    from dataclasses import replace

    case = next(case for case in corpus if case.name == "canonical")
    relabeled = replace(
        case,
        intended_key_mappings=((("Notes", "Memo"),),),
        intended_amount_pairs=(("Vendor ID", "Supplier"),),
    )
    before = discovery_calibration.evaluate_case(case)
    after = discovery_calibration.evaluate_case(relabeled)
    for original, changed in zip(before["key_candidates"], after["key_candidates"], strict=True):
        for field in ("key_pairs", "cardinality", "headers", "facts", "ratios"):
            assert original[field] == changed[field]
    for original, changed in zip(
        before["amount_candidates"], after["amount_candidates"], strict=True
    ):
        for field in ("column_a", "column_b", "header", "facts", "ratios"):
            assert original[field] == changed[field]
    for name, signals in before["signal_leaders"].items():
        for signal, original in signals.items():
            changed = after["signal_leaders"][name][signal]
            assert original["value"] == changed["value"]
            assert original["candidate_indices"] == changed["candidate_indices"]
    assert before["key_candidates"] != after["key_candidates"]
    assert before["amount_candidates"] != after["amount_candidates"]


def test_calibration_unicode_json_is_portable_ascii_and_preserves_original_text():
    report = {"column": "账单 €", "tokens": ["账单", "€"]}
    serialized = discovery_calibration.report_json(report)
    encoded = serialized.encode("ascii")
    assert json.loads(encoded) == report
    assert serialized == discovery_calibration.report_json(report)
