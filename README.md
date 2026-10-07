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
```

## Core invariant

For every valid reconciliation:

```text
File A control total - File B control total = sum of reconciliation finding deltas
```

Duplicate keys are never silently paired. They remain explicit ambiguous findings even when their group totals offset to zero.

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
