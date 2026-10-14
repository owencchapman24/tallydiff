"""Observe deterministic discovery workloads without changing runtime behavior.

Run from the repository root with ``uv run python -m scripts.discovery_benchmark``.
The default sizes are data rows per side, not combined rows. Input generation is
outside the measurements. An untraced pass times each phase; a separate pass over
the same inputs observes Python allocations. No observation is a pass/fail limit.
"""

import argparse
import csv
import gc
import json
import time
import tracemalloc
from collections.abc import Sequence
from io import StringIO
from itertools import combinations

from tallydiff.discovery import (
    DiscoverySnapshot,
    compare_amount_columns,
    compare_key_mapping,
    discover_csv,
)
from tallydiff.models import Source

HEADERS_A = (
    "Vendor ID",
    "Invoice Number",
    "Numeric ID",
    "Invoice Amount",
    "Quantity",
    "Control Number",
    "Document Date",
    "Currency",
    "Notes",
    "Status",
    "Reference Text",
    "Optional Text",
)
HEADERS_B = (
    "Supplier",
    "Invoice Ref",
    "Numeric Identifier",
    "Gross Amount",
    "Units",
    "Control ID",
    "Posting Date",
    "Currency Code",
    "Memo",
    "State",
    "Description",
    "Comment",
)
DEFAULT_ROW_COUNTS = (1_000, 5_000, 20_000)
DEFAULT_COMPOSITE_LIMIT = 64
_MIB = 1024 * 1024


