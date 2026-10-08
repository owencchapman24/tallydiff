import csv
from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
from io import StringIO

import pytest

from scripts.benchmark import measure, verify_result
from scripts.synthetic_data import (
    AMOUNT_A,
    AMOUNT_B,
    HEADERS_A,
    HEADERS_B,
    KEYS_A,
    KEYS_B,
    decimal_units,
    generate_pair,
)
from tallydiff import (
    ColumnMapping,
    FindingCategory,
    ReconciliationMode,
    Source,
    export_exceptions_csv,
    export_mapping_profile,
    ingest_csv,
    ingest_xlsx,
    inspect_csv_columns,
    load_mapping_profile,
    reconcile,
)
from tallydiff.presentation import review_exceptions


def test_generator_is_reproducible_and_scenario_mix_is_known_by_construction() -> None:
    pair = generate_pair(100, seed=7)
    assert pair == generate_pair(100, seed=7)
    assert pair != generate_pair(100, seed=8)
    exact = pair.expected()
    tolerated = pair.expected(Decimal("0.01"))
    assert exact.category_counts == {
        "exact_match": 70,
        "within_tolerance": 0,
        "amount_mismatch": 18,
        "a_only": 4,
        "b_only": 4,
        "duplicate_ambiguous": 4,
    }
    assert tolerated.category_counts == {
        **exact.category_counts,
        "within_tolerance": 10,
        "amount_mismatch": 8,
    }
    assert (exact.record_count_a, exact.record_count_b) == (99, 97)
    assert len(exact.groups) == 100
    assert len(exact.exception_keys) == 30
    assert len(tolerated.exception_keys) == 20
    assert tolerated.tolerated_delta_total == Decimal("0.0050")
    # Anchor one hand-auditable recipe, independent of CSV parsing and the engine.
    assert pair.scenarios[70].amounts_a[0] - pair.scenarios[70].amounts_b[0] == 100
    assert pair.scenarios[98].amounts_a[0] + pair.scenarios[98].amounts_a[1] == 0
    assert pair.scenarios[99].amounts_a == (0, 0) and pair.scenarios[99].amounts_b == ()
    assert "Café" in pair.csv_a and '"Synthetic supplies, north office"' in pair.csv_a


