"""Independent normalized workload validation; 100k runs live in the benchmark CLI."""

import ast
import csv
import inspect
import json
import sys
from collections import Counter
from dataclasses import replace
from decimal import Decimal, localcontext
from io import StringIO
from random import Random

import pytest

import scripts.synthetic_data as oracle
import tallydiff.engine as engine
from scripts.benchmark import measure, normalization_config, verify_result
from scripts.synthetic_data import (
    AMOUNT_A,
    AMOUNT_B,
    HEADERS_A,
    HEADERS_B,
    KEYS_A,
    KEYS_B,
    NORMALIZATION_RULES,
    decimal_units,
    generate_pair,
)
from tallydiff import (
    ColumnMapping,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationError,
    KeyNormalizationRules,
    NormalizationCollisionError,
    ReconciliationMode,
    Source,
    SourceRecord,
    export_exceptions_csv,
    export_mapping_profile,
    ingest_csv,
    ingest_xlsx,
    inspect_csv_columns,
    inspect_xlsx_columns,
    load_mapping_profile,
    reconcile,
)
from tallydiff.presentation import review_exceptions

CONFIG = normalization_config("normalization")
TOLERANCE = Decimal("0.01")
MAPPING = ColumnMapping(tuple(zip(KEYS_A, KEYS_B, strict=True)), AMOUNT_A, AMOUNT_B)


def _ingest_pair(pair):
    return (
        ingest_csv(pair.csv_a, source=Source.A, key_columns=KEYS_A, amount_column=AMOUNT_A),
        ingest_csv(pair.csv_b, source=Source.B, key_columns=KEYS_B, amount_column=AMOUNT_B),
    )


def _snapshot(records):
    return [(id(row), row.key, row.amount, dict(row.raw_fields)) for row in records]


def _run(pair, a, b, mode, config=CONFIG, tolerance=TOLERANCE):
    result = reconcile(a, b, mode=mode, amount_tolerance=tolerance, key_normalization=config)
    report = export_exceptions_csv(result)
    verify_result(
        a, b, result, report, pair.expected(tolerance, mode=mode.value), key_normalization=config
    )
    return result, report


@pytest.fixture(scope="module")
def realistic_normalized():
    pair = generate_pair(1_200, seed=42, workload="normalization")
    return pair, *_ingest_pair(pair)


@pytest.fixture(scope="module")
def large_normalized():
    pair = generate_pair(20_000, seed=42, workload="normalization")
    return pair, *_ingest_pair(pair)


