"""Run with: uv run streamlit run src/tallydiff/app.py."""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import streamlit as st

from tallydiff import (
    AmountParseError,
    ColumnMapping,
    FindingCategory,
    IngestionError,
    ProfileError,
    ReconciliationFinding,
    ReconciliationIntegrityError,
    ReconciliationMode,
    ReconciliationResult,
    Source,
    SourceRecord,
    export_exceptions_csv,
    export_mapping_profile,
    ingest_csv,
    ingest_xlsx,
    inspect_csv_columns,
    inspect_xlsx_columns,
    inspect_xlsx_sheets,
    load_mapping_profile,
    parse_amount,
    reconcile,
)
from tallydiff.presentation import (
    CATEGORY_LABELS,
    MODE_LABELS,
    configuration_id,
    decode_upload,
    display_amount,
    evidence_rows,
    finding_rows,
)


def _reset_mapping() -> None:
    for key in list(st.session_state):
        if key.startswith("map_") or key == "completed":
            del st.session_state[key]
    st.session_state.key_count = 1


def _change_key_count(change: int) -> None:
    st.session_state.key_count += change
    if change < 0:
        for side in ("a", "b"):
            st.session_state.pop(f"map_key_{side}_{st.session_state.key_count}", None)
    st.session_state.pop("completed", None)


@dataclass(frozen=True)
class _Input:
    name: str
    data: bytes
    columns: tuple[str, ...]
    worksheet: str | None = None
    text: str | None = None

    @property
    def label(self) -> str:
        return (
            self.name if self.worksheet is None else f"{self.name} — worksheet {self.worksheet!r}"
        )


def _choose_file(source: Source) -> _Input | None:
    st.subheader(f"File {source.value}")
    upload = st.file_uploader(
        f"File {source.value} CSV or XLSX",
        type=["csv", "xlsx"],
        key=f"upload_{source.value}",
        on_change=_reset_mapping,
    )
    if upload is None:
        return None
    st.text(upload.name)
    data = upload.getvalue()
    suffix = Path(upload.name).suffix.lower()
    worksheet = None
    text = None
    try:
        if suffix == ".csv":
            st.caption("Detected file type: CSV")
            text = decode_upload(data, source=source)
            columns = inspect_csv_columns(text, source=source)
        elif suffix == ".xlsx":
            st.caption("Detected file type: XLSX")
            sheets = inspect_xlsx_sheets(data, source=source)
            sheet_key = sha256(upload.name.encode("utf-8") + b"\0" + data).hexdigest()
            worksheet = st.selectbox(
                f"File {source.value} worksheet",
                sheets,
                index=0 if len(sheets) == 1 else None,
                key=f"sheet_{source.value}_{sheet_key}",
                placeholder="Choose a worksheet",
                on_change=_reset_mapping,
                help="A single worksheet is selected automatically. "
                "For multiple worksheets, choose explicitly. "
                "Hidden worksheets are included.",
            )
            if worksheet is None:
                st.info(f"Choose a worksheet for File {source.value} to detect its columns.")
                return None
            columns = inspect_xlsx_columns(data, source=source, worksheet=worksheet)
        else:
            raise IngestionError(source, "upload a .csv or .xlsx file")
    except IngestionError as exc:
        st.error(str(exc))
        return None
    st.caption("Detected columns")
    st.text(" | ".join(columns))
    return _Input(upload.name, data, columns, worksheet, text)