@pytest.mark.parametrize("tolerance", [Decimal("0"), Decimal("0.0100")])
def test_generated_exports_match_independent_truth_end_to_end(tolerance: Decimal) -> None:
    pair = generate_pair(1_500, seed=314)
    truth = pair.expected(tolerance)
    columns_a = inspect_csv_columns(pair.csv_a, source=Source.A)
    columns_b = inspect_csv_columns(pair.csv_b, source=Source.B)
    assert columns_a == HEADERS_A and columns_b == HEADERS_B
    manual = ColumnMapping(tuple(zip(KEYS_A, KEYS_B, strict=True)), AMOUNT_A, AMOUNT_B)
    profile = load_mapping_profile(export_mapping_profile(manual, amount_tolerance=tolerance))
    profile.validate_columns(columns_a, columns_b)
    assert profile.mapping == manual
    assert profile.amount_tolerance.as_tuple() == tolerance.as_tuple()
    a = ingest_csv(
        pair.csv_a,
        source=Source.A,
        key_columns=profile.mapping.keys_a,
        amount_column=profile.mapping.amount_a,
    )
    b = ingest_csv(
        pair.csv_b,
        source=Source.B,
        key_columns=profile.mapping.keys_b,
        amount_column=profile.mapping.amount_b,
    )
    result = reconcile(a, b, amount_tolerance=profile.amount_tolerance)

    assert len(a) == truth.record_count_a and len(b) == truth.record_count_b
    assert len(result.findings) == len(truth.groups)
    assert Counter(f.category.value for f in result.findings) == Counter(truth.category_counts)
    assert result.total_a == truth.total_a
    assert result.total_b == truth.total_b
    assert result.control_difference == result.finding_delta_sum == truth.control_difference
    assert result.tolerated_delta_total == truth.tolerated_delta_total
    assert len(result.exceptions) == len(truth.exception_keys)
    for finding, expected in zip(result.findings, truth.groups, strict=True):
        assert (finding.key, finding.category.value) == (expected.key, expected.category)
        assert (len(finding.rows_a), len(finding.rows_b)) == (expected.rows_a, expected.rows_b)
        assert finding.amount_a == decimal_units(expected.units_a)
        assert finding.amount_b == decimal_units(expected.units_b)
        assert finding.delta == expected.delta
    accounted = [row for finding in result.findings for row in (*finding.rows_a, *finding.rows_b)]
    assert Counter((row.source, row.source_row) for row in accounted) == Counter(
        (row.source, row.source_row) for row in (*a, *b)
    )
    assert {id(row) for row in accounted} == {id(row) for row in (*a, *b)}
    for records, text in ((a, pair.csv_a), (b, pair.csv_b)):
        raw = list(csv.DictReader(StringIO(text, newline="")))
        assert [record.source_row for record in records] == list(range(2, len(records) + 2))
        assert all(dict(record.raw_fields) == row for record, row in zip(records, raw, strict=True))
    assert any(record.key[0].startswith("000") and record.key[1].startswith("000") for record in a)
    assert any(record.amount < 0 for record in a)
    assert any(record.amount.as_tuple().exponent < -2 for record in a)

    exported = list(
        csv.DictReader(StringIO(export_exceptions_csv(result).decode("utf-8"), newline=""))
    )
    assert [row["Matching key"] for row in exported] == [
        " / ".join(key) for key in truth.exception_keys
    ]
    assert len({row["Matching key"] for row in exported}) == len(exported)
    by_key = {finding.key: finding for finding in result.findings}
    for row, key in zip(exported, truth.exception_keys, strict=True):
        finding = by_key[key]
        assert Decimal(row["Delta (A - B)"]) == finding.delta
        assert row["File A records"] == "; ".join(
            str(record.source_row) for record in finding.rows_a
        )
        assert row["File B records"] == "; ".join(
            str(record.source_row) for record in finding.rows_b
        )
        if not finding.rows_a:
            assert row["File A amount"] == ""
        elif finding.amount_a == 0:
            assert Decimal(row["File A amount"]) == 0
        if not finding.rows_b:
            assert row["File B amount"] == ""
        elif finding.amount_b == 0:
            assert Decimal(row["File B amount"]) == 0
    exported_by_key = {row["Matching key"]: row for row in exported}
    for index, present, missing in (
        (88, "File A amount", "File B amount"),
        (92, "File B amount", "File A amount"),
    ):
        row = exported_by_key[" / ".join(pair.scenarios[index].key)]
        assert Decimal(row[present]) == 0
        assert row[missing] == ""

    duplicates = [
        finding
        for finding in result.findings
        if finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    ]
    assert len(duplicates) == truth.category_counts["duplicate_ambiguous"]
    assert any(
        finding.delta == 0 and len(finding.rows_a) == len(finding.rows_b) == 2
        for finding in duplicates
    )
    assert any(not finding.rows_a or not finding.rows_b for finding in duplicates)


