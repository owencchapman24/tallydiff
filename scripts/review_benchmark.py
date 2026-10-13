"""Validate and characterize review persistence without changing product behavior.

Run from the repository root::

    uv run --locked python scripts/review_benchmark.py
    uv run --locked python -m scripts.review_benchmark --groups 1000 5000 20000
    uv run --locked python scripts/review_benchmark.py \
        --output benchmark_output/review_v07.json

Each size runs in a fresh process. Phase timings come from an untraced pass;
a second complete, independently verified pass measures Python allocations.
Neither process startup nor the instrumented pass is part of the reported total.
Nothing is written unless --output is supplied. Observations have no thresholds.
"""

import argparse
import gc
import json
import platform
import subprocess
import sys
import time
import tracemalloc
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tallydiff import (
    ColumnMapping,
    FindingReview,
    ReconciliationMode,
    ReconciliationResult,
    ReviewDisposition,
    ReviewSession,
    Source,
    bind_review_result,
    export_exceptions_csv,
    export_review_session,
    export_reviewed_exceptions_csv,
    ingest_csv,
    load_review_session,
    reconcile,
)

if __package__:
    from .review_oracle import (
        expected_counts,
        expected_reviews,
        generate_inputs,
        verify_artifacts,
    )
else:
    from review_oracle import (
        expected_counts,
        expected_reviews,
        generate_inputs,
        verify_artifacts,
    )

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MEMORY_SCOPE = "Python allocations observed by tracemalloc"
_TIMING_SCOPE = (
    "Seconds in an untraced workflow; excludes process startup and the separate memory pass. "
    "Verification includes repeated binding/export checks and reconciliation integrity checks."
)


@dataclass(frozen=True, slots=True)
class _EvidenceSnapshot:
    """Capture identities and values, including raw fields excluded from model equality."""

    result: ReconciliationResult
    findings: tuple
    exceptions: tuple
    truth: tuple
    finding_evidence: tuple
    row_evidence: tuple
    original_csv: bytes
    fingerprint: str

    @classmethod
    def capture(cls, result, original_csv, fingerprint):
        findings = result.findings
        return cls(
            result,
            findings,
            result.exceptions,
            (
                result.total_a,
                result.total_b,
                result.control_difference,
                result.finding_delta_sum,
                result.tolerated_delta_total,
                result.tolerated_findings,
                result.mode,
                result.amount_tolerance,
            ),
            tuple(
                (
                    finding,
                    finding.key,
                    finding.category,
                    finding.amount_a,
                    finding.amount_b,
                    finding.delta,
                    finding.rows_a,
                    finding.rows_b,
                    finding.field_comparisons,
                    finding.correspondence_analysis,
                )
                for finding in findings
            ),
            tuple(
                (
                    row,
                    row.raw_fields,
                    row.source,
                    row.source_row,
                    row.key,
                    row.amount,
                    tuple(row.raw_fields.items()),
                )
                for finding in findings
                for row in (*finding.rows_a, *finding.rows_b)
            ),
            original_csv,
            fingerprint,
        )

    def assert_unchanged(self, result, binding):
        assert result is self.result and result.findings is self.findings
        assert binding.result is result and binding.result_fingerprint == self.fingerprint
        assert result.exceptions == self.exceptions
        assert all(a is b for a, b in zip(result.exceptions, self.exceptions, strict=True))
        assert (
            result.total_a,
            result.total_b,
            result.control_difference,
            result.finding_delta_sum,
            result.tolerated_delta_total,
            result.tolerated_findings,
            result.mode,
            result.amount_tolerance,
        ) == self.truth
        for finding, stored in zip(result.findings, self.finding_evidence, strict=True):
            original, key, category, amount_a, amount_b, delta, rows_a, rows_b, fields, analysis = (
                stored
            )
            assert finding is original
            assert (
                finding.key,
                finding.category,
                finding.amount_a,
                finding.amount_b,
                finding.delta,
            ) == (
                key,
                category,
                amount_a,
                amount_b,
                delta,
            )
            assert finding.rows_a is rows_a and finding.rows_b is rows_b
            assert (
                finding.field_comparisons is fields and finding.correspondence_analysis is analysis
            )
        for row, raw_fields, source, ordinal, key, amount, contents in self.row_evidence:
            assert row.raw_fields is raw_fields
            assert (row.source, row.source_row, row.key, row.amount) == (
                source,
                ordinal,
                key,
                amount,
            )
            assert tuple(row.raw_fields.items()) == contents
        assert export_exceptions_csv(result) == self.original_csv


