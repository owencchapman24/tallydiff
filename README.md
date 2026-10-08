# TallyDiff

**TallyDiff v0.3.0** compares two structured financial exports locally and explains
which key groups account for their difference. Pair exact key columns, such as
vendor and invoice number, then compare unique records or explicitly aggregate
all rows sharing each key. Review differences with every original source row.

- **CSV and XLSX inputs**, including CSV ↔ XLSX in either direction. Select one
  worksheet per workbook; multiple worksheets require an explicit choice.
- **Two reconciliation modes**: **Unique records** keeps duplicate keys ambiguous;
  **Group by matching key** compares exact-key totals without inferring individual
  row correspondence.
- **Absolute amount tolerance**, defaulting to zero. Accepted nonzero differences
  remain visible in a distinct **Within tolerance** category and review table.
- **Reusable mapping profiles** for ordered key pairs, amount columns, tolerance,
  and reconciliation mode. Current profiles use schema version 2; valid version 1
  profiles remain supported and load as Unique records.
- **Exact Decimal arithmetic** after parsing. XLSX numeric cells first convert
  through decimal text; Excel display formatting is not reconstructed.
- **Conservative formula handling**: formulas in selected key or amount fields
  block ingestion. Formulas are never calculated and cached results are not used.
- **Traceable exceptions and CSV export**, with every source row accounted for
  and all finding deltas explaining the control-total difference.
- **Display-only exception review** with key search, category and minimum-delta
  filters, and deterministic sorting. The exception export always stays complete.