def test_row_shuffle_changes_trace_positions_without_changing_financial_results() -> None:
    first = generate_pair(1_000, seed=11, shuffle_seed=1)
    second = generate_pair(1_000, seed=11, shuffle_seed=2)
    assert first.scenarios == second.scenarios
    assert first.csv_a != second.csv_a and first.csv_b != second.csv_b
    assert first.expected(Decimal("0.01")) == second.expected(Decimal("0.01"))
    results = []
    for pair in (first, second):
        a = ingest_csv(pair.csv_a, source=Source.A, key_columns=KEYS_A, amount_column=AMOUNT_A)
        b = ingest_csv(pair.csv_b, source=Source.B, key_columns=KEYS_B, amount_column=AMOUNT_B)
        result = reconcile(a, b, amount_tolerance=Decimal("0.01"))
        # Source row numbers must describe the current file, so compare only
        # financial semantics across physically different CSV row orders.
        results.append(
            [(f.key, f.category, f.amount_a, f.amount_b, f.delta) for f in result.findings]
        )
        assert result == reconcile(reversed(a), reversed(b), amount_tolerance=Decimal("0.01"))
    assert results[0] == results[1]


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("tolerance", [Decimal("0"), Decimal("0.01")])
def test_grouped_workload_oracle_has_hand_auditable_counts_and_recipes(mode, tolerance) -> None:
    pair = generate_pair(100, seed=42, workload="grouped")
    assert pair == generate_pair(100, seed=42, workload="grouped")
    assert pair != generate_pair(100, seed=43, workload="grouped")
    truth = pair.expected(tolerance, mode=mode.value)
    accepting = tolerance != 0
    if mode is ReconciliationMode.UNIQUE:
        counts = (40, 10 if accepting else 0, 10 if accepting else 20, 5, 5, 30)
        tolerated = "0.0050" if accepting else "0"
    else:
        counts = (57, 15 if accepting else 0, 16 if accepting else 31, 6, 6, 0)
        tolerated = "0.0100" if accepting else "0"
    assert list(truth.category_counts.values()) == list(counts)
    assert truth.tolerated_delta_total == Decimal(tolerated)
    assert (truth.record_count_a, truth.record_count_b) == (187, 186)
    assert len(truth.exception_keys) == 100 - counts[0] - counts[1]
    scenarios = pair.scenarios
    for index, shape in ((70, (1, 4)), (75, (4, 1)), (80, (4, 4))):
        scenario = scenarios[index]
        assert len(scenario.amounts_a) == 1 if shape[0] == 1 else len(scenario.amounts_a) >= 4
        assert len(scenario.amounts_b) == 1 if shape[1] == 1 else len(scenario.amounts_b) >= 4
        assert sum(scenario.amounts_a) == sum(scenario.amounts_b)
    assert sum(scenarios[85].amounts_a) - sum(scenarios[85].amounts_b) == 100
    assert sum(scenarios[90].amounts_a) - sum(scenarios[90].amounts_b) == 101
    assert scenarios[95].amounts_b == () and sum(scenarios[95].amounts_a) == 0
    assert scenarios[96].amounts_a == () and sum(scenarios[96].amounts_b) == 0
    assert sum(scenarios[97].amounts_a) == sum(scenarios[97].amounts_b) == 0
    assert set((*scenarios[98].amounts_a, *scenarios[98].amounts_b)) == {0}
    assert sum(scenarios[99].amounts_a) - sum(scenarios[99].amounts_b) == 2501
    assert scenarios[99].amounts_a[0] == 10**20 + 17
    assert any(amount < 0 for scenario in scenarios[70:95] for amount in scenario.amounts_a)
    assert len({scenario.key[1] for scenario in scenarios}) < len(scenarios)


