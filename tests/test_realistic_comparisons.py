"""Independent realistic acceptance and CI-sized secondary-comparison stress."""

import csv
import json
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from io import StringIO

import pytest

import tallydiff.engine as engine
from scripts.benchmark import comparison_mapping, verify_comparison_result
from scripts.synthetic_data import (
    COMPARISON_HEADERS_A,
    COMPARISON_HEADERS_B,
    COMPARISON_MAPPINGS,
    generate_pair,
)
from tallydiff import (
    ComparisonFieldMapping,
    FindingCategory,
    IngestionError,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    NormalizationCollisionError,
    ReconciliationMode,
    Source,
    export_exceptions_csv,
    export_mapping_profile,
    ingest_csv,
    ingest_xlsx,
    load_mapping_profile,
    reconcile,
)
from tallydiff.presentation import review_exceptions

TOLERANCE = Decimal("0.0100")
FORMATS = (("csv", "csv"), ("csv", "xlsx"), ("xlsx", "csv"), ("xlsx", "xlsx"))


def _csv(headers, rows):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def _literal_text(workbook):
    # Real exports may contain formula-looking text; make their cell type explicit.
    for worksheet in workbook:
        for row in worksheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"


def _ingest_pair(pair, *, formats=("csv", "csv"), xlsx_bytes=None, mapping=None):
    mapping = mapping or comparison_mapping()
    inputs = []
    for source, text, kind, keys, amount, comparisons in (
        (
            Source.A,
            pair.csv_a,
            formats[0],
            mapping.keys_a,
            mapping.amount_a,
            mapping.comparisons_a,
        ),
        (
            Source.B,
            pair.csv_b,
            formats[1],
            mapping.keys_b,
            mapping.amount_b,
            mapping.comparisons_b,
        ),
    ):
        options = {
            "source": source,
            "key_columns": keys,
            "amount_column": amount,
            "comparison_columns": comparisons,
        }
        if kind == "csv":
            records = ingest_csv(text, **options)
        else:
            rows = list(csv.reader(StringIO(text, newline="")))
            data = xlsx_bytes({"Ledger é": rows}, configure=_literal_text)
            records = ingest_xlsx(data, worksheet="Ledger é", **options)
        raw = list(csv.DictReader(StringIO(text, newline="")))
        assert [dict(record.raw_fields) for record in records] == raw
        assert [record.source_row for record in records] == list(range(2, len(raw) + 2))
        inputs.append(records)
    return tuple(inputs)


def _run(pair, inputs, mode, *, mapping=None):
    mapping = mapping or comparison_mapping()
    truth = pair.expected(TOLERANCE, mode=mode.value)
    result = reconcile(
        *inputs,
        amount_tolerance=TOLERANCE,
        mode=mode,
        comparison_fields=mapping.comparison_fields,
    )
    report = export_exceptions_csv(result)
    verify_comparison_result(*inputs, result, report, truth)
    return result, report, truth


def _semantics(result):
    return tuple(
        (
            finding.key,
            finding.category.value,
            finding.amount_a,
            finding.amount_b,
            finding.delta,
            finding.has_secondary_mismatch,
            finding.is_exception,
            tuple(
                (
                    (comparison.mapping.file_a, comparison.mapping.file_b),
                    comparison.values_a,
                    comparison.values_b,
                    comparison.status.value,
                )
                for comparison in finding.field_comparisons
            ),
        )
        for finding in result.findings
    )


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("formats", FORMATS)
def test_realistic_comparison_schema_all_formats_and_v4_profile(mode, formats, xlsx_bytes):
    pair = generate_pair(300, seed=314, workload="comparison")
    mapping = comparison_mapping()
    data = export_mapping_profile(mapping, amount_tolerance=TOLERANCE, reconciliation_mode=mode)
    document = json.loads(data)
    assert document["version"] == 4
    assert document["comparison_fields"] == [
        {"file_a": a, "file_b": b} for a, b in COMPARISON_MAPPINGS
    ]
    assert document["amount_tolerance"] == "0.0100"
    assert document["reconciliation_mode"] == mode.value
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
    assert not any(scenario.key[0].encode() in data for scenario in pair.scenarios)
    profile = load_mapping_profile(data)
    profile.validate_columns(COMPARISON_HEADERS_A, COMPARISON_HEADERS_B)
    assert profile.mapping == mapping
    assert profile.amount_tolerance.as_tuple() == TOLERANCE.as_tuple()
    assert profile.reconciliation_mode is mode
    assert profile.key_normalization is None
    inputs = _ingest_pair(pair, formats=formats, xlsx_bytes=xlsx_bytes, mapping=profile.mapping)
    result, report, truth = _run(pair, inputs, mode, mapping=profile.mapping)
    assert len(result.findings) == 300
    assert len(result.comparison_fields) == 3
    assert [finding.key for finding in result.exceptions] == list(truth.exception_keys)
    assert any(
        value.startswith("=")
        for records in inputs
        for record in records
        for value in record.raw_fields.values()
    )
    assert any(
        "\n" in value for records in inputs for r in records for value in r.raw_fields.values()
    )
    assert result == reconcile(
        *(reversed(records) for records in inputs),
        amount_tolerance=TOLERANCE,
        mode=mode,
        comparison_fields=profile.mapping.comparison_fields,
    )
    assert export_exceptions_csv(result) == report


