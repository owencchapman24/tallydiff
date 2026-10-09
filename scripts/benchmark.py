"""Developer benchmark of synthetic exports; no application behavior or timing thresholds."""

import argparse
import cProfile
import csv
import gc
import json
import platform
import pstats
import time
import tracemalloc
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, localcontext
from io import StringIO

from scripts.synthetic_data import (
    AMOUNT_A,
    AMOUNT_B,
    COMPARISON_AMOUNT_A,
    COMPARISON_AMOUNT_B,
    COMPARISON_KEYS_A,
    COMPARISON_KEYS_B,
    COMPARISON_MAPPINGS,
    KEYS_A,
    KEYS_B,
    MODES,
    NORMALIZATION_RULES,
    WORKLOADS,
    ComparisonGroundTruth,
    GroundTruth,
    decimal_units,
    generate_pair,
)
from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    export_exceptions_csv,
    ingest_csv,
    inspect_csv_columns,
    reconcile,
)


@dataclass(frozen=True, slots=True)
class Measurement:
    rows_a: int
    rows_b: int
    exceptions: int
    generation: float
    ingestion: float
    reconciliation: float
    export: float
    verification: float
    total: float
    summary: dict[str, object]


def normalization_config(workload: str) -> KeyNormalizationConfig | None:
    """Build product configuration only in the runner, never in the fixture oracle."""

    if workload == "normalization":
        return KeyNormalizationConfig(
            tuple(KeyNormalizationRules(**rules) for rules in NORMALIZATION_RULES)
        )
    return None


def measure(
    groups: int,
    seed: int,
    tolerance: Decimal,
    *,
    mode: ReconciliationMode = ReconciliationMode.UNIQUE,
    workload: str = "grouped",
) -> Measurement:
    """Time the full path; independent verification is reported as its own phase."""

    started = time.perf_counter()
    pair = generate_pair(groups, seed=seed, workload=workload)
    generated = time.perf_counter()
    columns_a = inspect_csv_columns(pair.csv_a, source=Source.A)
    columns_b = inspect_csv_columns(pair.csv_b, source=Source.B)
    mapping = (
        comparison_mapping()
        if workload == "comparison"
        else ColumnMapping(tuple(zip(KEYS_A, KEYS_B, strict=True)), AMOUNT_A, AMOUNT_B)
    )
    if mapping.problem(columns_a, columns_b):
        raise AssertionError("Synthetic schema is not compatible with its explicit mapping")
    a = ingest_csv(
        pair.csv_a,
        source=Source.A,
        key_columns=mapping.keys_a,
        amount_column=mapping.amount_a,
        comparison_columns=mapping.comparisons_a,
    )
    b = ingest_csv(
        pair.csv_b,
        source=Source.B,
        key_columns=mapping.keys_b,
        amount_column=mapping.amount_b,
        comparison_columns=mapping.comparisons_b,
    )
    ingested = time.perf_counter()
    configuration = normalization_config(workload)
    result = reconcile(
        a,
        b,
        amount_tolerance=tolerance,
        mode=mode,
        key_normalization=configuration,
        comparison_fields=mapping.comparison_fields,
    )
    reconciled = time.perf_counter()
    report = export_exceptions_csv(result)
    exported = time.perf_counter()

    truth = pair.expected(tolerance, mode=mode.value)
    verifier = verify_comparison_result if workload == "comparison" else verify_result
    verifier(a, b, result, report, truth, key_normalization=configuration)
    summary = truth.summary()
    finished = time.perf_counter()
    return Measurement(
        len(a),
        len(b),
        len(truth.exception_keys),
        generated - started,
        ingested - generated,
        reconciled - ingested,
        exported - reconciled,
        finished - exported,
        finished - started,
        summary,
    )


def comparison_mapping() -> ColumnMapping:
    """Build the workload's three ordered asymmetric mappings on the actual side."""

    return ColumnMapping(
        tuple(zip(COMPARISON_KEYS_A, COMPARISON_KEYS_B, strict=True)),
        COMPARISON_AMOUNT_A,
        COMPARISON_AMOUNT_B,
        comparison_fields=tuple(ComparisonFieldMapping(*names) for names in COMPARISON_MAPPINGS),
    )