def _profile_controls(columns_a: tuple[str, ...], columns_b: tuple[str, ...]) -> None:
    st.subheader("Mapping profile")
    st.caption(
        "Reuse column mappings, amount tolerance, and reconciliation mode "
        "with a local JSON profile. "
        "Profiles contain no uploaded records or reconciliation results."
    )
    upload = st.file_uploader("Mapping profile JSON", type=["json"], key="profile_upload")
    if st.button("Apply profile", key="apply_profile", disabled=upload is None):
        try:
            profile = load_mapping_profile(upload.getvalue())
            profile.validate_columns(columns_a, columns_b)
        except ProfileError as exc:
            st.error(str(exc))
        else:
            # These widgets have not been instantiated on this run. Validate everything
            # before replacing any state, so failed application cannot partially apply.
            _reset_mapping()
            st.session_state.key_count = len(profile.mapping.key_pairs)
            for index, (a, b) in enumerate(profile.mapping.key_pairs):
                st.session_state[f"map_key_a_{index}"] = a
                st.session_state[f"map_key_b_{index}"] = b
            st.session_state.map_amount_a = profile.mapping.amount_a
            st.session_state.map_amount_b = profile.mapping.amount_b
            st.session_state.amount_tolerance = format(profile.amount_tolerance, "f")
            st.session_state.reconciliation_mode = profile.reconciliation_mode
            st.session_state.profile_notice = True
            st.rerun()
    if st.session_state.pop("profile_notice", False):
        st.success("Mapping profile applied. You can still edit any configuration value.")


def _map_columns(columns_a: tuple[str, ...], columns_b: tuple[str, ...]) -> ColumnMapping:
    st.caption("Each numbered pair defines one matching key field, in this order.")
    pairs = []
    for index in range(st.session_state.key_count):
        left, right = st.columns(2)
        with left:
            a = st.selectbox(
                f"File A key field {index + 1}",
                columns_a,
                index=None,
                key=f"map_key_a_{index}",
                placeholder="Choose a column",
            )
        with right:
            b = st.selectbox(
                f"File B key field {index + 1}",
                columns_b,
                index=None,
                key=f"map_key_b_{index}",
                placeholder="Choose a column",
            )
        pairs.append((a, b))
    add, remove = st.columns(2)
    add.button(
        "+ Add key field",
        key="add_key",
        on_click=_change_key_count,
        args=(1,),
        disabled=st.session_state.key_count >= min(len(columns_a), len(columns_b)),
    )
    remove.button(
        "Remove last key field",
        key="remove_key",
        on_click=_change_key_count,
        args=(-1,),
        disabled=st.session_state.key_count == 1,
    )
    left, right = st.columns(2)
    with left:
        amount_a = st.selectbox(
            "File A amount field",
            columns_a,
            index=None,
            key="map_amount_a",
            placeholder="Choose an amount column",
        )
    with right:
        amount_b = st.selectbox(
            "File B amount field",
            columns_b,
            index=None,
            key="map_amount_b",
            placeholder="Choose an amount column",
        )
    return ColumnMapping(tuple(pairs), amount_a, amount_b)


def _show_evidence(rows: tuple[SourceRecord, ...], source: Source) -> None:
    st.subheader(f"File {source.value} evidence")
    if not rows:
        st.info(f"No File {source.value} records for this key.")
    for row in rows:
        with st.container(border=True):
            st.caption(f"Source record {row.source_row}")
            st.dataframe(evidence_rows(row), hide_index=True, width="stretch")


def _show_finding_table(findings: tuple[ReconciliationFinding, ...], *, table_key: str) -> None:
    st.caption("Select a row to inspect its original source records below.")
    event = st.dataframe(
        finding_rows(findings),
        hide_index=True,
        width="stretch",
        key=table_key,
        on_select="rerun",
        selection_mode="single-row",
        lazy=False,
        height=min(440, 35 * (len(findings) + 1) + 3),
    )
    if event.selection.rows:
        finding = findings[event.selection.rows[0]]
        st.text(f"Selected key: {' / '.join(finding.key)}")
        st.caption(
            "CSV evidence preserves parsed text; source numbers count logical records. "
            "XLSX evidence shows underlying cell values, not display formatting; "
            "source numbers are worksheet row numbers."
        )
        left, right = st.columns(2)
        with left:
            _show_evidence(finding.rows_a, Source.A)
        with right:
            _show_evidence(finding.rows_b, Source.B)


