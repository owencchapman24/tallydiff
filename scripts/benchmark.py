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
from decimal import Decimal
from io import StringIO

from scripts.synthetic_data import (
    AMOUNT_A,
    AMOUNT_B,
    KEYS_A,
    KEYS_B,
    generate_pair,
)
from tallydiff import (
    ColumnMapping,
    Source,
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


def measure(groups: int, seed: int, tolerance: Decimal) -> Measurement:
    """Time the full path; independent verification is reported as its own phase."""

    started = time.perf_counter()
    pair = generate_pair(groups, seed=seed)
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
    result = reconcile(a, b, amount_tolerance=tolerance)
    reconciled = time.perf_counter()
    report = export_exceptions_csv(result)
    exported = time.perf_counter()

    truth = pair.expected(tolerance)
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
    if result.finding_delta_sum != truth.control_difference:
        raise AssertionError("Finding deltas do not explain the control difference")
    accounted = Counter(
        (row.source, row.source_row)
        for finding in result.findings
        for row in (*finding.rows_a, *finding.rows_b)
    )
    if accounted != Counter((row.source, row.source_row) for row in (*a, *b)):
        raise AssertionError("Source rows were not accounted for exactly once")
    rows = list(csv.DictReader(StringIO(report.decode("utf-8"), newline="")))
    if [row["Matching key"] for row in rows] != [" / ".join(key) for key in truth.exception_keys]:
        raise AssertionError("Exported exception groups differ from independent construction")
    finished = time.perf_counter()
    return Measurement(
        len(a),
        len(b),
        len(rows),
        generated - started,
        ingested - generated,
        reconciled - ingested,
        exported - reconciled,
        finished - exported,
        finished - started,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", nargs="+", type=int, default=[1_000, 10_000, 50_000])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tolerances", nargs="+", default=["0", "0.01"])
    parser.add_argument(
        "--memory", action="store_true", help="Separate slower tracemalloc pass per scale/mode"
    )
    parser.add_argument(
        "--profile", action="store_true", help="Print cProfile cumulative costs for largest run"
    )
    args = parser.parse_args()
    tolerances = [Decimal(text) for text in args.tolerances]
    for value in tolerances:
        if not value.is_finite() or value < 0:
            parser.error("tolerances must be finite and nonnegative")
    if any(groups < 100 for groups in args.groups):
        parser.error("groups must be at least 100")
    print(f"Python {platform.python_version()} | {platform.system()} | seed={args.seed}")
    print("Times: seconds, untraced single passes; total includes independent verification.")
    print(
        "Peak: Python allocations in a separate tracemalloc pass, not process RSS or native memory."
    )
    print(
        "groups tolerance rows_a rows_b exceptions gen_s ingest_s reconcile_s "
        "export_s verify_s total_s peak_MiB"
    )
    for groups in args.groups:
        for tolerance in tolerances:
            gc.collect()
            result = measure(groups, args.seed, tolerance)
            peak = "-"
            if args.memory:
                gc.collect()
                tracemalloc.start()
                try:
                    measure(groups, args.seed, tolerance)
                    _, peak_bytes = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                peak = f"{peak_bytes / (1024 * 1024):.1f}"
            print(
                f"{groups} {tolerance} {result.rows_a} {result.rows_b} {result.exceptions} "
                f"{result.generation:.3f} {result.ingestion:.3f} {result.reconciliation:.3f} "
                f"{result.export:.3f} {result.verification:.3f} {result.total:.3f} {peak}",
                flush=True,
            )
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(measure, max(args.groups), args.seed, tolerances[-1])
        print(
            "\ncProfile: a separate instrumented pass; its times are not comparable to the table."
        )
        pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(20)


if __name__ == "__main__":
    main()
