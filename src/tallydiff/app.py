"""Run with: uv run streamlit run src/tallydiff/app.py."""

import json
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from tallydiff import (
    AmountParseError,
    ColumnMapping,
    ComparisonFieldMapping,
    CorrespondenceReason,
    CorrespondenceStatus,
    FindingCategory,
    FindingReview,
    IngestionError,
    KeyNormalizationConfig,
    KeyNormalizationError,
    KeyNormalizationRules,
    NormalizationCollisionError,
    OneToManyPolicy,
    ProfileError,
    ReconciliationFinding,
    ReconciliationIntegrityError,
    ReconciliationMode,
    ReconciliationResult,
    ReviewBinding,
    ReviewDisposition,
    ReviewSession,
    ReviewSessionError,
    Source,
    SourceRecord,
    bind_review_result,
    export_exceptions_csv,
    export_mapping_profile,
    export_review_session,
    export_reviewed_exceptions_csv,
    ingest_csv,
    ingest_xlsx,
    inspect_csv_columns,
    inspect_xlsx_columns,
    inspect_xlsx_sheets,
    load_mapping_profile,
    load_review_session,
    parse_amount,
    reconcile,
)
from tallydiff.presentation import (
    CATEGORY_LABELS,
    CORRESPONDENCE_REASON_LABELS,
    CORRESPONDENCE_STATUS_LABELS,
    EXCEPTION_CATEGORIES,
    EXCEPTION_SORT_LABELS,
    MODE_LABELS,
    NORMALIZATION_LABELS,
    REVIEW_DISPOSITION_LABELS,
    configuration_id,
    decode_upload,
    display_amount,
    evidence_rows,
    filter_review_dispositions,
    finding_rows,
    review_exceptions,
    review_finding_rows,
)

_NORMALIZATION_HELP = {
    "casefold": "Compare letter case with Unicode casefolding. Accents are preserved.",
    "collapse_whitespace": "Collapse Unicode whitespace runs to one space and trim edges.",
    "remove_punctuation": "Remove Unicode punctuation. "
    "Currency, mathematical symbols and emoji remain.",
    "strip_leading_zeros": "Strip zeros only from ASCII digit-only values at this stage. "
    "All-zero identifiers become 0.",
}
_REVIEW_STATE_KEY = "_review_state"
_REVIEW_BINDING_KEY = "_review_binding"
_REVIEW_NOTE_DRAFTS_KEY = "_review_note_drafts"
_REVIEW_NOTE_ERRORS_KEY = "_review_note_errors"
_REVIEW_EDITOR_SELECTION_KEY = "_review_editor_selection"
_REVIEW_UPLOAD_KEY = "review_session_upload"
_REVIEW_APPLY_KEY = "review_apply_session"
_REVIEW_PREPARE_KEY = "review_prepare_downloads"
_REVIEW_PREPARED_KEY = "_review_prepared_downloads"
_REVIEW_PREPARE_REQUEST_KEY = "_review_prepare_requested"
_REVIEW_PREPARE_ERROR_KEY = "_review_prepare_error"
_REVIEW_IMPORT_ERROR_KEY = "_review_import_error"
_REVIEW_APPLY_NOTICE_KEY = "_review_apply_notice"
_REVIEW_DOWNLOAD_CSV_KEY = "review_download_csv"
_REVIEW_DOWNLOAD_SESSION_KEY = "review_download_session"


@dataclass(frozen=True, slots=True)
class _PreparedReviewDownloads:
    """Stable complete committed artifacts, scoped by binding and annotation revision."""

    result_fingerprint: str
    annotation_revision: int
    session_json: bytes
    reviewed_csv: bytes


@dataclass(slots=True)
class _ReviewRuntimeState:
    """App-owned annotations; immutable domain snapshots remain the export boundary."""

    binding: ReviewBinding
    reviews_by_key: dict[tuple[str, ...], FindingReview]
    annotation_revision: int = 0
    editor_generation: int = 0

    def snapshot(self) -> ReviewSession:
        """Validate exact dictionary identity/order, then use the public session model."""

        if not isinstance(self.binding, ReviewBinding):
            raise ReviewSessionError("runtime binding must be a ReviewBinding")
        if not isinstance(self.reviews_by_key, dict):
            raise ReviewSessionError("runtime reviews must be a dictionary")
        if tuple(self.reviews_by_key) != self.binding.exception_keys:
            raise ReviewSessionError(
                "runtime reviews must cover the binding exception keys in order"
            )
        for key, review in self.reviews_by_key.items():
            if not isinstance(review, FindingReview) or review.matching_key != key:
                raise ReviewSessionError(
                    "runtime review must be a FindingReview for its dictionary key"
                )
        for counter in (self.annotation_revision, self.editor_generation):
            if type(counter) is not int or counter < 0:
                raise ReviewSessionError(
                    "runtime revisions and generations must be nonnegative integers"
                )
        return ReviewSession(self.binding, tuple(self.reviews_by_key.values()))


def _adopt_review_binding(
    new_binding: ReviewBinding, previous_state: _ReviewRuntimeState | None = None
) -> _ReviewRuntimeState:
    """Transfer complete validated annotations to a new binding, or start fresh.

    Corrupted previous app state is discarded so a valid reconciliation can still
    complete. Errors in a newly constructed binding are handled by the run path.
    """

    if previous_state is not None:
        try:
            previous = _ReviewRuntimeState.snapshot(previous_state)
            if (
                previous.binding.result_fingerprint == new_binding.result_fingerprint
                and previous.binding.exception_keys == new_binding.exception_keys
            ):
                preserved = ReviewSession(new_binding, previous.reviews)
                return _ReviewRuntimeState(
                    new_binding,
                    {review.matching_key: review for review in preserved.reviews},
                    previous_state.annotation_revision,
                    previous_state.editor_generation,
                )
        except (ReviewSessionError, TypeError, AttributeError):
            pass
    initial = ReviewSession.unreviewed(new_binding)
    return _ReviewRuntimeState(
        new_binding, {review.matching_key: review for review in initial.reviews}
    )


def _clear_review_state() -> None:
    """Clear result-scoped ownership, filters, selections, and future review widgets."""

    for key in list(st.session_state):
        if key.startswith(("_review_", "review_", "exceptions_", "tolerated_", "exact_")):
            del st.session_state[key]


def _clear_result() -> None:
    st.session_state.pop("completed", None)
    _clear_review_state()


def _invalidate_review_downloads() -> None:
    st.session_state.pop(_REVIEW_PREPARED_KEY, None)