def _nonnegative_integer(value: int, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")


def _validate_width(column_count: int) -> None:
    if type(column_count) is not int:
        raise TypeError("column_count must be an integer")
    if not 10 <= column_count <= len(HEADERS_A):
        raise ValueError("column_count must be between 10 and 12")


def _synthetic_row(index: int) -> tuple[str, ...]:
    cents = 12_000 + (index % 503) * 137
    return (
        f"V{index // 4:06d}",
        f"INV-{index % 4 + 1:02d}",
        str(100_000 + index),
        f"${cents // 100:,}.{cents % 100:02d}",
        str(index % 7 + 1),
        str(200_000 + index // 3),
        f"2026-10-{index % 28 + 1:02d}",
        ("USD", "CAD")[index % 2],
        f"memo-{index % 17}, batch-{index // 31}",
        ("open", "approved", "held")[index % 3],
        f"shipment {index % 11}",
        "" if index % 5 == 0 else f"comment-{index % 13}",
    )


def generate_csv_pair(row_count: int, column_count: int = 12) -> tuple[str, str]:
    """Return in-memory deterministic CSVs with overlapping unique composite keys.

    Vendor and invoice components are duplicated separately; their pair identifies
    each generated observation. File B starts one quarter of the workload later,
    creating partial overlap for workloads of at least four rows. Numeric IDs and
    monetary fields are parseable amounts; dates and other text remain text.
    """

    _nonnegative_integer(row_count, "row_count")
    _validate_width(column_count)
    output: list[str] = []
    for headers, offset in ((HEADERS_A, 0), (HEADERS_B, row_count // 4)):
        stream = StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers[:column_count])
        for index in range(offset, offset + row_count):
            writer.writerow(_synthetic_row(index)[:column_count])
        output.append(stream.getvalue())
    return output[0], output[1]


def _validate_columns(columns: Sequence[str], name: str) -> tuple[str, ...]:
    if not isinstance(columns, Sequence) or isinstance(columns, str | bytes | bytearray):
        raise TypeError(f"{name} must be a sequence of column names")
    values = tuple(columns)
    if any(not isinstance(value, str) for value in values):
        raise TypeError(f"{name} must contain strings")
    if any(not value.strip() for value in values) or len(set(values)) != len(values):
        raise ValueError(f"{name} must contain nonblank distinct column names")
    return values


def composite_candidates(
    columns_a: Sequence[str],
    columns_b: Sequence[str],
    limit: int = DEFAULT_COMPOSITE_LIMIT,
) -> tuple[tuple[tuple[str, str], tuple[str, str]], ...]:
    """Sample two-component mappings across the full deterministic candidate range.

    File A combinations follow header order. Each File B combination has both
    correspondence orders. Evenly spaced integer indices spread a bounded sample
    across column choices without constructing the full Cartesian workload. The
    first candidate includes the generated vendor/invoice correspondence.
    """

    _nonnegative_integer(limit, "limit")
    names_a = _validate_columns(columns_a, "columns_a")
    names_b = _validate_columns(columns_b, "columns_b")
    pairs_a = tuple(combinations(names_a, 2))
    pairs_b = tuple(combinations(names_b, 2))
    total = len(pairs_a) * len(pairs_b) * 2
    sample_count = min(limit, total)
    result: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for sample_index in range(sample_count):
        index = sample_index * total // sample_count
        pair_a = pairs_a[index // (len(pairs_b) * 2)]
        pair_b = pairs_b[(index // 2) % len(pairs_b)]
        if index % 2:
            pair_b = pair_b[::-1]
        result.append(((pair_a[0], pair_b[0]), (pair_a[1], pair_b[1])))
    return tuple(result)


def _key_checksum(snapshot_a: DiscoverySnapshot, snapshot_b: DiscoverySnapshot, pairs) -> int:
    result = compare_key_mapping(snapshot_a, snapshot_b, key_pairs=pairs)
    return (
        result.eligible_row_count_a
        + result.eligible_row_count_b
        + result.distinct_key_count_a
        + result.distinct_key_count_b
        + result.duplicate_excess_count_a
        + result.duplicate_excess_count_b
        + result.overlap_distinct_count
        + result.overlap_row_count_a
        + result.overlap_row_count_b
        + result.only_a_distinct_count
        + result.only_b_distinct_count
    )


def _single_key_phase(snapshot_a, snapshot_b, columns_a, columns_b) -> tuple[int, int]:
    count = checksum = 0
    for name_a in columns_a:
        for name_b in columns_b:
            checksum += _key_checksum(snapshot_a, snapshot_b, ((name_a, name_b),))
            count += 1
    return count, checksum


def _amount_phase(snapshot_a, snapshot_b, columns_a, columns_b) -> tuple[int, int]:
    count = checksum = 0
    for name_a in columns_a:
        for name_b in columns_b:
            result = compare_amount_columns(
                snapshot_a, snapshot_b, column_a=name_a, column_b=name_b
            )
            checksum += (
                result.parseable_count_a
                + result.parseable_count_b
                + result.blank_count_a
                + result.blank_count_b
                + (result.control_difference is not None)
            )
            count += 1
            del result
    return count, checksum


def _composite_key_phase(snapshot_a, snapshot_b, candidates) -> tuple[int, int]:
    count = checksum = 0
    for pairs in candidates:
        checksum += _key_checksum(snapshot_a, snapshot_b, pairs)
        count += 1
    return count, checksum


def _measure_pass(csv_pair, columns_a, columns_b, composites, *, measure_memory: bool) -> dict:
    elapsed: dict[str, float] = {}
    counts: dict[str, int] = {}
    checksums: dict[str, int] = {}
    peaks: dict[str, float] = {}
    incremental_peaks: dict[str, float] = {}
    retained_snapshot_bytes = 0
    baseline = 0
    if measure_memory:
        if tracemalloc.is_tracing():
            raise RuntimeError("benchmark memory measurement requires inactive tracemalloc")
        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]

    def begin_phase() -> tuple[float, int]:
        before = 0
        if measure_memory:
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
        return time.perf_counter(), before

    def finish_phase(name: str, started: float, before: int) -> None:
        elapsed[name] = time.perf_counter() - started
        if measure_memory:
            peak = tracemalloc.get_traced_memory()[1]
            peaks[name] = peak / _MIB
            incremental_peaks[name] = max(0, peak - before) / _MIB

    try:
        started, before = begin_phase()
        snapshot_a = discover_csv(csv_pair[0], source=Source.A)
        snapshot_b = discover_csv(csv_pair[1], source=Source.B)
        finish_phase("discover_csv_pair", started, before)
        if measure_memory:
            gc.collect()
            retained_snapshot_bytes = tracemalloc.get_traced_memory()[0] - baseline
        for name, action in (
            (
                "single_key_pairs",
                lambda: _single_key_phase(snapshot_a, snapshot_b, columns_a, columns_b),
            ),
            (
                "amount_pairs",
                lambda: _amount_phase(snapshot_a, snapshot_b, columns_a, columns_b),
            ),
            (
                "composite_key_pairs",
                lambda: _composite_key_phase(snapshot_a, snapshot_b, composites),
            ),
        ):
            started, before = begin_phase()
            counts[name], checksums[name] = action()
            finish_phase(name, started, before)
    finally:
        if measure_memory:
            tracemalloc.stop()
    return {
        "elapsed_seconds": elapsed,
        "candidate_counts": counts,
        "checksums": checksums,
        "python_memory": {
            "retained_snapshot_bytes": retained_snapshot_bytes,
            "retained_snapshot_mib": retained_snapshot_bytes / _MIB,
            "peak_mib": peaks,
            "incremental_peak_mib": incremental_peaks,
        },
    }


def benchmark_case(
    row_count: int, column_count: int = 12, composite_limit: int = DEFAULT_COMPOSITE_LIMIT
) -> dict:
    """Time one workload and measure its allocations in an independent second pass."""

    csv_pair = generate_csv_pair(row_count, column_count)
    columns_a = HEADERS_A[:column_count]
    columns_b = HEADERS_B[:column_count]
    composites = composite_candidates(columns_a, columns_b, composite_limit)
    timing = _measure_pass(csv_pair, columns_a, columns_b, composites, measure_memory=False)
    memory = _measure_pass(csv_pair, columns_a, columns_b, composites, measure_memory=True)
    if (
        timing["candidate_counts"] != memory["candidate_counts"]
        or timing["checksums"] != memory["checksums"]
    ):
        raise AssertionError("timing and memory passes observed different candidate facts")
    return {
        "row_count_per_side": row_count,
        "column_count_per_side": column_count,
        "candidate_counts": timing["candidate_counts"],
        "elapsed_seconds": timing["elapsed_seconds"],
        "python_memory": memory["python_memory"],
        "checksums": timing["checksums"],
    }


def run_benchmark(
    row_counts: Sequence[int] = DEFAULT_ROW_COUNTS,
    column_count: int = 12,
    composite_limit: int = DEFAULT_COMPOSITE_LIMIT,
) -> dict:
    """Return observational results; normal tests should supply tiny row counts."""

    return {
        "methodology": {
            "timing": (
                "time.perf_counter seconds from an untraced pass, excluding input generation, "
                "process startup, and the separate memory pass"
            ),
            "memory": (
                "Separate tracemalloc pass over the same pre-generated CSV inputs; only Python "
                "allocations are observed. Both snapshots remain alive through comparisons. "
                "Peak is reset before each phase; peak_mib includes retained snapshots and "
                "small measurement allocations. incremental_peak_mib subtracts the current "
                "allocation count at phase start. retained_snapshot_bytes is traced current "
                "memory after discovery and garbage collection, minus the initial baseline; "
                "it estimates snapshots plus small measurement allocations, not process RSS."
            ),
            "candidate_results": (
                "Each compatibility object is discarded after its facts enter an integer "
                "checksum. Timing and memory passes must agree on candidate counts/checksums."
            ),
            "composites": (
                "At most composite_limit two-component candidates, sampled at evenly spaced "
                "integer indices across header-ordered File A combinations and both File B "
                "correspondence orders; no arbitrary-cardinality enumeration"
            ),
            "composite_limit": composite_limit,
            "thresholds": None,
        },
        "cases": [benchmark_case(rows, column_count, composite_limit) for rows in row_counts],
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, nargs="+", default=DEFAULT_ROW_COUNTS)
    parser.add_argument("--columns", type=int, default=12)
    parser.add_argument("--composite-limit", type=int, default=DEFAULT_COMPOSITE_LIMIT)
    args = parser.parse_args(argv)
    report = run_benchmark(args.rows, args.columns, args.composite_limit)
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