Equal totals alone never imply a reconciliation. TallyDiff reports differences;
it does not decide which source is authoritative. See [known limitations](#known-limitations)
and [XLSX cell policies](#xlsx-worksheet-and-cell-policies) before preparing exports.

![TallyDiff sample reconciliation](docs/tallydiff-demo.png)

The screenshot shows the synthetic +245 CSV example in the earlier interface.
The same Unique records result remains valid in v0.3.0. This image predates
reconciliation modes, review controls, tolerance, mapping profiles, and XLSX
worksheet selection; it does not show the full current workflow.

## Quick start

Requirements: Python **3.12+** and [uv](https://docs.astral.sh/uv/).
Clone this repository, then run these commands from its root:

```bash
uv sync --locked
uv run streamlit run src/tallydiff/app.py
```

Open <http://127.0.0.1:8501>. The tracked `.streamlit/config.toml` binds the server
to the local machine and disables Streamlit usage telemetry. TallyDiff does not
send uploaded data to external services, cache it globally, or save it to disk.
Files and results are held in the local session; downloaded reports are saved
where you choose. Installing dependencies requires network access initially.

## Sample workflow

The files in `sample_data/` contain only synthetic records.

1. Upload `sample_data/file_a.csv` as **File A** and `sample_data/file_b.csv` as
   **File B**. The app displays each filename and validated column names.
2. Choose the mappings below. Use **+ Add key field** for the second key pair.
   Every selector starts empty; the app does not infer mappings.

   | Selection | File A | File B |
   | --- | --- | --- |
   | Key field 1 | Vendor ID | Supplier |
   | Key field 2 | Invoice Number | Invoice Ref |
   | Amount | Invoice Amount | Gross Amount |

3. Leave **Reconciliation mode** at **Unique records** and **Amount tolerance** at
   its default `0`, then click **Run reconciliation**.
   Expect File A total **2550**, File B total **2305**, and net difference **+245**. The four key groups are:

   | Matching key | Category | Delta (A - B) |
   | --- | --- | --- |
   | V001 / 1042 | Amount mismatch | +45 |
   | V002 / 1043 | Exact match | 0 |
   | V003 / 1044 | File A only | +500 |
   | V004 / 1045 | File B only | -300 |

4. Select **V001 / 1042** in the exception table. Both evidence panels show
   source record **2**, including the original amounts **1250** and **1205**.
5. Click **Download exception report**. The CSV contains exactly the three
   exception groups above, with blank amounts for the missing sides.

The pair order defines the composite key. Incomplete mappings or repeated key
column selections disable reconciliation. A changed file or worksheet resets
mappings; any file, worksheet, mapping, mode, or tolerance change clears the
result and download until you run again. Review controls and evidence selection
preserve the current result.

## Reconciliation modes

**Unique records** is the default. Exactly one source row on each side is eligible
for an exact, tolerated, or mismatched comparison. More than one row on either
side makes the entire exact-key group **Duplicate / ambiguous**, even when totals
agree, offset to zero, or the other side is absent. All rows remain visible.

**Group by matching key** totals every row sharing the explicitly mapped composite
key independently on each side, then compares those totals. For example, File A
rows of `100` and `200` versus a File B row of `300` form an exact group. This
supports one-to-many, many-to-one, and many-to-many key groups without guessing
row pairs. Presence takes precedence over net amount: a zero-net group with no
File B rows remains **File A only**, and vice versa.

**Key-total agreement does not establish individual-row correspondence.** Grouped
mode performs no subset-sum search, allocation, or inferred many-to-many matching.
The result discloses its mode and keeps this disclaimer visible. Its **Grouped
exact key totals** table offers selectable evidence only for exact findings with
multiple source rows on at least one side. The overall **Exact match** count still
includes one-to-one exact findings. All counts represent key groups, not rows.

## Absolute amount tolerance

**Amount tolerance** accepts ordinary monetary text in the same units as the
selected amount columns. It defaults to `0`, requiring exact equality in either mode.
Negative, blank, malformed, and non-finite values block reconciliation. The
existing monetary grammar is used directly to construct a Decimal; no float
conversion or rounding occurs, and more than two decimal places are supported.

Tolerance applies to eligible keys present on both sides: single records in
Unique records mode, or complete key totals in Group by matching key mode. Exact
equality remains **Exact match**. A nonzero difference is **Within tolerance**
when `abs(A - B) <= tolerance` (inclusive); larger differences are **Amount
mismatch**. At tolerance `0.01`, `100.00` versus `100.01` is within tolerance with
a true delta of `-0.01`; `100.00` versus `100.02` remains an amount mismatch.
One-sided groups always require review, including zero-net grouped keys. Duplicate
/ ambiguous groups in Unique records mode remain exceptions regardless of tolerance.

Within-tolerance findings are accepted variances, excluded from ordinary
exceptions and their CSV report. The summary shows the tolerance used and the
Within tolerance group count. A separate **Within tolerance** table retains both
amounts, the actual delta, and selectable original source evidence. Its **Net
accepted variance (A - B)** is a signed sum: opposing deltas can cancel, while
the count and individual rows still expose every accepted difference.

With only exact matches and accepted differences, the app says **Reconciled
within configured tolerance**. The control totals and net difference always
include all true deltas; tolerance never zeroes, rounds, or hides arithmetic.

## Portable mapping profiles

For recurring exports with the same logical columns, save the current valid
configuration with **Download mapping profile**. The app offers this download
once the ordered key pairs, both amount columns, mode, and tolerance are valid; you do
not need to run reconciliation first. The default filename is
`tallydiff_profile.json`, and you may rename it outside TallyDiff.

A profile remembers ordered File A ↔ File B key pairs, both amount columns,
reconciliation mode, and the exact Decimal tolerance, including trailing zeroes such as
`0.0100`. It contains column names and configuration only: no uploaded source
records, source filenames or hashes, worksheet names, financial amounts, totals,
findings, timestamps, or machine-specific paths. Profiles stay local; TallyDiff
uses no external service, profile directory, database, account, browser storage,
or automatic filesystem writes.
Saving is an explicit download to a location you choose.

To reuse a profile:

1. Upload the new File A and File B exports (CSV or XLSX). Select worksheets
   for workbooks before applying a profile.
2. Under **Mapping profile**, upload the saved JSON file. Selecting a file does
   not change the current configuration.
3. Click **Apply profile**. TallyDiff validates the entire profile and every
   required column on its configured side before changing any setting.
4. Check the populated key pairs, amount columns, mode, and tolerance, then click
   **Run reconciliation**. Applying a valid profile clears any prior result and
   exception download, even if its configuration is identical.
5. Edit any populated control normally if needed, and download a new profile
   when the new configuration is valid. Profiles never lock the controls.

Column names must match exactly on the correct side, including case and
whitespace. Extra columns and changed filenames or header order are allowed.
Profiles are format-agnostic: they contain no input type or worksheet
name, and work with any CSV/XLSX combination exposing the required columns.
Missing or renamed required columns produce an error naming the unavailable
columns; nothing is partially applied or guessed. A rejected profile leaves the
current configuration and any result for it intact. Update mappings manually
when schemas change, then save a new profile.

Profiles use UTF-8 JSON with a stable format identifier and schema version:

```json
{
  "format": "tallydiff-mapping-profile",
  "version": 2,
  "key_pairs": [
    {"file_a": "Vendor ID", "file_b": "Supplier"},
    {"file_a": "Invoice Number", "file_b": "Invoice Ref"}
  ],
  "amount_columns": {
    "file_a": "Invoice Amount",
    "file_b": "Gross Amount"
  },
  "amount_tolerance": "0.0100",
  "reconciliation_mode": "grouped_by_key"
}
```

Tolerance is a JSON string parsed directly with the monetary grammar; JSON
numeric tolerances are rejected. Schema version 2 requires exactly the documented
fields, including `reconciliation_mode`, whose only values are `"unique"` and
`"grouped_by_key"`. Valid schema version 1 profiles contain the same fields except
`reconciliation_mode` and always load as **Unique records**. Current downloads
always use schema version 2. Package version **0.3.0** and profile schema version
**2** are separate versions. Invalid JSON, duplicate object fields, unsupported
formats or versions, blank or repeated key selections, invalid amount names,
and invalid or negative tolerance are rejected with a concise message.

## Supported inputs

- Each input may independently be comma-delimited UTF-8 CSV (optional BOM) or
  `.xlsx`: CSV ↔ CSV, CSV ↔ XLSX, XLSX ↔ CSV, and XLSX ↔ XLSX are supported.
  Header names and selections are exact. Header-only inputs are valid.
- One or more explicitly paired key columns in the same logical order, and one
  amount column per file. Matching trims surrounding key whitespace only;
  leading zeroes, case, punctuation, and internal whitespace remain significant.
- U.S.-style amounts such as `1200.00`, `1,200.00`, `$1,200.00`, `-1200.00`, and
  `(1,200.00)`. Quote CSV fields containing commas. All decimal places are retained.
- Invalid encoding, malformed CSV, missing fields, blank keys, invalid headers,
  or invalid amounts block the run with file/record/column context where available.
  Header discovery checks only the header; reconciliation validates every record.

See the [monetary grammar](#monetary-text-grammar) for the full accepted syntax.

## XLSX worksheet and cell policies

Upload an ordinary, unencrypted **.xlsx** workbook with text column names in
**worksheet row 1** and structured data underneath. A workbook with one ordinary
worksheet selects it automatically. With multiple worksheets, select one explicitly;
TallyDiff never guesses by name or uses Excel's active-sheet setting. Names are
shown exactly, in workbook order, including hidden/very-hidden worksheets.
Chartsheets are not reconciliation worksheets. Only the selected sheet is ingested.
Changing a worksheet clears mappings, results, and the exception download.

Headers must be nonblank, unique text, matched exactly. Numeric/date/formula
headers, merged-header interpretation, multiple header rows, and arbitrary layouts
are unsupported. Empty cells after the last header name are unused columns;
any populated data beyond the named columns blocks ingestion. Trailing wholly
empty rows, including format-only rows, are ignored. A wholly blank row inside
the populated data region blocks ingestion with its row number. Partially populated
rows remain subject to required-key and amount validation; they are never dropped.

**Amounts:** numeric cells exposed by openpyxl as int/float must be finite and
convert through Python's shortest deterministic decimal text, using
`Decimal(str(value))`: numeric `100` → `100`, `100.01` → `100.01`, and
`-42.5` → `-42.5`. TallyDiff preserves the decimal value represented by that
openpyxl value. It does not reconstruct Excel's underlying binary representation
or recover precision already lost in the workbook. No `Decimal(float)` conversion
occurs. Text amounts use the same strict monetary grammar as CSV, including
`$1,200.00` and `(42.50)`. Blank, boolean, error, date/time, and non-finite amounts
block ingestion.

**Keys:** text retains leading zeroes, case, internal whitespace, and punctuation;
only surrounding whitespace is trimmed for matching. Finite numeric keys become
canonical decimal text without locale formatting, exponent notation, or insignificant
fractional zeroes. Numeric `123` remains `"123"`, even with a `000000` number format;
text `"000123"` remains a different key. Store identifiers as **text** in Excel
when leading zeroes matter. Date/datetime/time keys use stable ISO text as exposed
by openpyxl; a date-formatted serial can appear as a datetime ending in
`T00:00:00`. Keep source types consistent when matching dates across exports.
Blank, boolean, Excel error, formula, and duration keys are rejected.

**Formulas:** workbooks load read-only with `data_only=False`. A formula in any
selected key or amount cell blocks ingestion with File A/B, worksheet, row, and
column context. Export or paste those reconciliation fields **as values** first.
TallyDiff never evaluates formulas or uses cached results. Ordinary formulas in
unselected evidence columns are retained as formula text.

**Evidence:** XLSX evidence shows deterministic **source cell values**, not Excel's
visual formatting: original text, numeric decimal text, ISO date/time text,
`TRUE`/`FALSE`, formula/error text, and empty strings for blank cells. Duration
values in unrelated evidence columns use their textual duration representation.
Number formats never infer currency, add commas/currency signs, restore leading
zeroes, invent trailing zeroes, round, or quantize amounts. For example, a cell
with underlying `1234.5` and display format `$#,##0.00` retains `1234.5`.
Styles, colors, comments, and calculated formula values are not preserved.

XLSX source references use **actual worksheet row numbers**: header row 1, first
data row 2. CSV references count logical CSV records, so multiline fields do not
advance the source number. Keep the original workbook and selected sheet with
any exception report to locate its evidence.

Processing is local and in memory using openpyxl; Excel/LibreOffice are not
required. Legacy `.xls`, macro-enabled `.xlsm`, templates, encrypted workbooks,
formula calculation, table/pivot interpretation, named ranges, and reconciling
multiple sheets at once are unsupported. The exception download remains CSV;
TallyDiff does not write Excel workbooks. Schema version 2 profiles store mappings,
tolerance, and mode independently of input format, filename, or sheet; valid
version 1 profiles continue to load as Unique records.

## Correctness and traceability

| Category | Unique records | Group by matching key |
| --- | --- | --- |
| Exact match | One row on each side with equal amounts. | Both sides present with equal key totals. |
| Within tolerance | One row on each side with a nonzero absolute delta at or below tolerance. | Both sides present with a nonzero absolute key-total delta at or below tolerance. |
| Amount mismatch | One row on each side with an absolute delta above tolerance. | Both sides present with an absolute key-total delta above tolerance. |
| File A only | One File A row and no File B row. | One or more File A rows and no File B rows, even with a zero net amount. |
| File B only | One File B row and no File A row. | One or more File B rows and no File A rows, even with a zero net amount. |
| Duplicate / ambiguous | More than one row on either side; all rows need review. | Not emitted: every exact-key group is compared by presence and totals. |

In Unique records mode, duplicates take precedence over other categories, even
when amounts agree or offset to zero. Grouped mode checks presence before totals.
Neither mode silently pairs or discards source rows.
Counts represent key groups, while the evidence panels retain every source row.
A missing side shows an em dash in the table and a blank amount in the export;
a present zero-dollar record shows its actual Decimal amount, such as `0.00`.

The engine checks both row accounting and this invariant on every run:

```text
File A control total - File B control total = sum of all finding deltas
```

Monetary text is parsed directly to `Decimal`. XLSX numeric cells first convert
from openpyxl's int/float value through decimal text, as described above. All
subsequent arithmetic uses exact `Decimal`, without rounding or quantization.
Tolerance changes classification only. Findings are sorted by key, and evidence
by source record number. Cell-value evidence is preserved in immutable snapshots.
XLSX source numbers are actual worksheet rows. The CSV header is record **1**, and the first data record is **2**; a quoted multiline
field is still part of one record, so these are not necessarily physical line numbers.

## Exception review controls

The exception table supports these display-only controls:

- **Search matching keys** finds a case-insensitive substring within any key
  component. Surrounding search whitespace is ignored; matching keys themselves
  are not normalized, fuzzily matched, or interpreted as regular expressions.
- **Exception categories** selects amount mismatch, File A only, File B only,
  and duplicate / ambiguous groups. All are selected initially; selecting none
  shows no exceptions.
- **Minimum absolute delta** defaults to `0` and includes the boundary. Blank
  means zero. Invalid or negative input shows an error without clearing the
  completed reconciliation. This filter does not change amount tolerance.
- **Exception sort order** defaults to ascending matching key, with absolute delta
  options largest or smallest first. Equal absolute deltas use the key as a
  deterministic tie-breaker.

**Showing X of Y exception groups** distinguishes displayed and total counts. A view
with no matches leaves the completed result available. Selecting any visible row
opens every original source row for that finding, including multi-row groups.
Exact grouped totals and accepted variances retain separate evidence tables.

Review controls and evidence selection do not change configuration identity,
profiles, reconciliation metrics, control totals, or the exception download.
**Download exception report always contains all exceptions**, including those
hidden by the current search or filters.

## Exception report

**Download exception report** saves `tallydiff_exceptions.csv` for the complete
reconciliation result, including exceptions hidden by review controls. There
is one row per exception key group, including zero-delta ambiguous and one-sided
groups. Exact matches and accepted within-tolerance findings are excluded; the UI offers no exception download when there are no exceptions.
When accepted variance exists, exception-report deltas alone need not equal the
control difference: add the net accepted variance shown in the UI.

Columns, in order: `Category`, `Matching key`, `File A amount`, `File B amount`,
`Delta (A - B)`, `File A records`, `File B records`. Matching key components use
` / `, as in the table. Amounts and signed deltas retain the application's exact
Decimal text. Missing-side amounts are blank; a present `0.00` remains `0.00`.

Each side lists **all** participating source record numbers, separated by `; `,
including every duplicate. Keep the original input files, selected worksheet
names, and the configuration shown in the UI: those references provide traceability without duplicating every
raw field in the report. The joined key is a display label, not a serialization
for reconstructing composite keys that themselves contain ` / `.

Output is comma-delimited UTF-8 CSV without a BOM, with CRLF record endings and
standard quoting for commas, quotes, and embedded newlines. The UI-independent
`export_exceptions_csv(result)` API returns bytes; a result with no exceptions
produces a header-only CSV.

**Spreadsheet safety:** matching-key text is exported as displayed, without
adding an apostrophe or tab prefix. Such prefixes would change identifiers for
downstream CSV consumers. This preserves traceability but means the report is
**not sanitized for spreadsheet formula execution**. Formula-like keys, including
values starting with `=`, `+`, `-`, or `@`, can be interpreted as formulas by a
spreadsheet; CSV quoting alone does not prevent this. Control characters and
locale-specific variants can also matter. See [OWASP's CSV injection guidance](https://community.owasp.org/attacks/CSV_Injection).

Do not open untrusted reports by double-clicking them in a spreadsheet. Use a CSV
reader, or import every column as text with formula evaluation disabled. Text
import also avoids losing leading zeroes or rounding long monetary values in the
spreadsheet. Numeric amount/delta cells are never prefixed or otherwise rewritten.

## Architecture

| Module | Responsibility |
| --- | --- |
| `_decimal.py` | Exact addition isolated from the caller's Decimal context. |
| `amounts.py` | Strict monetary text parsing directly to Decimal. |
| `models.py` | Validated source records, immutable evidence, findings and totals. |
| `engine.py` | Exact grouping, classification, deterministic ordering and integrity checks. |
| `ingest.py` | Header discovery and atomic CSV validation with explicit mappings. |
| `xlsx.py` | Worksheet/header discovery and atomic XLSX validation into the same source records. |
| `configuration.py` | Shared ordered column mappings and manual mapping validation. |
| `profiles.py` | Validated JSON profile import/export and directional column compatibility. |
| `presentation.py` | UTF-8 decoding, configuration identity, review filtering/sorting and display strings. |
| `export.py` | Ordinary CSV bytes, reusing UI-independent labels and Decimal formatting. |
| `app.py` | Streamlit controls, error messages, results, download and source evidence. |

Public Python APIs are exposed through `tallydiff`. The engine and export layer
have no Streamlit dependency. The UI performs no financial calculations.
Results carry a SHA-256 identity covering file contents, filenames, ordered key
pairs, amount selections, selected worksheets, mode, and the effective Decimal
tolerance; each rerun checks it before showing results or a download. Review
search, categories, minimum delta, sort order, evidence selection, profile filenames,
and JSON formatting do not enter this identity. Equivalent tolerance values share
an identity while profile serialization preserves their fractional trailing zeroes.
Expected validation failures are shown explicitly; unexpected errors are not
broadly swallowed.

## Development and verification

```bash
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
uv run python -m scripts.check_release_artifacts
git diff --check
```

`uv.lock` pins runtime and development dependencies; `--locked` rejects a stale
lockfile. Streamlit and openpyxl are runtime dependencies; pytest and Ruff are development
dependencies. Normal `uv build` uses an isolated Hatchling build to create a source
distribution and wheel in the ignored `dist/` directory. Explicit build selection
limits the wheel to runtime package files and metadata. The source distribution
retains source, tests, developer scripts, project metadata, README, synthetic
samples, the screenshot, CI workflow, and local Streamlit configuration.

Build exclusions also reject `%SystemDrive%/`, `benchmark_output/`, caches, build
output, virtual environments, review patches, and IDE files, even when present in
the local tree. `.gitignore` provides additional workspace hygiene. The stdlib
artifact guard checks both archives without extraction: exact intended file sets,
metadata versions, and source bytes must match the current checkout. The documented
app workflow uses that checkout, including its local configuration and samples.

[CI](.github/workflows/ci.yml) runs these gates on every push and pull request
using Python **3.12** (the minimum) and **3.14**. It starts from a clean checkout,
installs locked dependencies without restoring an environment cache, and normally
builds both package formats with isolation. Deliberately seeded local-artifact
canaries exercise packaging exclusions before the artifact guard inspects both
archives. Actions are pinned to commits; uv is pinned to **0.12.23**.
There is no deployment or publishing step.

Tests cover both modes, grouped cardinalities and evidence, row accounting and
control-total invariants, duplicate ambiguity in Unique records mode,
Decimal precision and context isolation, invalid monetary text, CSV validation,
encoding, mappings, missing versus zero amounts, evidence and export read-back.
Streamlit [AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)
exercises uploads, mappings, the sample workflow, source evidence, error states,
download visibility, and stale-result invalidation. Tolerance tests cover inclusive
boundaries, high precision, invalid types and values, duplicate and one-sided
precedence, accepted variance totals, separate review, and export exclusion.
Review tests cover display-only filtering/sorting, correct filtered-row evidence,
and complete exports. Artifact tests cover archive contamination, omissions,
metadata, and source mismatches. Profile tests cover strict version 1/2 schema
validation, migration to Unique records, exact Decimal round trips, key order,
directional compatibility, atomic application, editable controls, download
validity, configuration privacy, and stale-result clearing. XLSX tests cover
worksheet selection, cell conversion, formula/error rejection, row references,
corrupt inputs, resource closure, mixed-format results, and stale worksheet changes.

## Realistic-data validation (developer tooling)

The seeded generator in `scripts/synthetic_data.py` creates entirely synthetic,
reproducible CSV pairs with different headers and a two-column key. In the retained
legacy workload, every 100 key groups include 70 equal pairs, 10 small variances,
8 larger variances, 4 A-only, 4 B-only, and 4 ambiguous groups. Credits, zeros, sub-cent values, leading-zero
identifiers, shuffled records, quoted commas/newlines, and Unicode are included.
Expected counts and totals come from construction plans and integer
ten-thousandths; the generator does not import TallyDiff.

Run the normal CI-sized integration validation or the developer benchmark:

```bash
uv run pytest tests/test_realistic_data.py
uv run python -m scripts.benchmark --memory
uv run python -m scripts.benchmark --groups 50000 --tolerances 0.01 --profile
```

The benchmark defaults to the grouped-detail workload at 1,000, 10,000, and
50,000 **key groups**, comparing both `unique` and `grouped_by_key` with exact
and `0.01` tolerance on the same seeded exports. Every 100 groups include
40 one-to-one exact, 10 small variances, 10 mismatches, 10 one-sided keys,
and 30 multi-row keys: split totals, credits, zeros, offsets, large sub-cent
amounts, and zero-net one-sided groups. Use `--workload legacy --modes unique`
for the earlier workload, or `--groups 10000 50000 100000 --tolerances 0.01`
for a larger comparison. It separately times CSV generation, header
inspection/mapping/ingestion, reconciliation, exception serialization, and
independent verification, plus
total elapsed time. Verification checks every key/category, group amounts and
signed deltas, counts, control and tolerated totals, exact source-row accounting,
and exported exception keys against the independent integer construction plans.
This benchmark exercises CSV ingestion; its timings do not measure XLSX parsing.
No product behavior is changed by these scripts.

`--memory` adds a separate, slower `tracemalloc` pass so instrumentation does
not distort the reported timing pass. Its peak is an estimate of Python
allocations across the fixture and pipeline, including expected results; it is
not whole-process RSS or native Arrow/browser memory. `--profile` prints
standard-library cProfile cumulative costs from another instrumented pass.

For manual testing in Streamlit, explicitly write a larger pair:

```bash
uv run python -m scripts.synthetic_data --groups 10000 --seed 42 --write
```

This writes CSVs and an expected-results summary only under the ignored
`benchmark_output/` directory. Map `Vendor ID` ↔ `Supplier` and
`Invoice Number` ↔ `Invoice Ref`, with `Invoice Amount` ↔ `Gross Amount`.
The summary defaults to tolerance `0.01`; use `--tolerance 0` for exact mode.
Add `--workload grouped --mode grouped_by_key` to generate the grouped-detail
workload and its grouped expected-results summary. Without `--write`, generation
stays in memory. Do not commit generated files.

Independent deterministic synthetic validation has exercised **100,000 key
groups** in both modes, including row accounting, control totals, exception
exports, and repeatability. Integration tests also validate CSV ↔ CSV, CSV ↔ XLSX,
XLSX ↔ CSV, and XLSX ↔ XLSX in both modes. These checks use synthetic data only.

Timings depend on hardware, Python version, and workload composition. They are
developer observations, not CI performance thresholds or capacity guarantees.

## Python API contracts

### Reconciliation kernel

`reconcile(records_a, records_b, *, amount_tolerance=Decimal("0"),
mode=ReconciliationMode.UNIQUE)` defaults to Unique records and exact equality.
`mode` must be a `ReconciliationMode` enum member (`UNIQUE` or `GROUPED_BY_KEY`);
strings and other types raise `TypeError` before inputs are consumed. Tolerance must be a
finite, nonnegative `Decimal`; other types raise `TypeError`, and non-finite or
negative Decimals raise `ValueError`. No numeric coercion is performed.

`ReconciliationResult.mode` and `.amount_tolerance` record the configured values.
`result.tolerated_findings` contains accepted nonzero differences, and
`result.tolerated_delta_total` is their exact signed net delta.
`finding.is_exception` and `result.exceptions` exclude exact and within-tolerance
groups. `result.findings`, control totals, and the integrity invariant include
every group, including accepted variances.

- Inputs are already-parsed `SourceRecord` objects. Amounts must be finite `Decimal`
  values; floats, other numeric types, NaN, and infinity are rejected.
- Each row has a `Source` enum member, a positive integer source row number, and a
  nonempty tuple of nonblank strings as its key. Row numbers must be unique within
  each file; the same row number may appear in both files.
- Keys are compared exactly, without trimming or case conversion. Raw fields are
  preserved in an immutable copy for traceability.
- Every input row appears in exactly one finding. In Unique records mode, multiple
  rows on either side produce one ambiguous group containing all rows, even with
  equal totals. In grouped mode, all exact-key rows contribute to their side total;
  a missing side remains one-sided regardless of its opposite net amount.
- Findings are sorted by key, and rows within each finding by source row number.
  Inputs are snapshotted once, including one-pass iterables.
- Totals and deltas use exact Decimal arithmetic isolated from the caller's Decimal
  context. No rounding or quantization is applied; tolerance affects classification only.

### Mapping profiles

`ColumnMapping` now lives in `tallydiff.configuration` because both profiles and
the UI use it. The existing `tallydiff.presentation.ColumnMapping` import remains
available for compatibility, and public profile APIs are exposed via `tallydiff`:

- `export_mapping_profile(mapping, *, amount_tolerance=Decimal("0"),
  reconciliation_mode=ReconciliationMode.UNIQUE) -> bytes`
  validates a complete configuration and returns UTF-8 JSON without writing files.
- `load_mapping_profile(data: bytes | str) -> MappingProfile` returns immutable,
  validated `mapping`, `amount_tolerance`, and `reconciliation_mode` fields. Schema
  version 1 loads with `ReconciliationMode.UNIQUE`; current exports use version 2.
  It accepts a UTF-8 BOM on byte input and raises `ProfileError` for invalid contents.
- `profile.validate_columns(columns_a, columns_b)` raises `ProfileError` listing
  missing directional columns; extra columns are allowed. Call it before applying
  settings to currently uploaded files.

These APIs do not ingest financial records, run reconciliation, or access the
filesystem. Profiles configure the existing reconciliation path.

### CSV ingestion

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
  to classify according to the selected reconciliation mode.
- Caller-owned streams are consumed from their current position and never closed.
  Open files with `newline=""` and an explicit encoding. For UTF-8 files with an
  optional byte-order mark, use `encoding="utf-8-sig"`; ingestion does not infer
  encodings or remove a byte-order mark from already-decoded text.

`IngestionError` exposes `source`, `source_row`, `column`, `value`, and `reason`
when applicable. Display `str(error)` for a concise diagnostic instead of a
traceback. Ingestion stops at the first error; it does not return partial results.

### XLSX ingestion

`inspect_xlsx_sheets(data: bytes, *, source=...)` lists ordinary worksheets in
stored order, including hidden worksheets. `inspect_xlsx_columns(data, *, source=...,
worksheet=...)` validates the selected sheet's row-1 text headers.

`ingest_xlsx(data, *, source=..., worksheet=..., key_columns=..., amount_column=...)`
validates one worksheet and returns the same immutable `SourceRecord` tuple as CSV
ingestion. `source_row` is the actual worksheet row. Workbooks load read-only and
close on success or failure. See the [XLSX policies](#xlsx-worksheet-and-cell-policies)
for cell conversion, formulas, evidence, and blank-row behavior.

Expected workbook/input failures raise contextual `IngestionError`; unexpected
downstream programming errors propagate. `IngestionError` also exposes `worksheet`
for XLSX diagnostics. The engine and mapping-profile APIs remain format-agnostic.

### Monetary text grammar

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

## Known limitations

- Each run compares two CSV/XLSX inputs with one amount column per side. CSV must
  be comma-delimited UTF-8 (optional BOM); monetary text uses U.S.-style syntax.
  `.xlsx` is supported; `.xls`, `.xlsm`, templates, and encrypted workbooks are not.
  There is no Excel export, locale inference, currency conversion, or automatic
  sign correction.
- XLSX uses one ordinary worksheet with text headers in row 1. Selected key/amount
  formulas are rejected; formulas are never recalculated and cached results are
  never used. Arbitrary layouts, merged headers, table/pivot interpretation, named
  ranges, and reconciling several worksheets at once are unsupported.
- XLSX evidence represents underlying cell values, not Excel display formatting,
  styles, comments, or calculated formula values. Numeric identifiers do not
  reconstruct leading zeroes; store meaningful leading zeroes as text. Date/time
  keys use openpyxl's ISO representation and may differ from a text date export.
- Column mappings require exact names on the configured side. Key matching trims
  surrounding whitespace only, with no further automatic key normalization,
  automatic mapping, or fuzzy matching. Grouped mode aggregates only by that
  explicit exact key; it does not infer individual-row correspondence, subset-sum
  matches, allocations, or many-to-many matching. Tolerance is one global absolute
  amount, with no percentage or per-row tolerance. TallyDiff does not determine
  authority, make accounting decisions, or automate journal entries.
- Configuration reuse uses explicit schema-version-2 JSON profile downloads,
  with valid version 1 profiles supported as Unique records.
  Profiles require compatible columns and contain no financial records, results,
  source filenames, or hashes. There is no managed profile library, database,
  reconciliation history, accounting-system integration, authentication, or cloud
  service. Run locally from the repository root, not as a shared hosted service.
- Inputs, evidence, and reports are held in memory. There is no streaming
  reconciliation or large-file capacity guarantee. Developer benchmarks measure
  the documented synthetic CSV workload, not XLSX parsing or whole-browser memory.
- CSV reports retain key text and exact numeric strings. Import untrusted reports
  as text with formula evaluation disabled; CSV quoting does not prevent spreadsheet
  formula execution. Composite-key labels joined with ` / ` are display text, not
  a reversible key serialization; use the original inputs and source references.

No real or confidential financial data should be committed to this repository.
The repository intentionally has no software license; that decision remains with the owner.