def measure_review_workflow(groups: int) -> dict:
    """Run public APIs and a fixture-based oracle; return observations, never thresholds."""

    if type(groups) is not int or groups <= 0:
        raise ValueError("groups must be a positive integer")
    timings = {}
    started = phase = time.perf_counter()

    def finish_phase(name):
        nonlocal phase
        now = time.perf_counter()
        timings[name] = now - phase
        phase = now

    data_a, data_b = generate_inputs(groups)
    expected = expected_reviews(groups)
    expected_by_key = {review.matching_key: review for review in expected}
    counts = expected_counts(groups)
    finish_phase("generation")

    mapping = ColumnMapping((("ledger_id", "payment_id"),), "booked_amount", "settled_amount")
    records_a = ingest_csv(
        data_a.decode("utf-8"),
        source=Source.A,
        key_columns=mapping.keys_a,
        amount_column=mapping.amount_a,
    )
    records_b = ingest_csv(
        data_b.decode("utf-8"),
        source=Source.B,
        key_columns=mapping.keys_b,
        amount_column=mapping.amount_b,
    )
    finish_phase("ingestion")
    result = reconcile(records_a, records_b, mode=ReconciliationMode.UNIQUE)
    finish_phase("reconciliation")
    binding_arguments = dict(
        data_a=data_a, data_b=data_b, mapping=mapping, format_a="csv", format_b="csv"
    )
    binding = bind_review_result(result, **binding_arguments)
    finish_phase("binding")
    original_csv = export_exceptions_csv(result)
    finish_phase("original_csv_export")
    evidence = _EvidenceSnapshot.capture(result, original_csv, binding.result_fingerprint)
    finish_phase("integrity_capture")

    baseline = ReviewSession.unreviewed(binding)
    assert baseline.total == groups and baseline.unreviewed_count == groups
    session = ReviewSession(
        binding,
        tuple(
            FindingReview(item.matching_key, ReviewDisposition(item.disposition), item.note)
            for item in expected
        ),
    )
    finish_phase("session_population")
    session_json = export_review_session(session)
    finish_phase("session_json_export")
    loaded = load_review_session(session_json, binding=binding)
    finish_phase("session_json_load")
    reviewed_csv = export_reviewed_exceptions_csv(binding, loaded)
    finish_phase("reviewed_csv_export")

    assert len(records_a) == len(records_b) == len(result.exceptions) == groups
    assert binding.exception_keys == tuple(expected_by_key)
    assert all(getattr(session, name) == count for name, count in counts.items())
    total_a_cents = sum(10_000 + index % 97 for index in range(groups))
    assert result.total_a == Decimal(total_a_cents) / 100
    assert result.total_b == Decimal(total_a_cents - groups) / 100
    assert result.control_difference == Decimal(groups) / 100
    assert all(finding.delta == Decimal("0.01") for finding in result.findings)
    assert loaded == session and loaded.binding is binding
    assert export_review_session(session) == session_json
    assert export_review_session(loaded) == session_json
    repeated_binding = bind_review_result(result, **binding_arguments)
    assert repeated_binding.result_fingerprint == binding.result_fingerprint
    verified = verify_artifacts(
        session_json, original_csv, reviewed_csv, expected, binding.result_fingerprint
    )
    evidence.assert_unchanged(result, binding)
    finish_phase("independent_verification")
    timings["total"] = time.perf_counter() - started
    return {
        "groups": groups,
        "exceptions": len(binding.exception_keys),
        "counts": counts,
        "fingerprint": binding.result_fingerprint,
        "timings_s": timings,
        "artifact_bytes": {
            "session_json": len(session_json),
            "original_csv": len(original_csv),
            "reviewed_csv": len(reviewed_csv),
        },
        "verified": verified,
    }


def _benchmark_size(groups: int) -> dict:
    measurement = measure_review_workflow(groups)
    gc.collect()
    tracemalloc.start()
    try:
        memory_pass = measure_review_workflow(groups)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert memory_pass["fingerprint"] == measurement["fingerprint"]
    assert memory_pass["artifact_bytes"] == measurement["artifact_bytes"]
    measurement["python_allocation_peak_bytes"] = peak
    return measurement


def _output_path(parser, text):
    root = _REPO_ROOT / "benchmark_output"
    resolved_root = root.resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = _REPO_ROOT / candidate
    resolved = candidate.resolve()
    if not resolved_root.is_relative_to(_REPO_ROOT) or not resolved.is_relative_to(resolved_root):
        parser.error("--output must be a file inside benchmark_output/")
    if resolved == resolved_root:
        parser.error("--output must name a file, not benchmark_output/ itself")
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", nargs="+", type=int, default=[1000, 5000, 20000])
    parser.add_argument("--output", help="Write JSON only when requested, inside benchmark_output/")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(groups <= 0 for groups in args.groups) or (args.worker is not None and args.worker <= 0):
        parser.error("groups must be positive integers")
    if args.worker is not None:
        print(json.dumps(_benchmark_size(args.worker), ensure_ascii=False, allow_nan=False))
        return
    output = _output_path(parser, args.output) if args.output is not None else None
    environment = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "os": platform.platform(),
    }
    print(f"Python {environment['python']} | {environment['os']}", flush=True)
    print(_TIMING_SCOPE, flush=True)
    print(
        f"Peak memory: {_MEMORY_SCOPE}; separate verified pass in each fresh size process.",
        flush=True,
    )
    measurements = []
    for groups in args.groups:
        completed = subprocess.run(
            [sys.executable, "-m", "scripts.review_benchmark", "--worker", str(groups)],
            cwd=_REPO_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        measurement = json.loads(completed.stdout)
        measurements.append(measurement)
        artifacts = measurement["artifact_bytes"]
        peak = measurement["python_allocation_peak_bytes"] / (1024 * 1024)
        print(
            f"exceptions={groups} | JSON={artifacts['session_json']} bytes | "
            f"reviewed CSV={artifacts['reviewed_csv']} bytes | peak={peak:.2f} MiB",
            flush=True,
        )
        print(
            "  "
            + " | ".join(
                f"{name}_s={value:.6f}" for name, value in measurement["timings_s"].items()
            ),
            flush=True,
        )
        print("  Counts: " + json.dumps(measurement["counts"], sort_keys=True), flush=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "environment": environment,
            "memory_scope": _MEMORY_SCOPE,
            "timing_scope": _TIMING_SCOPE,
            "measurements": measurements,
        }
        output.write_text(
            json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"Wrote {output.relative_to(_REPO_ROOT)}", flush=True)


if __name__ == "__main__":
    main()