# Independent expectations for the public CSV protocol, deliberately not imported
# from product presentation/export helpers.
_COMPARISON_CATEGORY_LABELS = {
    "exact_match": "Exact match",
    "within_tolerance": "Within tolerance",
    "amount_mismatch": "Amount mismatch",
    "a_only": "File A only",
    "b_only": "File B only",
    "duplicate_ambiguous": "Duplicate / ambiguous",
}
_COMPARISON_BASE_COLUMNS = (
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
    "Secondary differences",
)


def _constructed_money_places(records: tuple, column: str) -> int:
    """Read only display precision from independently constructed financial strings."""

    return max(
        (
            len(
                "".join(
                    character
                    for character in record.raw_fields[column].split(".")[1]
                    if character.isdigit()
                )
            )
            for record in records
        ),
        default=0,
    )


def _integer_money_text(units: int, places: int, *, signed: bool = False) -> str:
    """Render integer ten-thousandths exactly at construction-specified precision."""

    if not 0 <= places <= 4 or units % (10 ** (4 - places)):
        raise AssertionError("Construction money cannot be represented at its stated precision")
    whole, fraction = divmod(abs(units), 10_000)
    sign = "-" if units < 0 else "+" if signed and units > 0 else ""
    text = f"{sign}{whole}"
    return text + "." + f"{fraction:04d}"[:places] if places else text