def test_expected_outcomes_do_not_import_or_call_product_code(monkeypatch) -> None:
    import ast
    import inspect

    import scripts.synthetic_data as oracle

    imports = [
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(ast.parse(inspect.getsource(oracle)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert not any(name and name.startswith("tallydiff") for name in imports)

    def forbidden(*args, **kwargs):
        pytest.fail("The construction oracle called reconciliation")

    monkeypatch.setattr("tallydiff.reconcile", forbidden)
    monkeypatch.setattr("tallydiff.engine.reconcile", forbidden)
    pair = oracle.generate_pair(100, seed=42, workload="grouped")
    with localcontext() as context:
        context.prec = 2
        for mode in ("unique", "grouped_by_key"):
            assert len(pair.expected(Decimal("0.01"), mode=mode).groups) == 100


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("tolerance", [Decimal("0"), Decimal("0.01")])
@pytest.mark.parametrize(
    "formats", [("csv", "csv"), ("xlsx", "xlsx"), ("csv", "xlsx"), ("xlsx", "csv")]
)
def test_grouped_workload_matches_independent_truth_in_every_format(
    mode, tolerance, formats, xlsx_bytes
) -> None:
    pair = generate_pair(300, seed=314, workload="grouped")
    mapping = ColumnMapping(tuple(zip(KEYS_A, KEYS_B, strict=True)), AMOUNT_A, AMOUNT_B)
    profile = load_mapping_profile(
        export_mapping_profile(mapping, amount_tolerance=tolerance, reconciliation_mode=mode)
    )
    assert profile.reconciliation_mode is mode
    inputs = []
    for source, text, kind, keys, amount in (
        (Source.A, pair.csv_a, formats[0], mapping.keys_a, mapping.amount_a),
        (Source.B, pair.csv_b, formats[1], mapping.keys_b, mapping.amount_b),
    ):
        options = {"source": source, "key_columns": keys, "amount_column": amount}
        if kind == "csv":
            records = ingest_csv(text, **options)
        else:
            # Monetary text cells preserve export precision, including large sub-cent values.
            rows = list(csv.reader(StringIO(text, newline="")))
            data = xlsx_bytes({"Ledger é": rows})
            records = ingest_xlsx(data, worksheet="Ledger é", **options)
        raw = list(csv.DictReader(StringIO(text, newline="")))
        assert [dict(record.raw_fields) for record in records] == raw
        assert [record.source_row for record in records] == list(range(2, len(records) + 2))
        assert any("Café" in str(record.raw_fields) for record in records)
        assert Counter((record.key, record.amount) for record in records) == Counter(
            (scenario.key, decimal_units(units))
            for scenario in pair.scenarios
            for units in (scenario.amounts_a if source is Source.A else scenario.amounts_b)
        )
        inputs.append(records)
    profile.validate_columns(tuple(inputs[0][0].raw_fields), tuple(inputs[1][0].raw_fields))
    truth = pair.expected(tolerance, mode=mode.value)
    result = reconcile(
        *inputs, amount_tolerance=profile.amount_tolerance, mode=profile.reconciliation_mode
    )
    report = export_exceptions_csv(result)
    verify_result(*inputs, result, report, truth)
    assert result == reconcile(
        *(reversed(records) for records in inputs), amount_tolerance=tolerance, mode=mode
    )
    assert result.total_a - result.total_b == sum(finding.delta for finding in result.findings)
    by_key = {finding.key: finding for finding in result.findings}
    for scenario in pair.scenarios:
        finding = by_key[scenario.key]
        multi = len(scenario.amounts_a) > 1 or len(scenario.amounts_b) > 1
        if mode is ReconciliationMode.UNIQUE and multi:
            assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
        elif mode is ReconciliationMode.GROUPED_BY_KEY:
            assert finding.category is not FindingCategory.DUPLICATE_AMBIGUOUS
            if scenario.kind == "a_only_zero_net":
                assert finding.category is FindingCategory.A_ONLY and finding.delta == 0
            elif scenario.kind == "b_only_zero_net":
                assert finding.category is FindingCategory.B_ONLY and finding.delta == 0
    exported_keys = {row["Matching key"] for row in csv.DictReader(StringIO(report.decode()))}
    for finding in result.findings:
        assert (" / ".join(finding.key) in exported_keys) == finding.is_exception


@pytest.fixture(scope="module")
def large_grouped_exports():
    pair = generate_pair(40_000, seed=2026, workload="grouped")
    a = ingest_csv(pair.csv_a, source=Source.A, key_columns=KEYS_A, amount_column=AMOUNT_A)
    b = ingest_csv(pair.csv_b, source=Source.B, key_columns=KEYS_B, amount_column=AMOUNT_B)
    return pair, a, b


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_large_grouped_result_integrity_review_and_complete_export(
    large_grouped_exports, mode
) -> None:
    pair, a, b = large_grouped_exports
    tolerance = Decimal("0.01")
    truth = pair.expected(tolerance, mode=mode.value)
    result = reconcile(a, b, amount_tolerance=tolerance, mode=mode)
    original_findings = result.findings
    exceptions = result.exceptions
    report = export_exceptions_csv(result)
    verify_result(a, b, result, report, truth)
    assert result.total_a - result.total_b == sum(finding.delta for finding in result.findings)
    assert result == reconcile(a, b, amount_tolerance=tolerance, mode=mode)
    assert len(exceptions) == (20_000 if mode is ReconciliationMode.UNIQUE else 11_200)
    exception_keys = set(truth.exception_keys)
    expected = [group for group in truth.groups if group.key in exception_keys]
    expected_keys = [group.key for group in expected]
    assert [finding.key for finding in review_exceptions(exceptions)] == expected_keys
    assert review_exceptions(exceptions, query=" \t ") == exceptions
    assert [finding.key for finding in review_exceptions(exceptions, query="adj-000000099")] == [
        pair.scenarios[99].key
    ]
    for category in (
        FindingCategory.AMOUNT_MISMATCH,
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
        FindingCategory.DUPLICATE_AMBIGUOUS,
    ):
        assert [
            finding.key for finding in review_exceptions(exceptions, categories=(category,))
        ] == [group.key for group in expected if group.category == category.value]
    for minimum in (Decimal("0.0101"), Decimal("0.010100000000000000000001")):
        visible = review_exceptions(exceptions, minimum_abs_delta=minimum)
        assert [finding.key for finding in visible] == [
            group.key
            for group in expected
            if decimal_units(abs(group.units_a - group.units_b)) >= minimum
        ]
    inclusive = review_exceptions(exceptions, minimum_abs_delta=Decimal("0.0101"))
    above = review_exceptions(exceptions, minimum_abs_delta=Decimal("0.010100000000000000000001"))
    assert len(inclusive) - len(above) == 1600
    for order, descending in (("absolute_delta_desc", True), ("absolute_delta_asc", False)):
        visible = review_exceptions(exceptions, sort_order=order)
        ordered = sorted(
            expected,
            key=lambda group: (
                (-1 if descending else 1) * abs(group.units_a - group.units_b),
                group.key,
            ),
        )
        assert [finding.key for finding in visible] == [group.key for group in ordered]
        assert review_exceptions(tuple(reversed(exceptions)), sort_order=order) == visible
    category = (
        FindingCategory.DUPLICATE_AMBIGUOUS
        if mode is ReconciliationMode.UNIQUE
        else FindingCategory.AMOUNT_MISMATCH
    )
    combined = review_exceptions(
        exceptions,
        query="ADJ-",
        categories=(category,),
        minimum_abs_delta=Decimal("0.2501"),
        sort_order="absolute_delta_desc",
    )
    planned = [
        group
        for group in expected
        if group.category == category.value
        and any("adj-" in component.casefold() for component in group.key)
        and abs(group.units_a - group.units_b) >= 2501
    ]
    planned.sort(key=lambda group: (-abs(group.units_a - group.units_b), group.key))
    assert len(combined) == len(planned) == 400
    assert [finding.key for finding in combined] == [group.key for group in planned]
    original_ids = {id(finding) for finding in exceptions}
    assert all(id(finding) in original_ids for finding in combined)
    assert result.findings is original_findings and result.exceptions == exceptions
    assert export_exceptions_csv(result) == report


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_grouped_workload_physical_row_shuffles_preserve_financial_semantics(mode) -> None:
    pairs = [generate_pair(500, seed=11, shuffle_seed=seed, workload="grouped") for seed in (1, 2)]
    assert pairs[0].scenarios == pairs[1].scenarios
    assert pairs[0].csv_a != pairs[1].csv_a and pairs[0].csv_b != pairs[1].csv_b
    outputs = []
    for pair in pairs:
        a = ingest_csv(pair.csv_a, source=Source.A, key_columns=KEYS_A, amount_column=AMOUNT_A)
        b = ingest_csv(pair.csv_b, source=Source.B, key_columns=KEYS_B, amount_column=AMOUNT_B)
        result = reconcile(a, b, amount_tolerance=Decimal("0.01"), mode=mode)
        outputs.append(
            [(f.key, f.category, f.amount_a, f.amount_b, f.delta) for f in result.findings]
        )
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("tolerance", [Decimal("0"), Decimal("0.01")])
def test_benchmark_exercises_each_mode_with_independent_verification(mode, tolerance) -> None:
    measured = measure(100, 42, tolerance, mode=mode)
    assert (measured.rows_a, measured.rows_b) == (187, 186)
    if mode is ReconciliationMode.UNIQUE:
        assert measured.exceptions == (60 if tolerance == 0 else 50)
    else:
        assert measured.exceptions == (43 if tolerance == 0 else 28)
    assert measured.total >= measured.reconciliation >= 0


@pytest.mark.parametrize("corruption", ["group_amount", "source_accounting", "export"])
def test_benchmark_verifier_rejects_corrupted_results(corruption) -> None:
    pair = generate_pair(100, seed=42, workload="grouped")
    a = ingest_csv(pair.csv_a, source=Source.A, key_columns=KEYS_A, amount_column=AMOUNT_A)
    b = ingest_csv(pair.csv_b, source=Source.B, key_columns=KEYS_B, amount_column=AMOUNT_B)
    mode = ReconciliationMode.GROUPED_BY_KEY
    truth = pair.expected(Decimal("0.01"), mode=mode.value)
    result = reconcile(a, b, amount_tolerance=Decimal("0.01"), mode=mode)
    report = export_exceptions_csv(result)
    if corruption == "export":
        report = b"Matching key\r\n"
        message = "Exported exception groups"
    else:
        findings = list(result.findings)
        index = next(index for index, finding in enumerate(findings) if len(finding.rows_a) > 1)
        original = findings[index]
        if corruption == "group_amount":
            findings[index] = replace(original, amount_a=original.amount_a + Decimal("0.0001"))
            message = "Group differs"
        else:
            findings[index] = replace(original, rows_a=(original.rows_a[0],) * len(original.rows_a))
            message = "Source rows were not accounted for exactly once"
        result = replace(result, findings=tuple(findings))
    with pytest.raises(AssertionError, match=message):
        verify_result(a, b, result, report, truth)
