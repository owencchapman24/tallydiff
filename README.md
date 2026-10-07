# TallyDiff

TallyDiff is a local-first financial reconciliation tool for comparing structured exports and explaining, traceably, why they do not agree.

The project is intentionally narrow: correctness, row-level traceability, duplicate detection, and control-total integrity come before integrations or dashboard features.

## Current milestone

The repository contains a local Streamlit workflow for choosing two CSV files,
mapping columns explicitly, reconciling, and inspecting source evidence. It uses
the existing deterministic kernel and strict CSV/monetary ingestion layer.

## Run locally

From the repository root:

```bash
uv sync
uv run streamlit run src/tallydiff/app.py
```

Open <http://127.0.0.1:8501>. The checked-in Streamlit configuration binds the
server to the local machine and disables usage telemetry. Uploaded files and
results stay in the current local Streamlit session; the app adds no persistence
or external integrations.

### Try the synthetic example

1. Upload `sample_data/file_a.csv` as **File A** and `sample_data/file_b.csv` as
   **File B**. The app displays each filename and validated column names.
2. Choose the mappings below. Use **+ Add key field** for the second key pair.
   Every selector starts empty; the app does not infer mappings.

   | Selection | File A | File B |
   | --- | --- | --- |
   | Key field 1 | Vendor ID | Supplier |
   | Key field 2 | Invoice Number | Invoice Ref |
   | Amount | Invoice Amount | Gross Amount |

3. Click **Run reconciliation**. Expect totals of **2550** and **2305**, a net
   difference of **+245**, one exact match, one amount mismatch, one File A-only
   group, one File B-only group, and no duplicate groups.
4. Select **V001 / 1042** in the exception table. Both evidence panels show source
   record **2**, including the original amounts **1250** and **1205**. The other
   exceptions have deltas **+500** and **-300**.

The pair order defines the composite key. Duplicate selections on either side
and incomplete mappings disable the run action. A changed file resets mappings;
any file or mapping change clears the previous result and requires a fresh run.
Selecting exception rows preserves the current result.

Totals and table amounts are exact Decimal strings, including every fractional
digit. Counts represent key groups. Equal control totals do not clear duplicate
or other row-level exceptions. Every source row in an ambiguous group is shown
in its evidence panel.

Uploads must be UTF-8, optionally with a BOM. Invalid encoding and ingestion
errors block results with contextual diagnostics. Header discovery checks only
the header; the run action validates every data record. Header-only files are
valid, and two empty inputs are reported as having no data records.

### Application structure

- `ingest.py`: shared header discovery through `inspect_csv_columns`, plus the
  existing blocking CSV ingestion API.
- `presentation.py`: UTF-8 decoding, ordered mapping state, configuration identity,
  and exact string/table presentation. It contains no reconciliation rules.
- `app.py`: Streamlit controls, calls to `ingest_csv` and `reconcile`, expected
  error messages, summary, and source-evidence rendering.
- Results are associated with a SHA-256 identity covering both file contents,
  filenames, ordered key pairs, and amount selections. Every rerun checks it
  before displaying stored results. No global cache stores uploaded financial data.

Exception drill-down uses Streamlit's [single-row dataframe selection](https://docs.streamlit.io/develop/api-reference/data/st.dataframe).
Automated [AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)
smoke tests exercise actual upload widgets, mapping controls, results, evidence,
and stale-result invalidation. New non-visual helpers also have focused tests.

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

## CSV ingestion

`inspect_csv_columns(data, source=...)` returns validated column names in their
original order, without requiring mappings or validating data records. It shares
header validation with `ingest_csv`. For text streams, inspection consumes the
header; rewind the stream or provide the original text before ingestion.

`ingest_csv` accepts decoded CSV text or an open text file-like object, plus an
explicit `Source`, ordered key column names, and an amount column name. It returns
an immutable tuple of validated `SourceRecord` objects only after the entire input
passes validation. No rows are returned on failure.

```python
from tallydiff import Source, ingest_csv, reconcile

records_a = ingest_csv(
    "vendor,invoice,amount\nV001,001042,1250.00\n",
    source=Source.A,
    key_columns=["vendor", "invoice"],
    amount_column="amount",
)
records_b = ingest_csv(
    "vendor,invoice,amount\nV001,001042,1205.00\n",
    source=Source.B,
    key_columns=["vendor", "invoice"],
    amount_column="amount",
)
result = reconcile(records_a, records_b)  # control difference: Decimal("45.00")
```

- CSV uses commas, double-quoted fields, and doubled double quotes for escaping.
  Fields containing commas or newlines must be quoted.
- The header is source record **1**; the first data record is **2**. A quoted field
  spanning physical lines is still part of one CSV record. `source_row` is a
  record ordinal, not necessarily a physical line number.
- Header names and column selections are exact, including case and surrounding
  whitespace. Missing headers, blank names, duplicate names (including unselected
  columns), and missing selected columns block ingestion. No mappings are guessed.
- Key values are strings, with only surrounding whitespace trimmed for matching.
  Leading zeroes, case, punctuation, and internal whitespace are preserved.
  Blank required key components block ingestion.
- `raw_fields` preserves every original field value as parsed by `csv`, including
  whitespace and embedded line endings. CSV quoting is decoded; stored evidence
  is never trimmed to match the normalized key.
- Blank records, extra or missing fields, malformed quoting, invalid amounts,
  and text read/decoding errors block ingestion. A valid header with no data
  produces an empty tuple. Duplicate **data keys** are retained for the engine
  to report as ambiguous groups.
- Caller-owned streams are consumed from their current position and never closed.
  Open files with `newline=""` and an explicit encoding. For UTF-8 files with an
  optional byte-order mark, use `encoding="utf-8-sig"`; ingestion does not infer
  encodings or remove a byte-order mark from already-decoded text.

`IngestionError` exposes `source`, `source_row`, `column`, `value`, and `reason`
when applicable. Display `str(error)` for a concise diagnostic instead of a
traceback. Ingestion stops at the first error; it does not return partial results.

## Monetary text grammar

`parse_amount` constructs `Decimal` directly from validated source text. There is
no float conversion, quantization, tolerance, currency conversion, or automatic
sign inversion. All fractional digits and trailing fractional zeroes are retained.

Supported forms include:

- plain amounts: `1200`, `1200.00`, `0`, `0.00`, `.125`, `1200.`;
- groups of three thousands digits: `1,200.00`, `1,234,567.8901`;
- an optional dollar prefix: `$1,200.00`;
- a leading sign before any dollar prefix: `-1200.00`, `-$1,200.00`, `+$1,200.00`;
- accounting negatives: `(1,200.00)`, `($1,200.00)`.

Only whitespace surrounding the entire value is ignored. The grammar uses ASCII
digits, comma thousands separators, and a period decimal separator. Parentheses
cannot be combined with an explicit sign. Blank amounts, malformed commas,
non-finite values, internal whitespace, other currency symbols, decimal-comma
locales, trailing signs, underscores, and scientific notation are rejected with
`AmountParseError` (wrapped in `IngestionError` with CSV record and column context
when ingesting CSV). No malformed amount is converted to zero.

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