def _clear_review_editor_state() -> None:
    """Discard editors and exception selections while retaining ownership and filters."""

    for key in list(st.session_state):
        if key.startswith(("review_disposition_", "review_note_", "exceptions_")) or key in (
            _REVIEW_NOTE_DRAFTS_KEY,
            _REVIEW_NOTE_ERRORS_KEY,
            _REVIEW_EDITOR_SELECTION_KEY,
        ):
            del st.session_state[key]


def _apply_review_session(runtime: _ReviewRuntimeState, data: bytes) -> None:
    """Validate a complete candidate, then replace all annotations in one installation."""

    _ReviewRuntimeState.snapshot(runtime)
    loaded = load_review_session(data, binding=runtime.binding)
    if loaded.binding is not runtime.binding:
        raise ReviewSessionError("loaded review session must use the active binding")
    validated = ReviewSession(runtime.binding, loaded.reviews)
    replacements = {review.matching_key: review for review in validated.reviews}
    revision = runtime.annotation_revision + 1
    generation = runtime.editor_generation + 1
    runtime.reviews_by_key = replacements
    runtime.annotation_revision = revision
    runtime.editor_generation = generation
    _clear_review_editor_state()
    _invalidate_review_downloads()
    st.session_state.pop(_REVIEW_PREPARE_ERROR_KEY, None)


def _prepare_review_downloads(runtime: _ReviewRuntimeState) -> _PreparedReviewDownloads:
    """Build both complete artifacts only from a validated committed snapshot."""

    session = _ReviewRuntimeState.snapshot(runtime)
    session_json = export_review_session(session)
    reviewed_csv = export_reviewed_exceptions_csv(runtime.binding, session)
    return _PreparedReviewDownloads(
        runtime.binding.result_fingerprint,
        runtime.annotation_revision,
        session_json,
        reviewed_csv,
    )


def _current_prepared_review_downloads(
    runtime: _ReviewRuntimeState,
) -> _PreparedReviewDownloads | None:
    prepared = st.session_state.get(_REVIEW_PREPARED_KEY)
    if prepared is not None and (
        prepared.result_fingerprint != runtime.binding.result_fingerprint
        or prepared.annotation_revision != runtime.annotation_revision
    ):
        _invalidate_review_downloads()
        return None
    return prepared


def _active_review_runtime() -> _ReviewRuntimeState:
    runtime = st.session_state.get(_REVIEW_STATE_KEY)
    completed = st.session_state.get("completed")
    if runtime is None or completed is None:
        raise ReviewSessionError("there is no active completed review")
    _ReviewRuntimeState.snapshot(runtime)
    if runtime.binding.result is not completed[1]:
        raise ReviewSessionError("active review binding does not own the completed result")
    return runtime


def _retain_active_editor_selection() -> None:
    """Candidate uploads and persistence actions do not change the represented row."""

    selection = st.session_state.get(_REVIEW_EDITOR_SELECTION_KEY)
    runtime = st.session_state.get(_REVIEW_STATE_KEY)
    if (
        selection is not None
        and runtime is not None
        and isinstance(runtime.binding, ReviewBinding)
        and isinstance(runtime.reviews_by_key, dict)
    ):
        if (
            selection[0] == runtime.binding.result_fingerprint
            and selection[2] == runtime.editor_generation
            and selection[1] in runtime.reviews_by_key
        ):
            _retain_editor_selection(*selection[:3])


def _apply_uploaded_review_session() -> None:
    upload = st.session_state.get(_REVIEW_UPLOAD_KEY)
    if upload is None:
        return
    _retain_active_editor_selection()
    try:
        _apply_review_session(_active_review_runtime(), upload.getvalue())
    except ReviewSessionError as exc:
        st.session_state[_REVIEW_IMPORT_ERROR_KEY] = f"Review session: {exc}"
    else:
        st.session_state.pop(_REVIEW_IMPORT_ERROR_KEY, None)
        st.session_state[_REVIEW_APPLY_NOTICE_KEY] = True


def _request_review_downloads() -> None:
    # Defer exports until widget callbacks finish. A selected note is explicitly
    # submitted and validated by its form before this request is made.
    _retain_active_editor_selection()
    st.session_state[_REVIEW_PREPARE_REQUEST_KEY] = True


def _prepare_requested_review_downloads() -> None:
    if not st.session_state.pop(_REVIEW_PREPARE_REQUEST_KEY, False):
        return
    try:
        prepared = _prepare_review_downloads(_active_review_runtime())
    except ReviewSessionError as exc:
        _invalidate_review_downloads()
        st.session_state[_REVIEW_PREPARE_ERROR_KEY] = f"Review state: {exc}"
    else:
        st.session_state[_REVIEW_PREPARED_KEY] = prepared
        st.session_state.pop(_REVIEW_PREPARE_ERROR_KEY, None)


def _show_review_persistence(runtime: _ReviewRuntimeState) -> DeltaGenerator:
    st.subheader("Review session")
    st.caption(
        "Review changes are kept in this session. Save note saves a note without preparing "
        "downloads. Prepare review downloads saves the selected note and captures the "
        "complete saved review state."
    )
    upload = st.file_uploader(
        "Review session JSON",
        type=["json"],
        key=_REVIEW_UPLOAD_KEY,
        on_change=_retain_active_editor_selection,
    )
    st.button(
        "Apply review session",
        key=_REVIEW_APPLY_KEY,
        disabled=upload is None,
        on_click=_apply_uploaded_review_session,
    )
    if st.session_state.pop(_REVIEW_APPLY_NOTICE_KEY, False):
        st.success("Review session applied.")
    error = st.session_state.pop(_REVIEW_IMPORT_ERROR_KEY, None)
    if error is not None:
        st.error(error)
    prepare_control = st.empty()
    error = st.session_state.pop(_REVIEW_PREPARE_ERROR_KEY, None)
    if error is not None:
        st.error(error)
    prepared = _current_prepared_review_downloads(runtime)
    if prepared is not None:
        # Native download clicks ignore reruns. AppTest may still request one;
        # keep its existing selection without invoking editors or regenerating bytes.
        if st.session_state.get(_REVIEW_DOWNLOAD_CSV_KEY) or st.session_state.get(
            _REVIEW_DOWNLOAD_SESSION_KEY
        ):
            _retain_active_editor_selection()
        st.download_button(
            "Download reviewed exception report",
            data=prepared.reviewed_csv,
            file_name="tallydiff_reviewed_exceptions.csv",
            mime="text/csv; charset=utf-8",
            key=_REVIEW_DOWNLOAD_CSV_KEY,
            on_click="ignore",
        )
        st.download_button(
            "Download review session",
            data=prepared.session_json,
            file_name="tallydiff_review_session.json",
            mime="application/json",
            key=_REVIEW_DOWNLOAD_SESSION_KEY,
            on_click="ignore",
        )
    return prepare_control