def test_normalization_oracle_is_stdlib_only_and_never_calls_product(monkeypatch) -> None:
    imports = [
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(ast.parse(inspect.getsource(oracle)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert all(name.split(".")[0] in sys.stdlib_module_names for name in imports)

    def forbidden(*args, **kwargs):
        pytest.fail("Independent generation/oracle must not call TallyDiff")

    monkeypatch.setattr("tallydiff.normalization.normalize_key", forbidden)
    monkeypatch.setattr(engine, "normalize_key", forbidden)
    monkeypatch.setattr(engine, "reconcile", forbidden)
    monkeypatch.setattr("tallydiff.reconcile", forbidden)
    pair = generate_pair(300, seed=42, workload="normalization")
    with localcontext() as context:
        context.prec = 2
        for mode in ("unique", "grouped_by_key"):
            truth = pair.expected(TOLERANCE, mode=mode)
            assert len(truth.groups) == 300
            assert sum(group.delta_units for group in truth.groups) == (
                sum(group.units_a for group in truth.groups)
                - sum(group.units_b for group in truth.groups)
            )


def test_normalization_reuses_grouped_amounts_and_has_known_raw_key_variations() -> None:
    pair = generate_pair(1_200, seed=42, workload="normalization")
    grouped = generate_pair(1_200, seed=42, workload="grouped")
    assert pair == generate_pair(1_200, seed=42, workload="normalization")
    assert pair != generate_pair(1_200, seed=43, workload="normalization")
    assert [(s.kind, s.amounts_a, s.amounts_b) for s in pair.scenarios] == [
        (s.kind, s.amounts_a, s.amounts_b) for s in grouped.scenarios
    ]
    assert len({scenario.key_variant for scenario in pair.scenarios}) == 12
    assert pair.scenarios[0].key == ("vendor000000 west", "0")
    assert pair.scenarios[0].raw_key_a == ("Vendor000000 West", "0")
    assert pair.scenarios[0].raw_key_b == ("VENDOR000000 WEST", "0")
    assert pair.scenarios[1].raw_key_a == ("vendor000001   west", "0")
    assert pair.scenarios[2].raw_key_a == ("vendor000002\twest", "0")
    assert pair.scenarios[3].raw_key_a == ("vendor000003\u00a0west", "0")
    assert pair.scenarios[3].raw_key_b == ("vendor000003\u2003west", "0")
    assert pair.scenarios[4].raw_key_a == ("vendor-000004 west", "0")
    assert pair.scenarios[5].raw_key_b == ("vendor“000005” west", "0")
    assert pair.scenarios[6].raw_key_a == ("vendor000006 west", "000000000")
    assert pair.scenarios[8].key == ("strasse000008 west", "0")
    assert pair.scenarios[8].raw_key_a == ("Straße000008 west", "0")
    assert pair.scenarios[8].raw_key_b == ("STRASSE000008 west", "0")
    assert pair.scenarios[9].key == pair.scenarios[9].raw_key_a == pair.scenarios[9].raw_key_b
    assert pair.scenarios[10].raw_key_a[0] == pair.scenarios[10].raw_key_b[0]
    assert pair.scenarios[10].raw_key_a[1] != pair.scenarios[10].raw_key_b[1]
    for side in ("a", "b"):
        present = [s for s in pair.scenarios if getattr(s, f"amounts_{side}")]
        assert len({s.key for s in present}) == len(present)
        assert len({getattr(s, f"raw_key_{side}") for s in present}) == len(present)
    for mode, counts in (
        ("unique", (480, 120, 120, 60, 60, 360)),
        ("grouped_by_key", (684, 180, 192, 72, 72, 0)),
    ):
        truth = pair.expected(TOLERANCE, mode=mode)
        legacy_truth = grouped.expected(TOLERANCE, mode=mode)
        assert list(truth.category_counts.values()) == list(counts)
        assert (
            truth.total_a,
            truth.total_b,
            truth.control_difference,
            truth.tolerated_delta_total,
        ) == (
            legacy_truth.total_a,
            legacy_truth.total_b,
            legacy_truth.control_difference,
            legacy_truth.tolerated_delta_total,
        )


@pytest.mark.parametrize("problem", ["canonical", "raw", "absent"])
def test_independent_construction_guard_rejects_broken_key_plans(problem) -> None:
    plans = list(generate_pair(100, workload="normalization").scenarios)
    if problem == "canonical":
        plans[1] = replace(plans[1], key=plans[0].key)
        message = "Duplicate canonical"
    elif problem == "raw":
        plans[1] = replace(plans[1], raw_key_a=plans[0].raw_key_a)
        message = "Original key reused"
    else:
        plans[65] = replace(plans[65], raw_key_a=("absent", "0"))
        message = "Absent source"
    with pytest.raises(AssertionError, match=message):
        oracle._validate_normalization_keys(tuple(plans))


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_large_normalized_results_account_for_every_row_and_raw_key(large_normalized, mode) -> None:
    pair, a, b = large_normalized
    result, report = _run(pair, a, b, mode)
    assert len(result.findings) == 20_000
    assert result.key_normalization is CONFIG
    assert len(result.exceptions) == (10_000 if mode is ReconciliationMode.UNIQUE else 5_600)
    assert len(result.tolerated_findings) == (2_000 if mode is ReconciliationMode.UNIQUE else 3_000)
    for records, text in ((a, pair.csv_a), (b, pair.csv_b)):
        raw = list(csv.DictReader(StringIO(text, newline="")))
        assert [dict(row.raw_fields) for row in records] == raw
        assert [row.source_row for row in records] == list(range(2, len(records) + 2))
    assert result.finding_delta_sum == result.control_difference
    assert export_exceptions_csv(result) == report


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_normalization_materially_increases_intended_cross_source_groups(
    realistic_normalized, mode
) -> None:
    pair, a, b = realistic_normalized
    normalized, _ = _run(pair, a, b, mode)
    exact = reconcile(a, b, mode=mode, amount_tolerance=TOLERANCE, key_normalization=None)
    intended = sum(bool(s.amounts_a and s.amounts_b) for s in pair.scenarios)
    identical_raw = sum(
        bool(s.amounts_a and s.amounts_b and s.raw_key_a == s.raw_key_b) for s in pair.scenarios
    )
    assert sum(bool(f.rows_a and f.rows_b) for f in normalized.findings) == intended
    assert sum(bool(f.rows_a and f.rows_b) for f in exact.findings) == identical_raw
    assert intended > identical_raw > 0
    assert (exact.total_a, exact.total_b, exact.control_difference) == (
        normalized.total_a,
        normalized.total_b,
        normalized.control_difference,
    )
    assert len(exact.findings) > len(normalized.findings)
    assert exact.key_normalization is None


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_reversed_and_seeded_record_order_preserve_findings_and_sorted_evidence(
    realistic_normalized, mode
) -> None:
    pair, a, b = realistic_normalized
    before = _snapshot((*a, *b))
    baseline, report = _run(pair, a, b, mode)
    for ordering_a, ordering_b in (
        (tuple(reversed(a)), tuple(reversed(b))),
        (tuple(Random(13).sample(a, len(a))), tuple(Random(71).sample(b, len(b)))),
    ):
        result, exported = _run(pair, ordering_a, ordering_b, mode)
        assert result == baseline
        assert exported == report
        assert [f.key for f in result.findings] == sorted(f.key for f in result.findings)
    assert _snapshot((*a, *b)) == before


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_physical_csv_shuffles_preserve_normalized_financial_results(mode) -> None:
    outputs = []
    for seed in (3, 17):
        pair = generate_pair(500, seed=42, shuffle_seed=seed, workload="normalization")
        a, b = _ingest_pair(pair)
        result, _ = _run(pair, a, b, mode)
        outputs.append(
            [(f.key, f.category, f.amount_a, f.amount_b, f.delta) for f in result.findings]
        )
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize(
    "formats", [("csv", "csv"), ("csv", "xlsx"), ("xlsx", "csv"), ("xlsx", "xlsx")]
)
def test_normalized_formats_and_profile_v4_match_independent_truth(
    mode, formats, xlsx_bytes
) -> None:
    pair = generate_pair(600, seed=42, workload="normalization")
    profile_bytes = export_mapping_profile(
        MAPPING,
        amount_tolerance=Decimal("0.0100"),
        reconciliation_mode=mode,
        key_normalization=CONFIG,
    )
    document = json.loads(profile_bytes)
    assert document["version"] == 4
    assert document["comparison_fields"] == []
    assert document["key_normalization"] == list(NORMALIZATION_RULES)
    assert set(document) == {
        "format",
        "version",
        "key_pairs",
        "amount_columns",
        "amount_tolerance",
        "reconciliation_mode",
        "key_normalization",
        "comparison_fields",
    }
    assert not any(
        scenario.raw_key_a[0].encode() in profile_bytes
        for scenario in pair.scenarios
        if scenario.raw_key_a
    )
    assert b"Synthetic" not in profile_bytes and b"2026-" not in profile_bytes
    profile = load_mapping_profile(profile_bytes)
    assert profile.mapping == MAPPING
    assert profile.amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    assert profile.reconciliation_mode is mode
    assert profile.key_normalization == CONFIG
    inputs, columns = [], []
    for source, text, kind, keys, amount in (
        (Source.A, pair.csv_a, formats[0], KEYS_A, AMOUNT_A),
        (Source.B, pair.csv_b, formats[1], KEYS_B, AMOUNT_B),
    ):
        options = {"source": source, "key_columns": keys, "amount_column": amount}
        raw = list(csv.DictReader(StringIO(text, newline="")))
        if kind == "csv":
            records = ingest_csv(text, **options)
            columns.append(inspect_csv_columns(text, source=source))
            expected_rows = list(range(2, len(raw) + 2))
        else:
            parsed = list(csv.reader(StringIO(text, newline="")))
            # Preserve huge/sub-cent monetary text. Only trailing blank rows
            # are supported; interior blank rows remain an ingestion error.
            sheet_rows = [*parsed, [None] * len(parsed[0]), [None] * len(parsed[0])]
            expected_rows = list(range(2, len(parsed) + 1))
            data = xlsx_bytes({"Ledger é": sheet_rows})
            records = ingest_xlsx(data, worksheet="Ledger é", **options)
            columns.append(inspect_xlsx_columns(data, source=source, worksheet="Ledger é"))
        assert [record.source_row for record in records] == expected_rows
        assert [dict(record.raw_fields) for record in records] == raw
        description = HEADERS_A[3] if source is Source.A else HEADERS_B[3]
        assert any("\n" in row.raw_fields[description] for row in records)
        inputs.append(records)
    profile.validate_columns(*columns)
    result, report = _run(
        pair,
        *inputs,
        profile.reconciliation_mode,
        profile.key_normalization,
        profile.amount_tolerance,
    )
    baseline, _ = _run(pair, *_ingest_pair(pair), mode)
    assert [(f.key, f.category, f.amount_a, f.amount_b, f.delta) for f in result.findings] == [
        (f.key, f.category, f.amount_a, f.amount_b, f.delta) for f in baseline.findings
    ]
    assert result.key_normalization is profile.key_normalization
    assert [f.key for f in result.exceptions] == [f.key for f in baseline.exceptions]
    exported = list(csv.DictReader(StringIO(report.decode(), newline="")))
    for finding, row in zip(result.exceptions, exported, strict=True):
        assert row["File A records"] == "; ".join(str(r.source_row) for r in finding.rows_a)
        assert row["File B records"] == "; ".join(str(r.source_row) for r in finding.rows_b)


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_all_false_profile_is_canonical_none_on_realistic_exports(
    realistic_normalized, mode
) -> None:
    pair, a, b = realistic_normalized
    profile = load_mapping_profile(
        export_mapping_profile(
            MAPPING,
            reconciliation_mode=mode,
            key_normalization=KeyNormalizationConfig(
                (KeyNormalizationRules(), KeyNormalizationRules())
            ),
        )
    )
    profile.validate_columns(HEADERS_A, HEADERS_B)
    assert profile.key_normalization is None
    assert reconcile(a, b, mode=mode, key_normalization=profile.key_normalization) == reconcile(
        a, b, mode=mode, key_normalization=None
    )
    assert len(pair.scenarios) == 1_200


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_normalized_export_and_review_use_all_independent_exception_keys(
    realistic_normalized, mode
) -> None:
    pair, a, b = realistic_normalized
    before = _snapshot((*a, *b))
    result, report = _run(pair, a, b, mode)
    truth = pair.expected(TOLERANCE, mode=mode.value)
    planned = [group for group in truth.groups if group.is_exception]
    exceptions = result.exceptions
    rows = list(csv.DictReader(StringIO(report.decode(), newline="")))
    assert [row["Matching key"] for row in rows] == [" / ".join(group.key) for group in planned]
    assert any(group.delta_units == 0 for group in planned)
    assert all(group.category not in ("exact_match", "within_tolerance") for group in planned)
    for group, row in zip(planned, rows, strict=True):
        assert (
            row["Category"]
            == {
                "amount_mismatch": "Amount mismatch",
                "a_only": "File A only",
                "b_only": "File B only",
                "duplicate_ambiguous": "Duplicate / ambiguous",
            }[group.category]
        )
        assert Decimal(row["Delta (A - B)"]) == decimal_units(group.delta_units)
    query = planned[0].key[0].upper()
    assert [f.key for f in review_exceptions(exceptions, query=query)] == [
        group.key for group in planned if any(query.casefold() in key for key in group.key)
    ]
    for category in (
        FindingCategory.AMOUNT_MISMATCH,
        FindingCategory.A_ONLY,
        FindingCategory.B_ONLY,
        FindingCategory.DUPLICATE_AMBIGUOUS,
    ):
        assert [f.key for f in review_exceptions(exceptions, categories=(category,))] == [
            group.key for group in planned if group.category == category.value
        ]
    for minimum in (101, 2501):
        assert [
            f.key for f in review_exceptions(exceptions, minimum_abs_delta=decimal_units(minimum))
        ] == [group.key for group in planned if abs(group.delta_units) >= minimum]
    for order, sign in (("absolute_delta_asc", 1), ("absolute_delta_desc", -1)):
        expected = sorted(planned, key=lambda group: (sign * abs(group.delta_units), group.key))
        visible = review_exceptions(exceptions, sort_order=order)
        assert [f.key for f in visible] == [group.key for group in expected]
        original_ids = {id(finding) for finding in exceptions}
        assert all(id(finding) in original_ids for finding in visible)
    assert export_exceptions_csv(result) == report
    assert _snapshot((*a, *b)) == before


def _risk_record(source, number, key):
    return SourceRecord(
        source,
        number,
        key,
        Decimal("0"),
        {"Vendor": key[0], "Invoice": key[1], "Memo": "Retained café,\noriginal", "Amount": "0"},
    )


def _collision_rows(source):
    return tuple(
        _risk_record(source, number, key)
        for number, key in (
            (11, ("ACME-01\tWEST", "00042")),
            (2, ("acme01 west", "42")),
            (7, ("Acme—01   West", "000042")),
            (17, ("ACME-01\tWEST", "00042")),
        )
    )


def _assert_collision_evidence(collisions, expected_key, originals):
    assert len(collisions) == 1
    collision = collisions[0]
    assert collision.normalized_key == expected_key
    distinct = sorted({row.key for row in originals})
    assert [original.original_key for original in collision.originals] == distinct
    for original in collision.originals:
        rows = sorted(
            (row for row in originals if row.key == original.original_key),
            key=lambda row: row.source_row,
        )
        assert [row.source_row for row in original.records] == [row.source_row for row in rows]
        assert len(original.records) == len(rows)
        assert all(
            retained is expected for retained, expected in zip(original.records, rows, strict=True)
        )
        assert all(
            dict(row.raw_fields)["Memo"] == "Retained café,\noriginal" for row in original.records
        )


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("sources", [("A",), ("B",), ("A", "B")])
def test_intentional_collisions_block_before_any_result_and_retain_all_originals(
    mode, sources, monkeypatch
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("A collision must block before classification or result construction")

    monkeypatch.setattr(engine, "_classify", forbidden)
    monkeypatch.setattr(engine, "ReconciliationResult", forbidden)
    a = (
        _collision_rows(Source.A)
        if "A" in sources
        else (_risk_record(Source.A, 2, ("acme01 west", "42")),)
    )
    b = (
        _collision_rows(Source.B)
        if "B" in sources
        else (_risk_record(Source.B, 2, ("acme01 west", "42")),)
    )
    before = _snapshot((*a, *b))
    errors = []
    for ordering_a, ordering_b in (
        (a, b),
        (tuple(reversed(a)), tuple(Random(11).sample(b, len(b)))),
    ):
        with pytest.raises(NormalizationCollisionError) as caught:
            reconcile(ordering_a, ordering_b, mode=mode, key_normalization=CONFIG)
        errors.append(caught.value)
        for source, rows, collisions in (
            ("A", a, caught.value.collisions_a),
            ("B", b, caught.value.collisions_b),
        ):
            if source in sources:
                _assert_collision_evidence(collisions, ("acme01 west", "42"), rows)
                assert collisions[0].source.value == source
            else:
                assert collisions == ()
    assert errors[0].collisions_a == errors[1].collisions_a
    assert errors[0].collisions_b == errors[1].collisions_b
    assert _snapshot((*a, *b)) == before


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_partial_composite_and_cross_source_convergence_are_safe(mode) -> None:
    a = (
        _risk_record(Source.A, 3, ("ACME-01 West", "00042")),
        _risk_record(Source.A, 2, ("ACME01 WEST", "43")),
    )
    b = (
        _risk_record(Source.B, 8, ("acme01 west", "42")),
        _risk_record(Source.B, 6, ("acme-01 WEST", "000043")),
    )
    before = _snapshot((*a, *b))
    result = reconcile(a, b, mode=mode, key_normalization=CONFIG)
    assert [finding.key for finding in result.findings] == [
        ("acme01 west", "42"),
        ("acme01 west", "43"),
    ]
    assert all(f.category is FindingCategory.EXACT_MATCH for f in result.findings)
    assert _snapshot((*a, *b)) == before


def _with_collision(records, scenario, source):
    raw = scenario.raw_key_a if source is Source.A else scenario.raw_key_b
    original = next(row for row in records if row.key == raw)
    maximum = max(row.source_row for row in records)
    keys = (raw, (scenario.key[0], raw[1]), (scenario.key[0].upper(), raw[1]))
    assert len(set(keys)) == 3
    extra = tuple(
        replace(
            original,
            source_row=maximum + offset,
            key=key,
            raw_fields={
                **original.raw_fields,
                (KEYS_A if source is Source.A else KEYS_B)[0]: key[0],
            },
        )
        for offset, key in enumerate(keys, start=1)
    )
    return (*records, *extra), (original, *extra)


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_large_collision_preflight_blocks_without_losing_sparse_evidence(
    large_normalized, mode, monkeypatch
) -> None:
    pair, records_a, records_b = large_normalized
    a, originals_a = _with_collision(records_a, pair.scenarios[7], Source.A)
    b, originals_b = _with_collision(records_b, pair.scenarios[8], Source.B)
    before = _snapshot((*originals_a, *originals_b))

    def forbidden(*args, **kwargs):
        pytest.fail("Grouped mode and large inputs must never bypass preflight")

    monkeypatch.setattr(engine, "_classify", forbidden)
    monkeypatch.setattr(engine, "ReconciliationResult", forbidden)
    with pytest.raises(NormalizationCollisionError) as caught:
        reconcile(reversed(a), reversed(b), mode=mode, key_normalization=CONFIG)
    for collisions, originals, scenario in (
        (caught.value.collisions_a, originals_a, pair.scenarios[7]),
        (caught.value.collisions_b, originals_b, pair.scenarios[8]),
    ):
        assert len(collisions) == 1
        assert collisions[0].normalized_key == scenario.key
        assert [item.original_key for item in collisions[0].originals] == sorted(
            {row.key for row in originals}
        )
        retained = [row for item in collisions[0].originals for row in item.records]
        assert Counter(map(id, retained)) == Counter(map(id, originals))
        for item in collisions[0].originals:
            assert [row.source_row for row in item.records] == sorted(
                row.source_row for row in item.records
            )
    assert _snapshot((*originals_a, *originals_b)) == before


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("source", [Source.A, Source.B])
@pytest.mark.parametrize("component", [0, 1])
def test_normalization_erasing_risky_component_blocks_with_logical_record_context(
    mode, source, component, monkeypatch
) -> None:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("Vendor", "Invoice", "Memo", "Amount"))
    writer.writerow(("Valid", "1", "multiline\noriginal", "10"))
    key = ("—…!!!", "42") if component == 0 else ("Vendor", "---")
    writer.writerow((*key, "malformed selected identifier", "0"))
    records = ingest_csv(
        output.getvalue(), source=source, key_columns=("Vendor", "Invoice"), amount_column="Amount"
    )
    config = KeyNormalizationConfig(
        (
            CONFIG.component_rules[0],
            replace(CONFIG.component_rules[1], remove_punctuation=True),
        )
    )
    before = _snapshot(records)

    def forbidden(*args, **kwargs):
        pytest.fail("Blank normalization must block without exact fallback or partial result")

    monkeypatch.setattr(engine, "_classify", forbidden)
    monkeypatch.setattr(engine, "ReconciliationResult", forbidden)
    with pytest.raises(KeyNormalizationError) as caught:
        reconcile(
            records if source is Source.A else (),
            records if source is Source.B else (),
            mode=mode,
            key_normalization=config,
        )
    message = str(caught.value)
    assert f"File {source.value} source row 3" in message
    assert f"key component {component + 1}" in message
    assert repr(key[component]) in message and "original value" in message
    assert _snapshot(records) == before


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_benchmark_validates_normalization_with_configuration_and_summary(mode) -> None:
    measured = measure(100, 42, TOLERANCE, mode=mode, workload="normalization")
    assert (measured.rows_a, measured.rows_b) == (187, 186)
    assert measured.exceptions == (50 if mode is ReconciliationMode.UNIQUE else 28)
    assert (
        measured.summary
        == generate_pair(100, workload="normalization")
        .expected(TOLERANCE, mode=mode.value)
        .summary()
    )
    assert measured.total >= measured.reconciliation >= 0


@pytest.mark.parametrize("corruption", ["configuration", "raw_key", "raw_fields", "row_order"])
def test_normalized_benchmark_verifier_rejects_corrupted_evidence(
    realistic_normalized, corruption
) -> None:
    pair, a, b = realistic_normalized
    result, report = _run(pair, a, b, ReconciliationMode.GROUPED_BY_KEY)
    if corruption == "configuration":
        result = replace(result, key_normalization=KeyNormalizationConfig(CONFIG.component_rules))
        message = "exact normalization configuration"
    else:
        findings = list(result.findings)
        index = next(index for index, finding in enumerate(findings) if len(finding.rows_a) > 1)
        finding = findings[index]
        rows = list(finding.rows_a)
        if corruption == "raw_key":
            rows[0] = replace(rows[0], key=("tampered", rows[0].key[1]))
            message = "Original raw key changed"
        elif corruption == "raw_fields":
            rows[0] = replace(rows[0], raw_fields={**rows[0].raw_fields, KEYS_A[0]: "tampered"})
            message = "Original raw key evidence changed"
        else:
            rows.reverse()
            message = "Evidence source rows are not sorted"
        findings[index] = replace(finding, rows_a=tuple(rows))
        result = replace(result, findings=tuple(findings))
    with pytest.raises(AssertionError, match=message):
        verify_result(
            a,
            b,
            result,
            report,
            pair.expected(TOLERANCE, mode="grouped_by_key"),
            key_normalization=CONFIG,
        )