def _expected_comparison_export(truth: ComparisonGroundTruth) -> bytes:
    """Serialize construction truth, never observed findings or product helpers."""

    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    columns = list(_COMPARISON_BASE_COLUMNS)
    for index, (column_a, column_b) in enumerate(truth.comparison_fields, start=1):
        columns.extend(
            (
                f"Comparison {index} File A values — {column_a}",
                f"Comparison {index} File B values — {column_b}",
                f"Comparison {index} status",
            )
        )
    writer.writerow(columns)
    for group in truth.groups:
        if not group.is_exception:
            continue
        places_a = _constructed_money_places(group.records_a, COMPARISON_AMOUNT_A)
        places_b = _constructed_money_places(group.records_b, COMPARISON_AMOUNT_B)
        row = [
            _COMPARISON_CATEGORY_LABELS[group.category],
            " / ".join(group.key),
            _integer_money_text(group.units_a, places_a) if group.records_a else "",
            _integer_money_text(group.units_b, places_b) if group.records_b else "",
            _integer_money_text(group.delta_units, max(places_a, places_b), signed=True),
            "; ".join(str(record.source_row) for record in group.records_a),
            "; ".join(str(record.source_row) for record in group.records_b),
            "; ".join(
                f"Comparison {index}: {comparison.mapping[0]} ↔ {comparison.mapping[1]}"
                for index, comparison in enumerate(group.field_comparisons, start=1)
                if comparison.status == "mismatch"
            ),
        ]
        for comparison in group.field_comparisons:
            row.extend(
                (
                    json.dumps(comparison.values_a, ensure_ascii=False),
                    json.dumps(comparison.values_b, ensure_ascii=False),
                    comparison.status,
                )
            )
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def verify_comparison_result(
    a: tuple[SourceRecord, ...],
    b: tuple[SourceRecord, ...],
    result: ReconciliationResult,
    report: bytes,
    truth: ComparisonGroundTruth,
    *,
    key_normalization: KeyNormalizationConfig | None = None,
) -> None:
    """Check every comparison and source row against independent construction truth."""

    if result.key_normalization is not key_normalization:
        raise AssertionError("Result does not retain the exact normalization configuration used")
    if result.mode.value != truth.mode:
        raise AssertionError("Reconciliation mode differs from independent construction")
    if tuple((mapping.file_a, mapping.file_b) for mapping in result.comparison_fields) != (
        truth.comparison_fields
    ):
        raise AssertionError("Comparison mapping count/order differs from independent construction")
    if (len(a), len(b)) != (truth.record_count_a, truth.record_count_b):
        raise AssertionError("Record counts differ from independent construction")
    if len(result.findings) != len(truth.groups):
        raise AssertionError("Key-group count differs from independent construction")
    if Counter(f.category.value for f in result.findings) != Counter(truth.category_counts):
        raise AssertionError("Category counts differ from independent construction")
    if (
        result.total_a,
        result.total_b,
        result.control_difference,
        result.tolerated_delta_total,
    ) != (truth.total_a, truth.total_b, truth.control_difference, truth.tolerated_delta_total):
        raise AssertionError("Totals differ from independent integer-based construction")
    if tuple(f.key for f in result.exceptions) != truth.exception_keys:
        raise AssertionError("Exception key order differs from independent construction")
    if tuple(f.key for f in result.tolerated_findings) != truth.tolerated_keys:
        raise AssertionError("Accepted-tolerance key order differs from independent construction")
    if tuple(f.key for f in result.secondary_mismatch_findings) != tuple(
        group.key for group in truth.groups if group.has_secondary_mismatch
    ):
        raise AssertionError("Secondary mismatch key order differs from independent construction")

    original_records = {}
    expected_identities = Counter()
    for observed, attribute in ((a, "records_a"), (b, "records_b")):
        expected_records = sorted(
            (record for group in truth.groups for record in getattr(group, attribute)),
            key=lambda record: record.source_row,
        )
        for row, expected in zip(observed, expected_records, strict=True):
            identity = (row.source.value, row.source_row)
            expected_identity = (expected.source, expected.source_row)
            if (
                identity != expected_identity
                or row.key != expected.key
                or row.amount != expected.amount
                or dict(row.raw_fields) != expected.raw_fields
            ):
                raise AssertionError(
                    f"Ingested source evidence differs from independent construction: "
                    f"{expected_identity}"
                )
            if identity in original_records:
                raise AssertionError(f"Source identity occurs more than once: {identity}")
            original_records[identity] = row
            expected_identities[expected_identity] += 1

    accounted = Counter()
    for finding, expected in zip(result.findings, truth.groups, strict=True):
        if (
            finding.key,
            finding.category.value,
            len(finding.rows_a),
            len(finding.rows_b),
            finding.amount_a,
            finding.amount_b,
            finding.delta,
            finding.has_secondary_mismatch,
            finding.is_exception,
        ) != (
            expected.key,
            expected.category,
            expected.rows_a,
            expected.rows_b,
            decimal_units(expected.units_a),
            decimal_units(expected.units_b),
            expected.delta,
            expected.has_secondary_mismatch,
            expected.is_exception,
        ):
            raise AssertionError(f"Group differs from independent construction: {expected.key}")
        if len(finding.field_comparisons) != len(expected.field_comparisons):
            raise AssertionError(f"Comparison count differs: {expected.key}")
        for actual_comparison, expected_comparison in zip(
            finding.field_comparisons, expected.field_comparisons, strict=True
        ):
            if (
                (actual_comparison.mapping.file_a, actual_comparison.mapping.file_b),
                actual_comparison.values_a,
                actual_comparison.values_b,
                actual_comparison.status.value,
            ) != (
                expected_comparison.mapping,
                expected_comparison.values_a,
                expected_comparison.values_b,
                expected_comparison.status,
            ):
                raise AssertionError(f"Field comparison differs: {expected.key}")
        for records, expected_records in (
            (finding.rows_a, expected.records_a),
            (finding.rows_b, expected.records_b),
        ):
            if [row.source_row for row in records] != sorted(row.source_row for row in records):
                raise AssertionError(f"Evidence source rows are not sorted: {expected.key}")
            for row, expected_record in zip(records, expected_records, strict=True):
                identity = (row.source.value, row.source_row)
                if (
                    identity != (expected_record.source, expected_record.source_row)
                    or row.key != expected_record.key
                    or row.amount != expected_record.amount
                    or dict(row.raw_fields) != expected_record.raw_fields
                ):
                    raise AssertionError(f"Finding source evidence differs: {expected.key}")
                if row is not original_records.get(identity):
                    raise AssertionError(f"Original source object was replaced: {expected.key}")
                accounted[identity] += 1
    if accounted != expected_identities or any(count != 1 for count in accounted.values()):
        raise AssertionError("Source rows were not accounted for exactly once")

    # Precision is derived from independent integer construction, not product summation.
    with localcontext() as context:
        context.prec = (
            max(
                len(str(abs(group.units_a))) + len(str(abs(group.units_b)))
                for group in truth.groups
            )
            + len(str(len(truth.groups)))
            + 2
        )
        if (
            result.total_a - result.total_b
            != sum((finding.delta for finding in result.findings), Decimal(0))
            or result.control_difference != result.finding_delta_sum
            or result.control_difference
            != sum((finding.delta for finding in result.exceptions), Decimal(0))
            + result.tolerated_delta_total
        ):
            raise AssertionError("Finding/review deltas do not explain the control difference")

    expected_report = _expected_comparison_export(truth)
    if report != expected_report:
        raise AssertionError("Complete structured export differs from independent construction")
    reader = csv.DictReader(StringIO(report.decode("utf-8"), newline=""))
    rows = list(reader)
    expected_reader = csv.DictReader(StringIO(expected_report.decode("utf-8"), newline=""))
    if reader.fieldnames != expected_reader.fieldnames:
        raise AssertionError("Export column order differs from independent construction")
    for row, group in zip(
        rows, (group for group in truth.groups if group.is_exception), strict=True
    ):
        for index, comparison in enumerate(group.field_comparisons, start=1):
            name_a, name_b = comparison.mapping
            if (
                tuple(json.loads(row[f"Comparison {index} File A values — {name_a}"]))
                != comparison.values_a
                or tuple(json.loads(row[f"Comparison {index} File B values — {name_b}"]))
                != comparison.values_b
                or row[f"Comparison {index} status"] != comparison.status
            ):
                raise AssertionError(f"Exported comparison evidence differs: {group.key}")
    if export_exceptions_csv(result) != report:
        raise AssertionError("Repeated complete exports are not byte-identical")