def _ensure_review_state(result: ReconciliationResult) -> bool:
    """Reuse the successful-run binding and recover missing/corrupted annotations.

    The separate cached binding retains successful-run identity even if the
    mutable holder is lost. Ordinary reruns never recompute it. A completed
    result without any matching successful-run binding cannot be displayed.
    """

    binding = st.session_state.get(_REVIEW_BINDING_KEY)
    runtime = st.session_state.get(_REVIEW_STATE_KEY)
    if not isinstance(binding, ReviewBinding) or binding.result is not result:
        candidate = getattr(runtime, "binding", None)
        if isinstance(candidate, ReviewBinding) and candidate.result is result:
            binding = candidate
            st.session_state[_REVIEW_BINDING_KEY] = binding
        else:
            _clear_result()
            st.error(
                "Review binding integrity check failed: completed run has no matching binding."
            )
            return False
    try:
        snapshot = _ReviewRuntimeState.snapshot(runtime)
    except (ReviewSessionError, TypeError, AttributeError):
        snapshot = None
    if snapshot is None or snapshot.binding is not binding:
        st.session_state[_REVIEW_STATE_KEY] = _adopt_review_binding(binding)
    return True


def _review_widget_key(
    field: Literal["disposition", "note", "note_form", "note_save", "note_prepare"],
    fingerprint: str,
    matching_key: tuple[str, ...],
    generation: int,
) -> str:
    """Scope compact editor identity to a completed run, canonical key, and generation."""

    key_json = json.dumps(matching_key, ensure_ascii=False, separators=(",", ":"))
    key_digest = sha256(key_json.encode("utf-8")).hexdigest()
    return f"review_{field}_{fingerprint}_{generation}_{key_digest}"


def _callback_review_state(
    fingerprint: str, matching_key: tuple[str, ...], generation: int
) -> _ReviewRuntimeState | None:
    """Ignore obsolete callbacks; validate complete current ownership before edits."""

    runtime = st.session_state.get(_REVIEW_STATE_KEY)
    completed = st.session_state.get("completed")
    if runtime is None or completed is None:
        return None
    if not isinstance(runtime.binding, ReviewBinding):
        raise ReviewSessionError("active runtime has an invalid review binding")
    if runtime.binding.result_fingerprint != fingerprint or runtime.editor_generation != generation:
        return None
    _ReviewRuntimeState.snapshot(runtime)
    if runtime.binding.result is not completed[1]:
        raise ReviewSessionError("active review binding does not own the completed result")
    if matching_key not in runtime.reviews_by_key:
        return None
    return runtime


def _commit_review_disposition(
    fingerprint: str, matching_key: tuple[str, ...], generation: int
) -> None:
    """Commit only this field, retaining the authoritative note rather than a draft."""

    runtime = _callback_review_state(fingerprint, matching_key, generation)
    if runtime is None:
        return
    _retain_editor_selection(fingerprint, matching_key, generation)
    widget_key = _review_widget_key("disposition", fingerprint, matching_key, generation)
    current = runtime.reviews_by_key[matching_key]
    replacement = FindingReview(matching_key, st.session_state[widget_key], current.note)
    if replacement != current:
        runtime.reviews_by_key[matching_key] = replacement
        runtime.annotation_revision += 1
        _invalidate_review_downloads()


def _commit_review_note(fingerprint: str, matching_key: tuple[str, ...], generation: int) -> bool:
    """Validate a note draft without reading or committing a disposition widget."""

    runtime = _callback_review_state(fingerprint, matching_key, generation)
    if runtime is None:
        return False
    _retain_editor_selection(fingerprint, matching_key, generation)
    widget_key = _review_widget_key("note", fingerprint, matching_key, generation)
    candidate = st.session_state[widget_key]
    current = runtime.reviews_by_key[matching_key]
    try:
        replacement = FindingReview(matching_key, current.disposition, candidate)
    except (TypeError, ValueError) as exc:
        # Widget keys are cleaned up when their row is no longer rendered. Keep
        # invalid drafts separately so navigating away cannot erase corrections.
        st.session_state.setdefault(_REVIEW_NOTE_DRAFTS_KEY, {})[widget_key] = candidate
        st.session_state.setdefault(_REVIEW_NOTE_ERRORS_KEY, {})[widget_key] = (
            f"Analyst note: {exc}"
        )
        return False
    st.session_state.get(_REVIEW_NOTE_DRAFTS_KEY, {}).pop(widget_key, None)
    st.session_state.get(_REVIEW_NOTE_ERRORS_KEY, {}).pop(widget_key, None)
    if replacement != current:
        runtime.reviews_by_key[matching_key] = replacement
        runtime.annotation_revision += 1
        _invalidate_review_downloads()
    return True


def _commit_review_note_and_prepare(
    fingerprint: str, matching_key: tuple[str, ...], generation: int
) -> None:
    """Commit the form's submitted note before requesting a complete snapshot."""

    try:
        valid = _commit_review_note(fingerprint, matching_key, generation)
    except ReviewSessionError as exc:
        _invalidate_review_downloads()
        st.session_state[_REVIEW_PREPARE_ERROR_KEY] = f"Review state: {exc}"
        return
    if valid:
        _request_review_downloads()


def _retain_editor_selection(
    fingerprint: str, matching_key: tuple[str, ...], generation: int
) -> None:
    """Carry the represented row through a commit only in its existing table scope.

    A changed ordered visible population produces a different table key, so this
    cannot restore a positional selection onto a successor after disposition filtering.
    """

    selection = st.session_state.get(_REVIEW_EDITOR_SELECTION_KEY)
    if selection is not None and selection[:3] == (fingerprint, matching_key, generation):
        table_key, row_index = selection[3:]
        st.session_state[table_key] = {
            "selection": {"rows": [row_index], "columns": [], "cells": []}
        }