def _show_results(
    result: ReconciliationResult, mapping: ColumnMapping, identity: str, name_a: str, name_b: str
) -> None:
    st.header("4. Review results")
    st.caption("Configuration used for this reconciliation")
    st.text(f"File A: {name_a}\nFile B: {name_b}")
    for index, (left, right) in enumerate(mapping.key_pairs, start=1):
        st.text(f"Key {index}: {left}  \u2194  {right}")
    st.text(f"Amount: {mapping.amount_a}  \u2194  {mapping.amount_b}")
    st.text(f"Amount tolerance: {display_amount(result.amount_tolerance)} (absolute difference)")
    st.text(f"Reconciliation mode: {MODE_LABELS[result.mode]}")
    if result.mode is ReconciliationMode.GROUPED_BY_KEY:
        st.caption(
            "Grouped results compare totals for each matching key; "
            "they do not claim that individual rows correspond."
        )

    a, b, difference = st.columns(3)
    a.metric("File A control total", display_amount(result.total_a))
    b.metric("File B control total", display_amount(result.total_b))
    difference.metric(
        "Net difference (A - B)", display_amount(result.control_difference, signed=True)
    )
    counts = Counter(finding.category for finding in result.findings)
    for column, category in zip(st.columns(len(FindingCategory)), FindingCategory, strict=True):
        column.metric(CATEGORY_LABELS[category], str(counts[category]))
    st.caption("Counts are matching key groups. Each group can contain multiple source rows.")

    exceptions = result.exceptions
    tolerated = result.tolerated_findings
    if not result.findings:
        st.info("Both files contain headers only; there are no data records to reconcile.")
        return
    if not exceptions:
        if tolerated:
            st.success("Reconciled within configured tolerance. No exceptions to review.")
        else:
            st.success(
                "All matching key totals agree exactly. No exceptions to review."
                if result.mode is ReconciliationMode.GROUPED_BY_KEY
                else "All key groups match exactly. No exceptions to review."
            )
    elif result.control_difference == 0:
        st.warning(f"Control totals agree, but {len(exceptions)} key groups still require review.")
    else:
        st.warning(f"{len(exceptions)} key groups require review.")
    if exceptions:
        st.subheader("Exceptions")
        st.download_button(
            "Download exception report",
            data=export_exceptions_csv(result),
            file_name="tallydiff_exceptions.csv",
            mime="text/csv; charset=utf-8",
            key=f"download_{identity}",
            on_click="ignore",
        )
        _show_finding_table(exceptions, table_key=f"exceptions_{identity}")
    if tolerated:
        st.subheader("Within tolerance")
        st.metric(
            "Net accepted variance (A - B)",
            display_amount(result.tolerated_delta_total, signed=True),
        )
        st.caption(
            "Accepted nonzero differences are included in control totals and excluded from the "
            "exception report. Opposing accepted deltas can cancel in this net amount."
        )
        _show_finding_table(tolerated, table_key=f"tolerated_{identity}")
    if result.mode is ReconciliationMode.GROUPED_BY_KEY:
        exact = tuple(
            finding
            for finding in result.findings
            if finding.category is FindingCategory.EXACT_MATCH
            and (len(finding.rows_a) > 1 or len(finding.rows_b) > 1)
        )
        if exact:
            st.subheader("Grouped exact key totals")
            _show_finding_table(exact, table_key=f"exact_{identity}")


def _clear_result() -> None:
    st.session_state.pop("completed", None)