def verify_result(
    a: tuple[SourceRecord, ...],
    b: tuple[SourceRecord, ...],
    result: ReconciliationResult,
    report: bytes,
    truth: GroundTruth,
    *,
    key_normalization: KeyNormalizationConfig | None = None,
) -> None:
    """Check observed output against independent construction, including every key."""

    if result.key_normalization is not key_normalization:
        raise AssertionError("Result does not retain the exact normalization configuration used")
    if result.mode.value != truth.mode:
        raise AssertionError("Reconciliation mode differs from requested ground truth")
    if len(a) != truth.record_count_a or len(b) != truth.record_count_b:
        raise AssertionError("Record counts differ from independent construction")
    if len(result.findings) != len(truth.groups):
        raise AssertionError("Key-group count differs from independent construction")
    if Counter(f.category.value for f in result.findings) != Counter(truth.category_counts):
        raise AssertionError("Category counts differ from independent construction")
    if (
        result.total_a,
        result.total_b,
        result.control_difference,
        result.tolerated_delta_total,
    ) != (truth.total_a, truth.total_b, truth.control_difference, truth.tolerated_delta_total):
        raise AssertionError("Totals differ from independent integer-based construction")
    if len(result.tolerated_findings) != truth.category_counts["within_tolerance"]:
        raise AssertionError("Tolerated finding count differs from independent construction")
    for finding, expected in zip(result.findings, truth.groups, strict=True):
        if (
            finding.key,
            finding.category.value,
            len(finding.rows_a),
            len(finding.rows_b),
            finding.amount_a,
            finding.amount_b,
            finding.delta,
        ) != (
            expected.key,
            expected.category,
            expected.rows_a,
            expected.rows_b,
            decimal_units(expected.units_a),
            decimal_units(expected.units_b),
            expected.delta,
        ):
            raise AssertionError(f"Group differs from independent construction: {expected.key}")
        if finding.is_exception != expected.is_exception:
            raise AssertionError(f"Exception status differs: {expected.key}")
        for records, raw_key, columns in (
            (finding.rows_a, expected.raw_key_a, KEYS_A),
            (finding.rows_b, expected.raw_key_b, KEYS_B),
        ):
            if [row.source_row for row in records] != sorted(row.source_row for row in records):
                raise AssertionError(f"Evidence source rows are not sorted: {expected.key}")
            if raw_key is not None:
                for row in records:
                    if row.key != raw_key:
                        raise AssertionError(f"Original raw key changed: {expected.key}")
                    if tuple(row.raw_fields[column] for column in columns) != raw_key:
                        raise AssertionError(f"Original raw key evidence changed: {expected.key}")
    # Exercise the arithmetic identity directly, without product summation helpers.
    with localcontext() as context:
        context.prec = (
            max(
                len(str(abs(group.units_a))) + len(str(abs(group.units_b)))
                for group in truth.groups
            )
            + len(str(len(truth.groups)))
            + 2
        )
        if result.total_a - result.total_b != sum(finding.delta for finding in result.findings):
            raise AssertionError("Finding deltas do not explain the control difference")
    if result.finding_delta_sum != truth.control_difference:
        raise AssertionError("Finding delta total differs from independent construction")
    if len(result.exceptions) != len(truth.exception_keys):
        raise AssertionError("Exception count differs from independent construction")
    accounted = Counter(
        (row.source, row.source_row)
        for finding in result.findings
        for row in (*finding.rows_a, *finding.rows_b)
    )
    if accounted != Counter((row.source, row.source_row) for row in (*a, *b)):
        raise AssertionError("Source rows were not accounted for exactly once")
    if {id(row) for f in result.findings for row in (*f.rows_a, *f.rows_b)} != {
        id(row) for row in (*a, *b)
    }:
        raise AssertionError("Original source evidence was not retained")
    rows = list(csv.DictReader(StringIO(report.decode("utf-8"), newline="")))
    if [row["Matching key"] for row in rows] != [" / ".join(key) for key in truth.exception_keys]:
        raise AssertionError("Exported exception groups differ from independent construction")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", nargs="+", type=int, default=[1_000, 10_000, 50_000])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tolerances", nargs="+", default=["0", "0.01"])
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--workload", choices=WORKLOADS, default="grouped")
    parser.add_argument(
        "--memory", action="store_true", help="Separate slower tracemalloc pass per scale/mode"
    )
    parser.add_argument(
        "--profile", action="store_true", help="Print cProfile cumulative costs for largest run"
    )
    args = parser.parse_args()
    tolerances = [Decimal(text) for text in args.tolerances]
    modes = [ReconciliationMode(value) for value in args.modes]
    for value in tolerances:
        if not value.is_finite() or value < 0:
            parser.error("tolerances must be finite and nonnegative")
    if any(groups < 100 for groups in args.groups):
        parser.error("groups must be at least 100")
    print(
        f"Python {platform.python_version()} | {platform.platform()} | "
        f"seed={args.seed} | workload={args.workload}"
    )
    print(
        "Normalization config: "
        + json.dumps(NORMALIZATION_RULES if args.workload == "normalization" else None)
    )
    print(
        "Comparison config: "
        + json.dumps(COMPARISON_MAPPINGS if args.workload == "comparison" else ())
    )
    print("Times: seconds, untraced single passes; total includes independent verification.")
    if args.workload == "normalization":
        print("reconcile_s includes normalization and collision preflight; no artificial split.")
    print(
        "Peak: Python allocations in a separate tracemalloc pass, not process RSS or native memory."
    )
    print(
        "groups mode tolerance rows_a rows_b exceptions gen_s ingest_s reconcile_s "
        "export_s verify_s total_s peak_MiB"
    )
    for groups in args.groups:
        for tolerance in tolerances:
            for mode in modes:
                gc.collect()
                result = measure(groups, args.seed, tolerance, mode=mode, workload=args.workload)
                peak = "-"
                if args.memory:
                    gc.collect()
                    tracemalloc.start()
                    try:
                        measure(groups, args.seed, tolerance, mode=mode, workload=args.workload)
                        _, peak_bytes = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    peak = f"{peak_bytes / (1024 * 1024):.1f}"
                print(
                    f"{groups} {mode.value} {tolerance} "
                    f"{result.rows_a} {result.rows_b} {result.exceptions} "
                    f"{result.generation:.3f} {result.ingestion:.3f} {result.reconciliation:.3f} "
                    f"{result.export:.3f} {result.verification:.3f} {result.total:.3f} {peak}",
                    flush=True,
                )
                print(
                    f"Verified {groups} {mode.value}: {json.dumps(result.summary, sort_keys=True)}",
                    flush=True,
                )
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(
            measure,
            max(args.groups),
            args.seed,
            tolerances[-1],
            mode=modes[-1],
            workload=args.workload,
        )
        print(
            "\ncProfile: a separate instrumented pass; its times are not comparable to the table."
        )
        pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(20)


if __name__ == "__main__":
    main()