def _show_analyst_review(finding: ReconciliationFinding, runtime: _ReviewRuntimeState) -> None:
    """Display scoped editors before long evidence, preserving uncommitted invalid drafts."""

    review = runtime.reviews_by_key[finding.key]
    fingerprint = runtime.binding.result_fingerprint
    generation = runtime.editor_generation
    scope = (fingerprint, finding.key, generation)
    disposition_key = _review_widget_key("disposition", *scope)
    note_key = _review_widget_key("note", *scope)
    # Disposition follows committed state. Initialize the note only when absent:
    # unrelated reruns must not reset the form's still-unsubmitted frontend text.
    st.session_state[disposition_key] = review.disposition
    if note_key not in st.session_state:
        st.session_state[note_key] = st.session_state.get(_REVIEW_NOTE_DRAFTS_KEY, {}).get(
            note_key, review.note
        )
    st.subheader("Analyst review")
    st.caption(
        "Analyst review does not change the reconciliation category, financial delta, "
        "secondary evidence, or inferred-correspondence status."
    )
    st.selectbox(
        "Analyst disposition",
        tuple(ReviewDisposition),
        index=None,
        format_func=REVIEW_DISPOSITION_LABELS.__getitem__,
        key=disposition_key,
        on_change=_commit_review_disposition,
        args=scope,
    )
    with st.form(
        _review_widget_key("note_form", *scope), clear_on_submit=False, enter_to_submit=False
    ):
        st.caption(
            "Notes are saved only with Save note or Prepare review downloads. "
            "Submit the note before changing rows; unsubmitted edits are not saved."
        )
        st.text_area("Analyst note", value=None, key=note_key)
        save, prepare = st.columns(2)
        with save:
            saved = st.form_submit_button(
                "Save note",
                key=_review_widget_key("note_save", *scope),
                on_click=_commit_review_note,
                args=scope,
            )
        with prepare:
            prepared = st.form_submit_button(
                "Prepare review downloads",
                key=_review_widget_key("note_prepare", *scope),
                on_click=_commit_review_note_and_prepare,
                args=scope,
            )
    error = st.session_state.get(_REVIEW_NOTE_ERRORS_KEY, {}).get(note_key)
    if error is not None:
        st.error(error)
        st.caption(
            "This note was not saved. Existing prepared downloads still contain saved notes."
        )
    elif saved or prepared:
        st.caption("Note saved.")


def _show_review_progress(session: ReviewSession) -> None:
    st.subheader("Review progress")
    metrics = (
        ("Review items", session.total),
        ("Unreviewed", session.unreviewed_count),
        ("Needs follow-up", session.needs_follow_up_count),
        ("Review complete", session.complete_count),
    )
    for column, (label, count) in zip(st.columns(4), metrics, strict=True):
        column.metric(label, str(count))
    st.caption(f"Reviewed: {session.reviewed_count} · Open review items: {session.open_count}")


def _reset_mapping() -> None:
    _clear_result()
    for key in list(st.session_state):
        if key.startswith(("map_", "norm_")):
            del st.session_state[key]
    st.session_state.key_count = 1
    st.session_state.comparison_count = 0


def _change_key_count(change: int) -> None:
    st.session_state.key_count += change
    if change < 0:
        for side in ("a", "b"):
            st.session_state.pop(f"map_key_{side}_{st.session_state.key_count}", None)
    index = st.session_state.key_count if change < 0 else st.session_state.key_count - 1
    for name in NORMALIZATION_LABELS:
        st.session_state.pop(f"norm_{name}_{index}", None)
    _clear_result()


def _change_comparison_count(change: int) -> None:
    previous = st.session_state.comparison_count
    count = max(0, previous + change)
    st.session_state.comparison_count = count
    # Clear both removed and newly added slots so selections cannot resurrect.
    for index in range(min(previous, count), max(previous, count)):
        for side in ("a", "b"):
            st.session_state.pop(f"map_comparison_{side}_{index}", None)
    _clear_result()


@dataclass(frozen=True)
class _Input:
    name: str
    data: bytes
    columns: tuple[str, ...]
    input_format: Literal["csv", "xlsx"]
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
            input_format = "csv"
            st.caption("Detected file type: CSV")
            text = decode_upload(data, source=source)
            columns = inspect_csv_columns(text, source=source)
        elif suffix == ".xlsx":
            input_format = "xlsx"
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
    return _Input(upload.name, data, columns, input_format, worksheet, text)


