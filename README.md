# TallyDiff

TallyDiff is a local-first financial reconciliation tool for comparing structured exports and explaining, traceably, why they do not agree.

The project is intentionally narrow: correctness, row-level traceability, duplicate detection, and control-total integrity come before integrations or dashboard features.

## Current milestone

The repository currently contains the deterministic reconciliation kernel only. CSV ingestion and the user interface will be added after the core accounting logic is tested and stable.

## Development setup

Requirements:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Core invariant

For every valid reconciliation:

```text
File A control total - File B control total = sum of reconciliation finding deltas
```

Duplicate keys are never silently paired. They remain explicit ambiguous findings even when their group totals offset to zero.

## Kernel contract

- Inputs are already-parsed `SourceRecord` objects. Amounts must be finite `Decimal`
  values; floats, other numeric types, NaN, and infinity are rejected.
- Each row has a `Source` enum member, a positive integer source row number, and a
  nonempty tuple of nonblank strings as its key. Row numbers must be unique within
  each file; the same row number may appear in both files.
- Keys are compared exactly, without trimming or case conversion. Raw fields are
  preserved in an immutable copy for traceability.
- Every input row appears in exactly one finding. Multiple rows on either side of
  a key produce one ambiguous group containing all rows, even with equal totals.
- Findings are sorted by key, and rows within each finding by source row number.
  Inputs are snapshotted once, including one-pass iterables.
- Totals and deltas use exact Decimal arithmetic isolated from the caller's Decimal
  context. No rounding, quantization, or tolerance is applied.

## Scope

Planned v0 scope:

- two CSV files at a time;
- user-defined exact matching key;
- one amount column per file;
- Decimal-based monetary comparison;
- exact matches, amount mismatches, A-only, B-only, and duplicate/ambiguous groups;
- traceable source rows;
- exception export.

No real or confidential financial data should be committed to this repository.
