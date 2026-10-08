import csv
from collections import Counter
from decimal import Decimal
from io import StringIO

import pytest

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
    Source,
    export_exceptions_csv,
    export_mapping_profile,
    ingest_csv,
    inspect_csv_columns,
    load_mapping_profile,
    reconcile,
)


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
