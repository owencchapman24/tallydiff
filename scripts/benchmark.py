"""Developer benchmark of synthetic exports; no application behavior or timing thresholds."""

import argparse
import cProfile
import csv
import gc
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
    KEYS_A,
    KEYS_B,
    MODES,
    WORKLOADS,
    GroundTruth,
    decimal_units,
    generate_pair,
)
from tallydiff import (
    ColumnMapping,
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
    mapping = ColumnMapping(tuple(zip(KEYS_A, KEYS_B, strict=True)), AMOUNT_A, AMOUNT_B)
    if mapping.problem(columns_a, columns_b):
        raise AssertionError("Synthetic schema is not compatible with its explicit mapping")
    a = ingest_csv(
        pair.csv_a, source=Source.A, key_columns=mapping.keys_a, amount_column=mapping.amount_a
    )
    b = ingest_csv(
        pair.csv_b, source=Source.B, key_columns=mapping.keys_b, amount_column=mapping.amount_b
    )
    ingested = time.perf_counter()
    result = reconcile(a, b, amount_tolerance=tolerance, mode=mode)
    reconciled = time.perf_counter()
    report = export_exceptions_csv(result)
    exported = time.perf_counter()

    truth = pair.expected(tolerance, mode=mode.value)
    verify_result(a, b, result, report, truth)
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
    )


def verify_result(
    a: tuple[SourceRecord, ...],
    b: tuple[SourceRecord, ...],
    result: ReconciliationResult,
    report: bytes,
    truth: GroundTruth,
) -> None:
    """Check observed output against independent construction, including every key."""

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
        f"Python {platform.python_version()} | {platform.system()} | "
        f"seed={args.seed} | workload={args.workload}"
    )
    print("Times: seconds, untraced single passes; total includes independent verification.")
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