@pytest.fixture(scope="module")
def ci_comparison_exports():
    pair = generate_pair(20_000, seed=2026, workload="comparison")
    return pair, _ingest_pair(pair)


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_twenty_thousand_groups_full_comparison_integrity(ci_comparison_exports, mode):
    pair, inputs = ci_comparison_exports
    mapping = comparison_mapping()
    profile = load_mapping_profile(
        export_mapping_profile(mapping, amount_tolerance=TOLERANCE, reconciliation_mode=mode)
    )
    assert profile.mapping.comparison_fields == mapping.comparison_fields
    assert profile.amount_tolerance.as_tuple() == TOLERANCE.as_tuple()
    assert profile.reconciliation_mode is mode
    result, report, truth = _run(pair, inputs, mode, mapping=profile.mapping)
    assert len(result.findings) == 20_000
    assert len(result.comparison_fields) == 3
    assert result.exceptions and result.tolerated_findings
    assert any(f.category is FindingCategory.EXACT_MATCH for f in result.exceptions)
    assert any(f.category is FindingCategory.WITHIN_TOLERANCE for f in result.exceptions)
    assert any(len(f.rows_a) > 1 or len(f.rows_b) > 1 for f in result.findings)
    assert set(truth.category_counts) == {category.value for category in FindingCategory}
    # Compute both identities directly from independently constructed exact units.
    with localcontext() as context:
        context.prec = 80
        expected_control = truth.total_a - truth.total_b
        assert expected_control == sum((group.delta for group in truth.groups), Decimal(0))
        assert (
            expected_control
            == sum((group.delta for group in truth.groups if group.is_exception), Decimal(0))
            + truth.tolerated_delta_total
        )
        assert result.control_difference == expected_control == result.finding_delta_sum
    assert export_exceptions_csv(result) == report


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_physical_shuffle_changes_row_references_without_changing_comparison_truth(mode):
    outputs = []
    trace_positions = []
    for shuffle_seed in (11, 73):
        pair = generate_pair(500, seed=42, shuffle_seed=shuffle_seed, workload="comparison")
        inputs = _ingest_pair(pair)
        result, _, truth = _run(pair, inputs, mode)
        outputs.append(_semantics(result))
        trace_positions.append(
            tuple((r.source.value, r.source_row, r.key) for records in inputs for r in records)
        )
        assert [f.key for f in result.findings] == sorted(group.key for group in truth.groups)
    assert outputs[0] == outputs[1]
    assert trace_positions[0] != trace_positions[1]


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_review_filters_do_not_change_complete_secondary_exception_export(mode):
    pair = generate_pair(300, seed=42, workload="comparison")
    inputs = _ingest_pair(pair)
    result, report, truth = _run(pair, inputs, mode)
    planned = tuple(group for group in truth.groups if group.is_exception)
    assert [f.key for f in review_exceptions(result.exceptions)] == [g.key for g in planned]
    assert [
        f.key for f in review_exceptions(result.exceptions, secondary_mismatches_only=True)
    ] == [g.key for g in planned if g.has_secondary_mismatch]
    for category in FindingCategory:
        assert [f.key for f in review_exceptions(result.exceptions, categories=(category,))] == [
            g.key for g in planned if g.category == category.value
        ]
    for minimum in (Decimal("0"), Decimal("0.01"), Decimal("100000000000000000000")):
        assert [f.key for f in review_exceptions(result.exceptions, minimum_abs_delta=minimum)] == [
            g.key for g in planned if abs(g.delta) >= minimum
        ]
    for order, sign in (("absolute_delta_asc", 1), ("absolute_delta_desc", -1)):
        expected = sorted(planned, key=lambda g: (sign * abs(g.delta), g.key))
        assert [f.key for f in review_exceptions(result.exceptions, sort_order=order)] == [
            g.key for g in expected
        ]
    assert review_exceptions(result.exceptions, query="not-a-real-comparison-key") == ()
    assert review_exceptions(result.exceptions, categories=()) == ()
    assert export_exceptions_csv(result) == report
    verify_comparison_result(*inputs, result, report, truth)


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_key_normalization_joins_identifiers_while_secondary_evidence_stays_original(mode):
    mapping = replace(
        comparison_mapping(),
        comparison_fields=(ComparisonFieldMapping("Vendor ID", "Supplier"),),
    )
    text_a = _csv(COMPARISON_HEADERS_A, [("ACME-01", "001", "Sales", "USD", "2026-01-02", "1000")])
    text_b = _csv(COMPARISON_HEADERS_B, [("ACME01", "001", "Sales", "USD", "2026-01-02", "1000")])
    inputs = (
        ingest_csv(
            text_a,
            source=Source.A,
            key_columns=mapping.keys_a,
            amount_column=mapping.amount_a,
            comparison_columns=mapping.comparisons_a,
        ),
        ingest_csv(
            text_b,
            source=Source.B,
            key_columns=mapping.keys_b,
            amount_column=mapping.amount_b,
            comparison_columns=mapping.comparisons_b,
        ),
    )
    configuration = KeyNormalizationConfig(
        (KeyNormalizationRules(remove_punctuation=True), KeyNormalizationRules())
    )
    profile_data = export_mapping_profile(
        mapping,
        amount_tolerance=TOLERANCE,
        reconciliation_mode=mode,
        key_normalization=configuration,
    )
    profile = load_mapping_profile(profile_data)
    assert profile.mapping == mapping
    assert profile.amount_tolerance.as_tuple() == TOLERANCE.as_tuple()
    assert profile.reconciliation_mode is mode
    assert profile.key_normalization == configuration
    assert json.loads(profile_data)["key_normalization"] == [
        {
            "casefold": False,
            "collapse_whitespace": False,
            "remove_punctuation": True,
            "strip_leading_zeros": False,
        },
        {
            "casefold": False,
            "collapse_whitespace": False,
            "remove_punctuation": False,
            "strip_leading_zeros": False,
        },
    ]
    result = reconcile(
        *inputs,
        amount_tolerance=profile.amount_tolerance,
        mode=profile.reconciliation_mode,
        key_normalization=profile.key_normalization,
        comparison_fields=profile.mapping.comparison_fields,
    )
    finding = result.findings[0]
    assert finding.key == ("ACME01", "001")
    assert finding.category is FindingCategory.EXACT_MATCH and finding.delta == 0
    assert finding.is_exception and finding.has_secondary_mismatch
    comparison = finding.field_comparisons[0]
    assert (comparison.values_a, comparison.values_b, comparison.status.value) == (
        ("ACME-01",),
        ("ACME01",),
        "mismatch",
    )
    assert finding.rows_a[0] is inputs[0][0] and finding.rows_b[0] is inputs[1][0]
    assert finding.rows_a[0].key == ("ACME-01", "001")
    assert finding.rows_a[0].raw_fields["Vendor ID"] == "ACME-01"
    row = next(csv.DictReader(StringIO(export_exceptions_csv(result).decode(), newline="")))
    assert row["Category"] == "Exact match" and Decimal(row["Delta (A - B)"]) == 0
    assert json.loads(row["Comparison 1 File A values — Vendor ID"]) == ["ACME-01"]


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("source", list(Source))
def test_same_source_normalization_collision_blocks_before_comparison_results(
    mode, source, monkeypatch
):
    mapping = comparison_mapping()
    row = ("ACME-01", "001", "Sales", "USD", "2026-01-02", "1000")
    convergent = ("ACME01", *row[1:])
    inputs = []
    for side, headers, keys, amount, comparisons in (
        (Source.A, COMPARISON_HEADERS_A, mapping.keys_a, mapping.amount_a, mapping.comparisons_a),
        (Source.B, COMPARISON_HEADERS_B, mapping.keys_b, mapping.amount_b, mapping.comparisons_b),
    ):
        records = ingest_csv(
            _csv(headers, (row, convergent) if side is source else (convergent,)),
            source=side,
            key_columns=keys,
            amount_column=amount,
            comparison_columns=comparisons,
        )
        inputs.append(records)

    def forbidden(*args, **kwargs):
        pytest.fail("Normalization collision must block before any result or classification")

    monkeypatch.setattr(engine, "_classify", forbidden)
    monkeypatch.setattr(engine, "ReconciliationResult", forbidden)
    with pytest.raises(NormalizationCollisionError) as caught:
        reconcile(
            *inputs,
            mode=mode,
            key_normalization=KeyNormalizationConfig(
                (KeyNormalizationRules(remove_punctuation=True), KeyNormalizationRules())
            ),
            comparison_fields=mapping.comparison_fields,
        )
    collisions = caught.value.collisions_a if source is Source.A else caught.value.collisions_b
    assert len(collisions) == 1 and collisions[0].normalized_key == ("ACME01", "001")
    originals = collisions[0].originals
    assert [item.original_key for item in originals] == [("ACME-01", "001"), ("ACME01", "001")]
    retained = [record for original in originals for record in original.records]
    original_records = inputs[0 if source is Source.A else 1]
    assert Counter(map(id, retained)) == Counter(map(id, original_records))
    assert [
        r.raw_fields["Department" if source is Source.A else "Cost Center"] for r in retained
    ] == [
        "Sales",
        "Sales",
    ]


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_profile_versions_imply_zero_comparisons_for_current_workload(version):
    document = json.loads(
        export_mapping_profile(
            comparison_mapping(),
            amount_tolerance=TOLERANCE,
            reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY,
        )
    )
    document["version"] = version
    del document["comparison_fields"]
    if version < 3:
        del document["key_normalization"]
    if version == 1:
        del document["reconciliation_mode"]
    profile = load_mapping_profile(json.dumps(document))
    assert profile.mapping.comparison_fields == ()
    assert profile.mapping.keys_a == COMPARISON_HEADERS_A[:2]
    assert profile.mapping.keys_b == COMPARISON_HEADERS_B[:2]
    assert profile.mapping.amount_a == "Amount" and profile.mapping.amount_b == "Gross Amount"
    assert profile.amount_tolerance.as_tuple() == TOLERANCE.as_tuple()
    assert profile.key_normalization is None
    assert profile.reconciliation_mode is (
        ReconciliationMode.UNIQUE if version == 1 else ReconciliationMode.GROUPED_BY_KEY
    )