def _profile_controls(columns_a: tuple[str, ...], columns_b: tuple[str, ...]) -> None:
    st.subheader("Mapping profile")
    st.caption(
        "Reuse column mappings, secondary comparisons, amount tolerance, reconciliation mode, "
        "and key normalization "
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
                rules = (
                    profile.key_normalization.component_rules[index]
                    if profile.key_normalization is not None
                    else KeyNormalizationRules()
                )
                for name in NORMALIZATION_LABELS:
                    st.session_state[f"norm_{name}_{index}"] = getattr(rules, name)
            st.session_state.comparison_count = len(profile.mapping.comparison_fields)
            for index, comparison in enumerate(profile.mapping.comparison_fields):
                st.session_state[f"map_comparison_a_{index}"] = comparison.file_a
                st.session_state[f"map_comparison_b_{index}"] = comparison.file_b
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
    st.caption(
        "Enabled rules always run in this fixed order: casefold → collapse whitespace → "
        "remove punctuation → strip leading zeros from ASCII digit-only values. "
        "Punctuation removal can expose digits to zero stripping: "
        "-001 → 1 when both rules are enabled."
    )
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
        with st.expander(f"Key {index + 1} normalization"):
            st.caption("Optional rules for this key pair only; apply equally to File A and File B.")
            rule_columns = st.columns(2)
            for rule_index, (name, label) in enumerate(NORMALIZATION_LABELS.items()):
                with rule_columns[rule_index % 2]:
                    st.checkbox(
                        label,
                        value=False,
                        key=f"norm_{name}_{index}",
                        help=_NORMALIZATION_HELP[name],
                        on_change=_clear_result,
                    )
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
    st.subheader("Secondary comparison fields — optional")
    st.caption(
        "Compare additional fields after records are matched. "
        "Each numbered pair maps a File A column ↔ a File B column. "
        "Differences require review but do not change the financial delta."
    )
    count = st.session_state.comparison_count
    if count == 0:
        st.caption("No secondary comparison fields configured.")
    # Snapshot every selection before rendering: earlier pairs must also exclude
    # selections in later pairs, while preserving their own value on conflicts.
    selections = {
        side: [st.session_state.get(f"map_comparison_{side}_{index}") for index in range(count)]
        for side in ("a", "b")
    }
    comparisons = []
    for index in range(count):
        values = []
        for column, side, columns, amount in zip(
            st.columns(2), ("a", "b"), (columns_a, columns_b), (amount_a, amount_b), strict=True
        ):
            current = selections[side][index]
            others = {value for other, value in enumerate(selections[side]) if other != index}
            options = [
                name
                for name in columns
                if name == current or (name != amount and name not in others)
            ]
            if current is not None and current not in options:
                options.append(current)
            with column:
                values.append(
                    st.selectbox(
                        f"File {side.upper()} comparison field {index + 1}",
                        options,
                        index=None,
                        key=f"map_comparison_{side}_{index}",
                        placeholder="Choose a comparison column",
                        on_change=_clear_result,
                    )
                )
        comparisons.append(ComparisonFieldMapping(*values))
    add, remove = st.columns(2)
    add.button(
        "+ Add comparison field",
        key="add_comparison",
        on_click=_change_comparison_count,
        args=(1,),
        disabled=count >= max(0, min(len(columns_a), len(columns_b)) - 1),
    )
    remove.button(
        "Remove last comparison field",
        key="remove_comparison",
        on_click=_change_comparison_count,
        args=(-1,),
        disabled=count == 0,
    )
    return ColumnMapping(tuple(pairs), amount_a, amount_b, comparison_fields=tuple(comparisons))


def _normalization_config() -> KeyNormalizationConfig | None:
    component_rules = tuple(
        KeyNormalizationRules(
            **{name: st.session_state[f"norm_{name}_{index}"] for name in NORMALIZATION_LABELS}
        )
        for index in range(st.session_state.key_count)
    )
    if not any(getattr(rules, name) for rules in component_rules for name in NORMALIZATION_LABELS):
        return None
    return KeyNormalizationConfig(component_rules)


def _show_evidence(rows: tuple[SourceRecord, ...], source: Source) -> None:
    st.subheader(f"File {source.value} evidence")
    if not rows:
        st.info(f"No File {source.value} records for this key.")
    for row in rows:
        with st.container(border=True):
            st.caption(f"Source record {row.source_row}")
            st.dataframe(evidence_rows(row), hide_index=True, width="stretch")


def _show_normalization_collisions(error: NormalizationCollisionError) -> None:
    st.error(str(error))
    st.caption(
        "Distinct original keys in the same file collapse to the same normalized key, "
        "so TallyDiff refuses to merge them automatically."
    )
    for source, collisions in ((Source.A, error.collisions_a), (Source.B, error.collisions_b)):
        if not collisions:
            continue
        st.subheader(f"File {source.value} normalization collisions")
        for collision in collisions:
            matching_key = json.dumps(collision.normalized_key, ensure_ascii=False)
            st.text(f"Normalized matching key: {matching_key}")
            st.dataframe(
                [
                    {
                        "Original key": json.dumps(original.original_key, ensure_ascii=False),
                        "Source records": ", ".join(
                            str(row.source_row) for row in original.records
                        ),
                    }
                    for original in collision.originals
                ],
                hide_index=True,
                width="stretch",
            )
            with st.expander("Inspect original source records"):
                for original in collision.originals:
                    st.text(
                        f"Original key: {json.dumps(original.original_key, ensure_ascii=False)}"
                    )
                    _show_evidence(original.records, source)


def _reference_rows(rows: tuple[SourceRecord, ...]) -> list[dict[str, str | int]]:
    """Compact original record references; full raw evidence remains below."""

    return [
        {
            "Source": f"File {row.source.value}",
            "Source row": row.source_row,
            "Amount": display_amount(row.amount),
            "Original key": json.dumps(row.key, ensure_ascii=False),
        }
        for row in rows
    ]


def _show_references(label: str, rows: tuple[SourceRecord, ...]) -> None:
    st.markdown(f"**{label}**")
    st.dataframe(_reference_rows(rows), hide_index=True, width="stretch")


def _show_correspondence(finding: ReconciliationFinding, policy: OneToManyPolicy) -> None:
    """Display the stored analysis without searching or reallocating parent records."""

    analysis = finding.correspondence_analysis
    st.subheader("Correspondence analysis")
    st.text(f"Status: {CORRESPONDENCE_STATUS_LABELS[analysis.status]}")
    if analysis.reason is not None:
        st.text(f"Reason: {CORRESPONDENCE_REASON_LABELS[analysis.reason]}")
    st.text(
        f"Search complete: {'Yes' if analysis.search_complete else 'No'}\n"
        f"Planned combinations: {analysis.planned_combinations:,}\n"
        f"Examined combinations: {analysis.examined_combinations:,}\n"
        f"Reserved combinations: {analysis.reserved_combinations:,}"
    )
    # A singleton witness has no intrinsic direction; the complete parent does.
    anchor = None
    if len(finding.rows_a) == 1 and len(finding.rows_b) > 1:
        anchor = finding.rows_a[0]
    elif len(finding.rows_b) == 1 and len(finding.rows_a) > 1:
        anchor = finding.rows_b[0]
    if anchor is not None:
        _show_references("Search anchor", (anchor,))

    if analysis.status is CorrespondenceStatus.UNIQUE_EXACT:
        st.markdown("**Inferred exact correspondence — reconciliation exception**")
        solution = analysis.accepted_solution
        selected = solution.rows_b if anchor.source is Source.A else solution.rows_a
        _show_references("Selected subset records", selected)
        residuals = analysis.unassigned_rows_a + analysis.unassigned_rows_b
        if residuals:
            st.subheader("Unassigned residual records")
            st.caption(
                "These records remain part of the parent finding and its complete financial delta."
            )
            st.dataframe(_reference_rows(residuals), hide_index=True, width="stretch")
    elif analysis.status is CorrespondenceStatus.AMBIGUOUS:
        st.caption(
            "At least two physical-row exact solutions were found; TallyDiff did not choose one."
        )
        if not analysis.search_complete:
            st.caption(
                "At least two exact solutions were found; enumeration stopped once ambiguity "
                "was proven."
            )
        for index, witness in enumerate(analysis.ambiguity_witnesses, start=1):
            _show_references(f"Candidate witness {index}", witness.rows_a + witness.rows_b)
    elif analysis.status is CorrespondenceStatus.SINGLETON_ONLY:
        st.caption(
            "A one-row candidate solution exists, but bounded one-to-many mode does not "
            "promote one-to-one matching inside a duplicate group."
        )
        witness = analysis.ambiguity_witnesses[0]
        _show_references("Singleton candidate witness", witness.rows_a + witness.rows_b)
    elif analysis.status is CorrespondenceStatus.NO_EXACT_SUBSET:
        st.caption(
            "The complete bounded search found no physical-row subset whose amount equals "
            "the anchor exactly."
        )
    elif analysis.reason is CorrespondenceReason.CANDIDATE_ROW_LIMIT:
        st.caption(
            f"This group exceeds the policy limit of {policy.max_candidate_rows:,} candidate rows. "
            "The search was not started."
        )
    elif analysis.reason is CorrespondenceReason.RUN_BUDGET_EXHAUSTED:
        st.caption(
            "The complete search for this group was not started because its full planned cost "
            "could not be reserved under the deterministic per-run budget."
        )
    elif analysis.reason is CorrespondenceReason.BOTH_SIDES_MULTIPLE:
        st.caption("v0.6 does not perform many-to-many allocation or partition search.")
    elif analysis.reason is CorrespondenceReason.MISSING_OPPOSITE_SIDE:
        st.caption(
            "This duplicate group lacks an opposite-side anchor and cannot undergo "
            "one-to-many inference."
        )
    if analysis.status is not CorrespondenceStatus.UNIQUE_EXACT:
        _show_references(
            "Unassigned parent records", analysis.unassigned_rows_a + analysis.unassigned_rows_b
        )


def _show_finding_table(
    findings: tuple[ReconciliationFinding, ...],
    *,
    table_key: str,
    one_to_many_policy: OneToManyPolicy | None = None,
    review_state: _ReviewRuntimeState | None = None,
) -> bool:
    st.caption("Select a row to inspect its original source records below.")
    event = st.dataframe(
        finding_rows(findings)
        if review_state is None
        else review_finding_rows(findings, review_state.reviews_by_key),
        hide_index=True,
        width="stretch",
        key=table_key,
        on_select="rerun",
        selection_mode="single-row",
        lazy=False,
        height=min(440, 35 * (len(findings) + 1) + 3),
    )
    if event.selection.rows:
        selected_index = event.selection.rows[0]
        if type(selected_index) is not int or not 0 <= selected_index < len(findings):
            return False
        finding = findings[selected_index]
        st.text(f"Selected key: {' / '.join(finding.key)}")
        if review_state is not None:
            st.session_state[_REVIEW_EDITOR_SELECTION_KEY] = (
                review_state.binding.result_fingerprint,
                finding.key,
                review_state.editor_generation,
                table_key,
                selected_index,
            )
            _show_analyst_review(finding, review_state)
        if finding.correspondence_analysis is not None:
            _show_correspondence(finding, one_to_many_policy)
        if finding.field_comparisons:
            st.subheader("Secondary comparison details")
            st.dataframe(
                [
                    {
                        "Comparison": index,
                        "File A field": comparison.mapping.file_a,
                        "File B field": comparison.mapping.file_b,
                        "File A distinct values": json.dumps(
                            comparison.values_a, ensure_ascii=False
                        ),
                        "File B distinct values": json.dumps(
                            comparison.values_b, ensure_ascii=False
                        ),
                        "Status": comparison.status.value,
                    }
                    for index, comparison in enumerate(finding.field_comparisons, start=1)
                ],
                hide_index=True,
                width="stretch",
            )
            if finding.correspondence_analysis is not None:
                st.caption(
                    "Secondary fields are retained as evidence but were not used to select "
                    "or disambiguate this correspondence."
                )
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
        return review_state is not None
    return False


def _show_exception_review(
    findings: tuple[ReconciliationFinding, ...],
    *,
    identity: str,
    one_to_many_policy: OneToManyPolicy | None = None,
    review_state: _ReviewRuntimeState | None = None,
) -> bool:
    query = st.text_input(
        "Search matching keys",
        key=f"review_search_{identity}",
        help="Case-insensitive substring search within each matching-key component.",
    )
    category_options = EXCEPTION_CATEGORIES + tuple(
        category
        for category in (FindingCategory.EXACT_MATCH, FindingCategory.WITHIN_TOLERANCE)
        if any(finding.category is category for finding in findings)
    )
    categories = st.multiselect(
        "Exception categories",
        category_options,
        default=category_options,
        format_func=CATEGORY_LABELS.__getitem__,
        key=f"review_categories_{identity}",
    )
    dispositions = (
        st.multiselect(
            "Review disposition",
            tuple(ReviewDisposition),
            default=tuple(ReviewDisposition),
            format_func=REVIEW_DISPOSITION_LABELS.__getitem__,
            key=f"review_dispositions_{identity}",
        )
        if review_state is not None
        else None
    )
    has_comparisons = any(finding.field_comparisons for finding in findings)
    secondary_only = (
        st.checkbox("Has secondary field mismatch", value=False, key=f"review_secondary_{identity}")
        if has_comparisons
        else False
    )
    left, right = st.columns(2)
    with left:
        minimum_text = st.text_input(
            "Minimum absolute delta",
            value="0",
            key=f"review_minimum_{identity}",
            help="Display groups at or above this absolute difference. "
            "Zero includes zero-delta exceptions. This does not change amount tolerance.",
        )
    with right:
        sort_order = st.selectbox(
            "Exception sort order",
            tuple(EXCEPTION_SORT_LABELS),
            format_func=EXCEPTION_SORT_LABELS.__getitem__,
            key=f"review_sort_{identity}",
        )
    try:
        minimum = parse_amount(minimum_text.strip() or "0")
        if minimum < 0:
            raise AmountParseError("must be zero or greater")
    except AmountParseError as exc:
        st.error(f"Minimum absolute delta: {exc}.")
        st.caption(f"Showing 0 of {len(findings):,} exception groups.")
        st.info("Enter a valid minimum absolute delta to display exception groups.")
        return False
    visible = review_exceptions(
        findings,
        query=query,
        categories=categories,
        minimum_abs_delta=minimum,
        sort_order=sort_order,
        secondary_mismatches_only=secondary_only,
    )
    if review_state is not None:
        visible = filter_review_dispositions(visible, review_state.reviews_by_key, dispositions)
    st.caption(f"Showing {len(visible):,} of {len(findings):,} exception groups.")
    if not visible:
        st.info("No exception groups match the current review filters.")
        return False
    # A different view must not reuse a selection index from another row order.
    view_state = [query, [category.value for category in categories], minimum_text, sort_order]
    if has_comparisons:
        view_state.append(secondary_only)
    if review_state is not None:
        view_state.extend(
            (
                review_state.binding.result_fingerprint,
                review_state.editor_generation,
                [disposition.value for disposition in dispositions],
            )
        )
    # Annotation changes can alter membership without changing filter widgets.
    # Hash exact ordered canonical keys, never notes or displayed key strings.
    view_state.append([finding.key for finding in visible])
    view_id = sha256(json.dumps(view_state).encode("utf-8")).hexdigest()
    return _show_finding_table(
        visible,
        table_key=f"exceptions_{identity}_{view_id}",
        one_to_many_policy=one_to_many_policy,
        review_state=review_state,
    )


def _show_results(
    result: ReconciliationResult,
    mapping: ColumnMapping,
    identity: str,
    name_a: str,
    name_b: str,
    review_state: _ReviewRuntimeState | None = None,
) -> None:
    session = None
    if review_state is not None:
        if review_state.binding.result is not result:
            raise ReviewSessionError("review runtime must own the displayed result")
        session = _ReviewRuntimeState.snapshot(review_state)
    st.header("4. Review results")
    st.caption("Configuration used for this reconciliation")
    st.text(f"File A: {name_a}\nFile B: {name_b}")
    st.text(
        "Key normalization: Exact — no transformations"
        if result.key_normalization is None
        else "Key normalization: Enabled by key component"
    )
    for index, (left, right) in enumerate(mapping.key_pairs, start=1):
        st.text(f"Key {index}: {left}  \u2194  {right}")
        if result.key_normalization is not None:
            rules = result.key_normalization.component_rules[index - 1]
            labels = [label for name, label in NORMALIZATION_LABELS.items() if getattr(rules, name)]
            st.text(
                f"Normalization: {', '.join(labels) if labels else 'Exact — no transformations'}"
            )
    if result.key_normalization is not None:
        st.caption(
            "Displayed matching keys may be normalized. "
            "Source evidence retains original source values. "
            "Rules apply equally to both files."
        )
    st.text(f"Amount: {mapping.amount_a}  \u2194  {mapping.amount_b}")
    st.text(f"Amount tolerance: {display_amount(result.amount_tolerance)} (absolute difference)")
    st.text(f"Reconciliation mode: {MODE_LABELS[result.mode]}")
    if result.mode is ReconciliationMode.GROUPED_BY_KEY:
        st.caption(
            "Grouped results compare totals for each matching key; "
            "they do not claim that individual rows correspond."
        )
    elif result.mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        policy = result.one_to_many_policy
        st.text(
            f"Inferred correspondence policy: {policy.policy_id}\n"
            f"Maximum candidate rows: {policy.max_candidate_rows:,}\n"
            f"Maximum planned combinations per run: {policy.max_planned_combinations_per_run:,}\n"
            f"Exact amounts only: {'Yes' if policy.exact_amounts_only else 'No'}\n"
            f"Physical-row uniqueness: {'Yes' if policy.physical_row_uniqueness else 'No'}\n"
            "Singleton alternatives count as rivals: "
            f"{'Yes' if policy.singleton_rivals_count else 'No'}"
        )
        st.caption(
            "Correspondence is inferred under this fixed policy. An accepted subset proves "
            "amount consistency under this policy. It does not establish which source is "
            "authoritative or prove business transaction identity."
        )

    if result.comparison_fields:
        st.text("Secondary comparisons: exact original evidence")
        for index, comparison in enumerate(result.comparison_fields, start=1):
            st.text(f"Comparison {index}: {comparison.file_a} ↔ {comparison.file_b}")
        st.caption("Secondary differences require review but do not alter financial deltas.")
        st.caption(
            "Each secondary field compares independent sets of distinct values on each side. "
            "This does not compare row correspondence, occurrence counts, cross-field "
            "combinations, or amount allocation."
            if result.mode is ReconciliationMode.GROUPED_BY_KEY
            else "Secondary equality is evaluated only for deterministic one-row-per-side matches."
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
    if result.comparison_fields:
        st.metric("Secondary field differences", str(len(result.secondary_mismatch_findings)))
        st.caption(
            "Primary category counts describe amount/presence. "
            "An Exact match or Within tolerance group may still require secondary review."
        )
    if result.mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        st.subheader("Correspondence summary")
        analyzed = result.correspondence_analysis_findings
        left, right = st.columns(2)
        left.metric("Analyzed duplicate groups", str(len(analyzed)))
        right.metric("Inferred correspondences", str(len(result.inferred_correspondence_findings)))
        statuses = Counter(finding.correspondence_status for finding in analyzed)
        st.caption(
            "Unresolved analyzed groups: "
            + "; ".join(
                f"{label}: {statuses[status]:,}"
                for status, label in CORRESPONDENCE_STATUS_LABELS.items()
                if status is not CorrespondenceStatus.UNIQUE_EXACT
            )
        )

    exceptions = result.exceptions
    tolerated = result.tolerated_findings
    if not result.findings:
        st.info("Both files contain headers only; there are no data records to reconcile.")
        return
    if not exceptions:
        if result.comparison_fields:
            st.success(
                "Reconciled within configured amount tolerance. "
                "Configured secondary fields passed where comparable. No exceptions to review."
                if tolerated
                else "All matching key totals agree exactly. "
                "Configured secondary fields passed where comparable. No exceptions to review."
            )
        elif tolerated:
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
        prepare_control = None
        if session is not None:
            _show_review_progress(session)
            prepare_control = _show_review_persistence(review_state)
        st.subheader("Exceptions")
        st.caption(
            "Review filters affect the displayed table only. The exception export remains complete."
        )
        st.download_button(
            "Download exception report",
            data=export_exceptions_csv(result),
            file_name="tallydiff_exceptions.csv",
            mime="text/csv; charset=utf-8",
            key=f"download_{identity}",
            on_click="ignore",
        )
        has_note_editor = _show_exception_review(
            exceptions,
            identity=identity,
            one_to_many_policy=result.one_to_many_policy,
            review_state=review_state,
        )
        if prepare_control is not None and not has_note_editor:
            with prepare_control:
                st.button(
                    "Prepare review downloads",
                    key=_REVIEW_PREPARE_KEY,
                    on_click=_request_review_downloads,
                )
    if tolerated:
        st.subheader("Within tolerance")
        st.metric(
            "Net accepted variance (A - B)",
            display_amount(result.tolerated_delta_total, signed=True),
        )
        st.caption(
            "Accepted amount differences did not produce a configured secondary mismatch. "
            "They are included in control totals and excluded from the exception report. "
            "Opposing accepted deltas can cancel in this net amount."
            if result.comparison_fields
            else "Accepted nonzero differences are included in control totals and excluded "
            "from the exception report. Opposing accepted deltas can cancel in this net amount."
        )
        _show_finding_table(tolerated, table_key=f"tolerated_{identity}")
    if result.mode is ReconciliationMode.GROUPED_BY_KEY:
        exact = tuple(
            finding
            for finding in result.findings
            if finding.category is FindingCategory.EXACT_MATCH
            and (len(finding.rows_a) > 1 or len(finding.rows_b) > 1)
            and not finding.is_exception
        )
        if exact:
            st.subheader("Grouped exact key totals")
            _show_finding_table(exact, table_key=f"exact_{identity}")


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
    st.session_state.setdefault("comparison_count", 0)
    left, right = st.columns(2)
    with left:
        file_a = _choose_file(Source.A)
    with right:
        file_b = _choose_file(Source.B)
    if file_a is None or file_b is None:
        _clear_result()
        st.info("Upload both files and select any required worksheets to map their columns.")
        return
    columns_a, columns_b = file_a.columns, file_b.columns
    st.header("2. Map columns")
    _profile_controls(columns_a, columns_b)
    mapping = _map_columns(columns_a, columns_b)
    key_normalization = _normalization_config()
    st.header("3. Run reconciliation")
    st.session_state.setdefault("reconciliation_mode", ReconciliationMode.UNIQUE)
    mode = st.radio(
        "Reconciliation mode",
        (
            ReconciliationMode.UNIQUE,
            ReconciliationMode.GROUPED_BY_KEY,
            ReconciliationMode.BOUNDED_ONE_TO_MANY,
        ),
        format_func=MODE_LABELS.__getitem__,
        key="reconciliation_mode",
        horizontal=True,
        on_change=_clear_result,
    )
    if mode is ReconciliationMode.UNIQUE:
        st.caption("Duplicate matching keys remain ambiguous and require review.")
    elif mode is ReconciliationMode.GROUPED_BY_KEY:
        st.caption(
            "Rows sharing the same matching key are totaled on each side "
            "and the key totals are compared. "
            "This does not claim that individual rows correspond."
        )
    else:
        st.caption(
            "TallyDiff may explain one source row using one uniquely determined exact subset "
            "of rows on the opposite side, within fixed search limits. "
            "Inferred correspondence is always treated as a reconciliation exception."
        )
        st.caption(
            "Ordinary deterministic one-row-per-side groups retain normal secondary comparison. "
            "Duplicate-shaped groups undergoing correspondence analysis report secondary fields "
            "as NOT_COMPARABLE. Secondary fields do not select, rank, or disambiguate "
            "financial subsets."
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
    if mode is ReconciliationMode.BOUNDED_ONE_TO_MANY:
        st.caption(
            "Amount tolerance applies only to ordinary one-row-per-side comparisons. "
            "Subset inference requires exact Decimal equality. Residual-bearing inferred groups "
            "remain Amount mismatch even when their residual delta is within tolerance."
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
            key_normalization=key_normalization,
        )
        if amount_tolerance is not None
        else None
    )
    completed = st.session_state.get("completed")
    problem = mapping.problem(columns_a, columns_b)
    if problem or identity is None or completed is None or completed[0] != identity:
        _clear_result()
    if problem:
        st.info(problem)
    elif amount_tolerance is not None:
        st.download_button(
            "Download mapping profile",
            data=export_mapping_profile(
                mapping,
                amount_tolerance=amount_tolerance,
                reconciliation_mode=mode,
                key_normalization=key_normalization,
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
        # Preserve only a local reference while removing active ownership. A new
        # successful binding must verify equivalence before these reviews return.
        previous_review_state = st.session_state.get(_REVIEW_STATE_KEY)
        _clear_result()
        records = []
        for source, input_file, keys, amount, comparisons in (
            (Source.A, file_a, mapping.keys_a, mapping.amount_a, mapping.comparisons_a),
            (Source.B, file_b, mapping.keys_b, mapping.amount_b, mapping.comparisons_b),
        ):
            try:
                if input_file.input_format == "csv":
                    parsed = ingest_csv(
                        input_file.text,
                        source=source,
                        key_columns=keys,
                        amount_column=amount,
                        comparison_columns=comparisons,
                    )
                else:
                    parsed = ingest_xlsx(
                        input_file.data,
                        source=source,
                        worksheet=input_file.worksheet,
                        key_columns=keys,
                        amount_column=amount,
                        comparison_columns=comparisons,
                    )
                records.append(parsed)
            except IngestionError as exc:
                st.error(str(exc))
        if len(records) == 2:
            try:
                result = reconcile(
                    records[0],
                    records[1],
                    amount_tolerance=amount_tolerance,
                    mode=mode,
                    key_normalization=key_normalization,
                    comparison_fields=mapping.comparison_fields,
                )
            except NormalizationCollisionError as exc:
                _show_normalization_collisions(exc)
            except KeyNormalizationError as exc:
                st.error(f"Key normalization blocked: {exc}")
            except ReconciliationIntegrityError as exc:
                st.error(f"Reconciliation integrity check failed: {exc}")
            else:
                try:
                    binding = bind_review_result(
                        result,
                        data_a=file_a.data,
                        data_b=file_b.data,
                        mapping=mapping,
                        format_a=file_a.input_format,
                        format_b=file_b.input_format,
                        worksheet_a=file_a.worksheet,
                        worksheet_b=file_b.worksheet,
                    )
                except ReviewSessionError as exc:
                    st.error(f"Review binding integrity check failed: {exc}")
                else:
                    runtime = _adopt_review_binding(binding, previous_review_state)
                    st.session_state[_REVIEW_BINDING_KEY] = binding
                    st.session_state[_REVIEW_STATE_KEY] = runtime
                    st.session_state.completed = (identity, result)
    _prepare_requested_review_downloads()
    completed = st.session_state.get("completed")
    if completed is not None and completed[0] == identity:
        persistence_error = st.session_state.get(_REVIEW_IMPORT_ERROR_KEY) or st.session_state.get(
            _REVIEW_PREPARE_ERROR_KEY
        )
        if persistence_error is not None:
            try:
                _active_review_runtime()
            except ReviewSessionError:
                # Do not let ordinary Stage-5 recovery replace corrupt annotations
                # after an explicit persistence failure. Keep the error visible until
                # a valid explicit Run or repair; financial evidence stays available.
                st.error(persistence_error)
                _show_results(completed[1], mapping, identity, file_a.label, file_b.label)
                return
        if not _ensure_review_state(completed[1]):
            return
        _show_results(
            completed[1],
            mapping,
            identity,
            file_a.label,
            file_b.label,
            review_state=st.session_state[_REVIEW_STATE_KEY],
        )


if __name__ == "__main__":
    main()