def main() -> None:
    st.set_page_config(page_title="TallyDiff", layout="wide")
    st.title("TallyDiff")
    st.caption("Compare two CSV or XLSX exports and trace every exception to its source records.")
    st.header("1. Choose files")
    st.caption(
        "UTF-8 CSV (with or without BOM), or structured XLSX with headers in row 1. "
        "Choose one worksheet per workbook. Selected formulas are blocked; paste/export as values."
    )
    st.session_state.setdefault("key_count", 1)
    left, right = st.columns(2)
    with left:
        file_a = _choose_file(Source.A)
    with right:
        file_b = _choose_file(Source.B)
    if file_a is None or file_b is None:
        st.session_state.pop("completed", None)
        st.info("Upload both files and select any required worksheets to map their columns.")
        return
    columns_a, columns_b = file_a.columns, file_b.columns
    st.header("2. Map columns")
    _profile_controls(columns_a, columns_b)
    mapping = _map_columns(columns_a, columns_b)
    st.header("3. Run reconciliation")
    st.session_state.setdefault("reconciliation_mode", ReconciliationMode.UNIQUE)
    mode = st.radio(
        "Reconciliation mode",
        tuple(ReconciliationMode),
        format_func=MODE_LABELS.__getitem__,
        key="reconciliation_mode",
        horizontal=True,
        on_change=_clear_result,
    )
    st.caption(
        "Rows sharing the same matching key are totaled on each side "
        "and the key totals are compared. "
        "This does not claim that individual rows correspond."
        if mode is ReconciliationMode.GROUPED_BY_KEY
        else "Duplicate matching keys remain ambiguous and require review."
    )
    st.session_state.setdefault("amount_tolerance", "0")
    tolerance_text = st.text_input(
        "Amount tolerance",
        value=None,
        key="amount_tolerance",
        help="Maximum absolute difference between amounts for a matching key, "
        "in your amount units. "
        "The boundary is inclusive; 0 requires exact equality. Actual deltas stay visible.",
        on_change=_clear_result,
    )
    try:
        amount_tolerance = parse_amount(tolerance_text)
        if amount_tolerance < 0:
            raise AmountParseError("must be zero or greater")
    except AmountParseError as exc:
        st.error(f"Amount tolerance: {exc}.")
        amount_tolerance = None
    identity = (
        configuration_id(
            file_a.data,
            file_b.data,
            mapping,
            name_a=file_a.name,
            name_b=file_b.name,
            worksheet_a=file_a.worksheet,
            worksheet_b=file_b.worksheet,
            amount_tolerance=amount_tolerance,
            reconciliation_mode=mode,
        )
        if amount_tolerance is not None
        else None
    )
    completed = st.session_state.get("completed")
    if completed is not None and completed[0] != identity:
        st.session_state.pop("completed")
    problem = mapping.problem(columns_a, columns_b)
    if problem:
        st.info(problem)
    elif amount_tolerance is not None:
        st.download_button(
            "Download mapping profile",
            data=export_mapping_profile(
                mapping, amount_tolerance=amount_tolerance, reconciliation_mode=mode
            ),
            file_name="tallydiff_profile.json",
            mime="application/json",
            key="download_profile",
            on_click="ignore",
        )
    if st.button(
        "Run reconciliation",
        type="primary",
        key="run",
        disabled=problem is not None or amount_tolerance is None,
    ):
        st.session_state.pop("completed", None)
        records = []
        for source, input_file, keys, amount in (
            (Source.A, file_a, mapping.keys_a, mapping.amount_a),
            (Source.B, file_b, mapping.keys_b, mapping.amount_b),
        ):
            try:
                if input_file.worksheet is None:
                    parsed = ingest_csv(
                        input_file.text, source=source, key_columns=keys, amount_column=amount
                    )
                else:
                    parsed = ingest_xlsx(
                        input_file.data,
                        source=source,
                        worksheet=input_file.worksheet,
                        key_columns=keys,
                        amount_column=amount,
                    )
                records.append(parsed)
            except IngestionError as exc:
                st.error(str(exc))
        if len(records) == 2:
            try:
                result = reconcile(
                    records[0], records[1], amount_tolerance=amount_tolerance, mode=mode
                )
            except ReconciliationIntegrityError as exc:
                st.error(f"Reconciliation integrity check failed: {exc}")
            else:
                st.session_state.completed = (identity, result)
    completed = st.session_state.get("completed")
    if completed is not None and completed[0] == identity:
        _show_results(completed[1], mapping, identity, file_a.label, file_b.label)


if __name__ == "__main__":
    main()