@pytest.mark.parametrize(
    ("csv_value", "xlsx_value", "observed", "status"),
    [
        ("100.00", 100, "100", "mismatch"),
        ("100", "100", "100", "match"),
        ("2026-01-02", date(2026, 1, 2), "2026-01-02", "match"),
        ("2026-01-02", datetime(2026, 1, 2), "2026-01-02T00:00:00", "mismatch"),
    ],
)
@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_csv_and_typed_xlsx_values_compare_exact_observed_representations(
    csv_value, xlsx_value, observed, status, mode, xlsx_bytes
):
    mapping = comparison_mapping()
    a = ingest_csv(
        _csv(COMPARISON_HEADERS_A, [("001", "I1", csv_value, "EUR", "2026-01-02", "1000")]),
        source=Source.A,
        key_columns=mapping.keys_a,
        amount_column=mapping.amount_a,
        comparison_columns=mapping.comparisons_a,
    )
    b = ingest_xlsx(
        xlsx_bytes(
            {
                "Typed evidence": [
                    COMPARISON_HEADERS_B,
                    ("001", "I1", xlsx_value, "EUR", "2026-01-02", "1000"),
                ]
            }
        ),
        source=Source.B,
        worksheet="Typed evidence",
        key_columns=mapping.keys_b,
        amount_column=mapping.amount_b,
        comparison_columns=mapping.comparisons_b,
    )
    finding = reconcile(a, b, mode=mode, comparison_fields=mapping.comparison_fields).findings[0]
    assert finding.category is FindingCategory.EXACT_MATCH and finding.delta == 0
    assert finding.field_comparisons[0].values_a == (csv_value,)
    assert finding.field_comparisons[0].values_b == (observed,)
    assert finding.field_comparisons[0].status.value == status
    assert all(c.status.value == "match" for c in finding.field_comparisons[1:])
    assert b[0].raw_fields["Cost Center"] == observed
    assert finding.is_exception is (status == "mismatch")


