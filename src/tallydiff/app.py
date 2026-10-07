"""Run with: uv run streamlit run src/tallydiff/app.py."""

from collections import Counter

import streamlit as st

from tallydiff import (
    FindingCategory,
    IngestionError,
    ReconciliationIntegrityError,
    ReconciliationResult,
    Source,
    SourceRecord,
    export_exceptions_csv,
    ingest_csv,
    inspect_csv_columns,
    reconcile,
)
from tallydiff.presentation import (
    CATEGORY_LABELS,
    ColumnMapping,
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


def _choose_file(source: Source) -> tuple[str, bytes, str, tuple[str, ...]] | None:
    st.subheader(f"File {source.value}")
    upload = st.file_uploader(
        f"File {source.value} CSV",
        type=["csv"],
        key=f"upload_{source.value}",
        on_change=_reset_mapping,
    )
    if upload is None:
        return None
    st.text(upload.name)
    data = upload.getvalue()
    try:
        text = decode_upload(data, source=source)
        columns = inspect_csv_columns(text, source=source)
    except IngestionError as exc:
        st.error(str(exc))
        return None
    st.caption("Detected columns")
    st.text(" | ".join(columns))
    return upload.name, data, text, columns


def _map_columns(columns_a: tuple[str, ...], columns_b: tuple[str, ...]) -> ColumnMapping:
    st.header("2. Map columns")
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


def _show_results(
    result: ReconciliationResult, mapping: ColumnMapping, identity: str, name_a: str, name_b: str
) -> None:
    st.header("4. Review results")
    st.caption("Configuration used for this reconciliation")
    st.text(f"File A: {name_a}\nFile B: {name_b}")
    for index, (left, right) in enumerate(mapping.key_pairs, start=1):
        st.text(f"Key {index}: {left}  \u2194  {right}")
    st.text(f"Amount: {mapping.amount_a}  \u2194  {mapping.amount_b}")

    a, b, difference = st.columns(3)
    a.metric("File A control total", display_amount(result.total_a))
    b.metric("File B control total", display_amount(result.total_b))
    difference.metric(
        "Net difference (A - B)", display_amount(result.control_difference, signed=True)
    )
    counts = Counter(finding.category for finding in result.findings)
    for column, category in zip(st.columns(5), FindingCategory, strict=True):
        column.metric(CATEGORY_LABELS[category], str(counts[category]))
    st.caption(
        "Counts are key groups. A duplicate / ambiguous group can contain multiple source rows."
    )

    exceptions = result.exceptions
    if not result.findings:
        st.info("Both files contain headers only; there are no data records to reconcile.")
        return
    if not exceptions:
        st.success("All key groups match exactly. No exceptions to review.")
        return
    if result.control_difference == 0:
        st.warning(f"Control totals agree, but {len(exceptions)} key groups still require review.")
    else:
        st.warning(f"{len(exceptions)} key groups require review.")
    st.subheader("Exceptions")
    st.download_button(
        "Download exception report",
        data=export_exceptions_csv(result),
        file_name="tallydiff_exceptions.csv",
        mime="text/csv; charset=utf-8",
        key=f"download_{identity}",
        on_click="ignore",
    )
    st.caption("Select a row to inspect its original source records below.")
    event = st.dataframe(
        finding_rows(exceptions),
        hide_index=True,
        width="stretch",
        key=f"exceptions_{identity}",
        on_select="rerun",
        selection_mode="single-row",
        lazy=False,
        height=min(440, 35 * (len(exceptions) + 1) + 3),
    )
    if event.selection.rows:
        finding = exceptions[event.selection.rows[0]]
        st.text(f"Selected key: {' / '.join(finding.key)}")
        st.caption(
            "Original values as parsed from CSV. Source numbers count records, not physical lines."
        )
        left, right = st.columns(2)
        with left:
            _show_evidence(finding.rows_a, Source.A)
        with right:
            _show_evidence(finding.rows_b, Source.B)


def main() -> None:
    st.set_page_config(page_title="TallyDiff", layout="wide")
    st.title("TallyDiff")
    st.caption("Compare two CSV exports and trace every exception to its source records.")
    st.header("1. Choose files")
    st.caption(
        "UTF-8 CSV, with or without BOM. Amounts use U.S.-style decimals and thousands commas."
    )
    st.session_state.setdefault("key_count", 1)
    left, right = st.columns(2)
    with left:
        file_a = _choose_file(Source.A)
    with right:
        file_b = _choose_file(Source.B)
    if file_a is None or file_b is None:
        st.session_state.pop("completed", None)
        st.info("Upload both CSV files to map their columns.")
        return
    name_a, data_a, text_a, columns_a = file_a
    name_b, data_b, text_b, columns_b = file_b
    mapping = _map_columns(columns_a, columns_b)
    identity = configuration_id(data_a, data_b, mapping, name_a=name_a, name_b=name_b)
    completed = st.session_state.get("completed")
    if completed is not None and completed[0] != identity:
        st.session_state.pop("completed")
    problem = mapping.problem(columns_a, columns_b)
    st.header("3. Run reconciliation")
    if problem:
        st.info(problem)
    if st.button("Run reconciliation", type="primary", key="run", disabled=problem is not None):
        st.session_state.pop("completed", None)
        records = []
        for source, text, keys, amount in (
            (Source.A, text_a, mapping.keys_a, mapping.amount_a),
            (Source.B, text_b, mapping.keys_b, mapping.amount_b),
        ):
            try:
                records.append(
                    ingest_csv(text, source=source, key_columns=keys, amount_column=amount)
                )
            except IngestionError as exc:
                st.error(str(exc))
        if len(records) == 2:
            try:
                result = reconcile(records[0], records[1])
            except ReconciliationIntegrityError as exc:
                st.error(f"Reconciliation integrity check failed: {exc}")
            else:
                st.session_state.completed = (identity, result)
    completed = st.session_state.get("completed")
    if completed is not None and completed[0] == identity:
        _show_results(completed[1], mapping, identity, name_a, name_b)


if __name__ == "__main__":
    main()