@pytest.mark.parametrize("value", ["=SUM(1,2)", "#N/A"])
@pytest.mark.parametrize("source", list(Source))
def test_actual_selected_xlsx_formula_or_error_is_rejected_with_full_context(
    value, source, xlsx_bytes
):
    mapping = comparison_mapping()
    headers = COMPARISON_HEADERS_A if source is Source.A else COMPARISON_HEADERS_B
    keys = mapping.keys_a if source is Source.A else mapping.keys_b
    amount = mapping.amount_a if source is Source.A else mapping.amount_b
    comparisons = mapping.comparisons_a if source is Source.A else mapping.comparisons_b
    data = xlsx_bytes(
        {"Selected evidence": [headers, ("001", "I1", value, "EUR", "2026-01-02", "1000")]}
    )
    with pytest.raises(IngestionError) as caught:
        ingest_xlsx(
            data,
            source=source,
            worksheet="Selected evidence",
            key_columns=keys,
            amount_column=amount,
            comparison_columns=comparisons,
        )
    message = str(caught.value)
    assert f"File {source.value}" in message
    assert "Selected evidence" in message and "row 2" in message
    assert comparisons[0] in message and value in message


def test_high_cardinality_distinct_sets_keep_all_rows_and_export_every_original_value():
    mapping = comparison_mapping()
    distinct = (
        "",
        "é",
        "e\u0301",
        "销售",
        "=1+1",
        "embedded\nnewline",
        *(f"Dept {i:04d}" for i in range(1_250)),
    )
    rows_a = tuple(
        (
            "HIGH",
            "H001",
            value,
            ("USD", "EUR", "")[index % 3],
            "2026-01-02",
            ("10", "-10", "0", "0")[index % 4],
        )
        for index, value in enumerate(distinct)
    )
    rows_b = (
        *reversed(rows_a),
        ("HIGH", "H001", "Only File B", "USD", "2026-01-02", "0"),
        ("HIGH", "H001", "Dept 0001", "USD", "2026-01-02", "0"),
    )
    a = ingest_csv(
        _csv(COMPARISON_HEADERS_A, rows_a),
        source=Source.A,
        key_columns=mapping.keys_a,
        amount_column=mapping.amount_a,
        comparison_columns=mapping.comparisons_a,
    )
    b = ingest_csv(
        _csv(COMPARISON_HEADERS_B, rows_b),
        source=Source.B,
        key_columns=mapping.keys_b,
        amount_column=mapping.amount_b,
        comparison_columns=mapping.comparisons_b,
    )
    result = reconcile(
        a, b, mode=ReconciliationMode.GROUPED_BY_KEY, comparison_fields=mapping.comparison_fields
    )
    finding = result.findings[0]
    assert finding.category is FindingCategory.EXACT_MATCH and finding.delta == 0
    assert finding.is_exception and finding.has_secondary_mismatch
    expected_a = tuple(sorted(distinct))
    expected_b = tuple(sorted((*distinct, "Only File B")))
    assert len(expected_a) == 1_256 and len(expected_b) == 1_257
    assert finding.field_comparisons[0].values_a == expected_a
    assert finding.field_comparisons[0].values_b == expected_b
    assert finding.field_comparisons[0].status.value == "mismatch"
    assert tuple(c.values_a for c in finding.field_comparisons[1:]) == (
        ("", "EUR", "USD"),
        ("2026-01-02",),
    )
    assert all(c.status.value == "match" for c in finding.field_comparisons[1:])
    assert finding.rows_a == a and finding.rows_b == b
    assert Counter(map(id, (*finding.rows_a, *finding.rows_b))) == Counter(map(id, (*a, *b)))
    assert result.total_a == sum((Decimal(row[-1]) for row in rows_a), Decimal(0))
    assert result.total_a == result.total_b
    report = export_exceptions_csv(result)
    assert report == export_exceptions_csv(result)
    exported = list(csv.DictReader(StringIO(report.decode(), newline="")))
    assert len(exported) == 1
    row = exported[0]
    assert json.loads(row["Comparison 1 File A values — Department"]) == list(expected_a)
    assert json.loads(row["Comparison 1 File B values — Cost Center"]) == list(expected_b)
    assert row["Comparison 1 status"] == "mismatch"
    assert row["File A records"] == "; ".join(str(n) for n in range(2, len(a) + 2))
    assert row["File B records"] == "; ".join(str(n) for n in range(2, len(b) + 2))


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_zero_comparison_export_remains_exact_historical_seven_column_bytes(mode):
    a = ingest_csv(
        "id,amount\r\nA,100.00\r\nB,0.00\r\nD,10.00\r\n",
        source=Source.A,
        key_columns=("id",),
        amount_column="amount",
    )
    b = ingest_csv(
        "ref,gross\r\nA,99.00\r\nC,5.00\r\nD,10.00\r\n",
        source=Source.B,
        key_columns=("ref",),
        amount_column="gross",
    )
    result = reconcile(a, b, mode=mode)
    expected = (
        b"Category,Matching key,File A amount,File B amount,Delta (A - B),"
        b"File A records,File B records\r\n"
        b"Amount mismatch,A,100.00,99.00,+1.00,2,2\r\n"
        b"File A only,B,0.00,,0.00,3,\r\n"
        b"File B only,C,,5.00,-5.00,,3\r\n"
    )
    assert result.comparison_fields == ()
    assert export_exceptions_csv(result) == expected == export_exceptions_csv(result)
