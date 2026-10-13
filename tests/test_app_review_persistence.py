"""Explicit imports replace complete annotations; prepared downloads capture committed state."""

import csv
import json
from dataclasses import FrozenInstanceError, fields, replace
from io import StringIO

import pytest
import streamlit as st
from test_app import (
    _exception_downloads,
    _exception_table,
    _review_app,
    _simple_mapping,
    _uploaded_app,
)
from test_app_review import (
    INVALID_NOTES,
    _editor_key,
    _fake_active,
    _invoke,
    _progress,
    _save_note,
    _select,
    _widget,
)
from test_app_review_state import (
    CORRUPTIONS,
    DATA_A,
    DATA_B,
    _assert_active,
    _binding,
    _corrupt_runtime,
    _run_app,
)

import tallydiff
import tallydiff.app as ui
import tallydiff.engine as engine
import tallydiff.ingest as ingest
import tallydiff.normalization as normalization
import tallydiff.subset_matching as subset_matching
from tallydiff import (
    FindingReview,
    ReconciliationMode,
    ReviewDisposition,
    ReviewSession,
    ReviewSessionError,
    export_exceptions_csv,
    export_review_session,
    export_reviewed_exceptions_csv,
)
from tallydiff.presentation import REVIEW_DISPOSITION_LABELS

REVIEWED_LABEL = "Download reviewed exception report"
SESSION_LABEL = "Download review session"
PREPARE_LABEL = "Prepare review downloads"
SAVE_LABEL = "Save note"
APPLY_LABEL = "Apply review session"
UPLOAD_LABEL = "Review session JSON"


@pytest.fixture
def downloads(monkeypatch):
    captured = {}
    original = st.download_button

    def capture(label, *args, **kwargs):
        captured[label] = dict(kwargs)
        return original(label, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture)
    return captured


def _button(app, label):
    return next(button for button in app.button if button.label == label)


def _upload(app, data, *, name="saved_review.json"):
    uploader = next(widget for widget in app.file_uploader if widget.label == UPLOAD_LABEL)
    uploader.set_value((name, data, "application/json") if data is not None else None).run()
    assert not app.exception
    return uploader.key


def _apply(app, data):
    _upload(app, data)
    _button(app, APPLY_LABEL).click().run()
    assert not app.exception


def _prepare(app):
    _button(app, PREPARE_LABEL).click().run()
    assert not app.exception
    return app.session_state[ui._REVIEW_PREPARED_KEY]


def _assert_downloads(app, *, prepared):
    labels = [button.label for button in app.download_button]
    assert labels.count("Download exception report") == 1
    assert labels.count(REVIEWED_LABEL) == int(prepared)
    assert labels.count(SESSION_LABEL) == int(prepared)


def _mixed_session(runtime):
    statuses = tuple(ReviewDisposition)
    return ReviewSession(
        runtime.binding,
        tuple(
            FindingReview(
                key,
                statuses[(index + 1) % len(statuses)],
                f'Imported {index}: =1+2, "quoted"\t🧾\r\nexact ',
            )
            for index, key in enumerate(runtime.binding.exception_keys)
        ),
    )


def _rows(data):
    return list(csv.reader(StringIO(data.decode("utf-8"), newline="")))


def _committed(runtime):
    return (
        export_review_session(runtime.snapshot()),
        tuple(runtime.reviews_by_key.items()),
        runtime.annotation_revision,
        runtime.editor_generation,
        runtime.binding,
        runtime.binding.result,
    )


def _assert_committed(runtime, before):
    data, reviews, revision, generation, binding, result = before
    assert export_review_session(runtime.snapshot()) == data
    assert tuple(runtime.reviews_by_key.items()) == reviews
    assert all(runtime.reviews_by_key[key] is review for key, review in reviews)
    assert (runtime.annotation_revision, runtime.editor_generation) == (revision, generation)
    assert runtime.binding is binding and runtime.binding.result is result


def _filters(app):
    return {
        label: _widget(app, label).value
        for label in (
            "Search matching keys",
            "Exception categories",
            "Review disposition",
            "Minimum absolute delta",
            "Exception sort order",
        )
    }


def _prepared_in_state(state, runtime):
    prepared = ui._prepare_review_downloads(runtime)
    state[ui._REVIEW_PREPARED_KEY] = prepared
    return prepared


def test_prepared_holder_is_private_immutable_slotted_and_captures_complete_snapshot(monkeypatch):
    runtime, state = _fake_active(monkeypatch)
    first = runtime.binding.exception_keys[0]
    runtime.reviews_by_key[first] = FindingReview(
        first, ReviewDisposition.REVIEW_COMPLETE, "  =formula\t🧾\r\n "
    )
    runtime.annotation_revision = 7
    runtime.editor_generation = 3
    before = _committed(runtime)
    prepared = ui._prepare_review_downloads(runtime)
    assert [field.name for field in fields(prepared)] == [
        "result_fingerprint",
        "annotation_revision",
        "session_json",
        "reviewed_csv",
    ]
    assert prepared.result_fingerprint == runtime.binding.result_fingerprint
    assert prepared.annotation_revision == 7
    assert prepared.session_json == export_review_session(runtime.snapshot())
    assert prepared.reviewed_csv == export_reviewed_exceptions_csv(
        runtime.binding, runtime.snapshot()
    )
    assert not hasattr(prepared, "__dict__")
    with pytest.raises(FrozenInstanceError):
        prepared.annotation_revision = 9
    assert ui._REVIEW_PREPARED_KEY not in state
    _assert_committed(runtime, before)
    assert not hasattr(tallydiff, "_PreparedReviewDownloads")


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("empty", (False, True), ids=("exceptions", "empty"))
def test_preparation_helper_covers_all_modes_and_empty_domain_sessions(mode, empty):
    binding = (
        _binding(b"id,amount\nCLEAN,1\n", b"id,amount\nCLEAN,1\n", mode=mode)
        if empty
        else _binding(mode=mode)
    )
    runtime = ui._adopt_review_binding(binding)
    before = _committed(runtime)
    prepared = ui._prepare_review_downloads(runtime)
    assert prepared.session_json == export_review_session(runtime.snapshot())
    assert prepared.reviewed_csv == export_reviewed_exceptions_csv(binding, runtime.snapshot())
    assert len(_rows(prepared.reviewed_csv)) == len(binding.exception_keys) + 1
    _assert_committed(runtime, before)


@pytest.mark.parametrize("corruption", CORRUPTIONS)
def test_preparation_helper_rejects_corrupt_runtime_without_repair(monkeypatch, corruption):
    runtime, state = _fake_active(monkeypatch)
    _corrupt_runtime(runtime, corruption)
    original = runtime.reviews_by_key
    revision, generation = runtime.annotation_revision, runtime.editor_generation
    with pytest.raises(ReviewSessionError):
        ui._prepare_review_downloads(runtime)
    assert runtime.reviews_by_key is original
    assert (runtime.annotation_revision, runtime.editor_generation) == (revision, generation)
    assert ui._REVIEW_PREPARED_KEY not in state


@pytest.mark.parametrize("mismatch", ("fingerprint", "revision", "both"))
def test_prepared_metadata_is_the_staleness_boundary(monkeypatch, mismatch):
    runtime, state = _fake_active(monkeypatch)
    prepared = _prepared_in_state(state, runtime)
    assert ui._current_prepared_review_downloads(runtime) is prepared
    stale = replace(
        prepared,
        result_fingerprint="f" * 64 if mismatch != "revision" else prepared.result_fingerprint,
        annotation_revision=1 if mismatch != "fingerprint" else prepared.annotation_revision,
    )
    state[ui._REVIEW_PREPARED_KEY] = stale
    before = _committed(runtime)
    assert ui._current_prepared_review_downloads(runtime) is None
    assert ui._REVIEW_PREPARED_KEY not in state
    _assert_committed(runtime, before)


def test_narrow_editor_cleanup_preserves_runtime_binding_filters_candidate_and_other_tables(
    monkeypatch,
):
    runtime, state = _fake_active(monkeypatch)
    kept = {
        "review_search_identity": "A",
        "review_categories_identity": ["amount_mismatch"],
        "review_dispositions_identity": [ReviewDisposition.NEEDS_FOLLOW_UP],
        "review_minimum_identity": "1",
        "review_sort_identity": "matching_key",
        ui._REVIEW_UPLOAD_KEY: object(),
        "tolerated_identity": {"selection": {"rows": [0]}},
        "exact_identity": {"selection": {"rows": [0]}},
    }
    state.update(kept)
    old_keys = (_editor_key(runtime, "note"), _editor_key(runtime, "disposition"))
    state.update(dict.fromkeys(old_keys, "old widget"))
    state[ui._REVIEW_NOTE_DRAFTS_KEY] = {old_keys[0]: "bad\0draft"}
    state[ui._REVIEW_NOTE_ERRORS_KEY] = {old_keys[0]: "error"}
    state[ui._REVIEW_EDITOR_SELECTION_KEY] = ("old",)
    state["exceptions_identity"] = {"selection": {"rows": [1]}}
    ui._clear_review_editor_state()
    assert state[ui._REVIEW_STATE_KEY] is runtime
    assert state[ui._REVIEW_BINDING_KEY] is runtime.binding
    assert state["completed"][1] is runtime.binding.result
    for key, value in kept.items():
        assert state[key] is value
    assert all(key not in state for key in old_keys)
    assert ui._REVIEW_NOTE_DRAFTS_KEY not in state
    assert ui._REVIEW_NOTE_ERRORS_KEY not in state
    assert ui._REVIEW_EDITOR_SELECTION_KEY not in state
    assert "exceptions_identity" not in state


@pytest.mark.parametrize("equal", (False, True), ids=("replacement", "identical"))
def test_apply_helper_replaces_every_object_with_exact_order_and_advances_both_counters(
    monkeypatch, equal
):
    runtime, state = _fake_active(monkeypatch)
    first = runtime.binding.exception_keys[0]
    runtime.reviews_by_key[first] = FindingReview(first, ReviewDisposition.REVIEW_COMPLETE, "old")
    runtime.annotation_revision = 8
    runtime.editor_generation = 4
    prior_objects = tuple(runtime.reviews_by_key.values())
    session = runtime.snapshot() if equal else ReviewSession.unreviewed(runtime.binding)
    old_binding, old_result = runtime.binding, runtime.binding.result
    prepared = _prepared_in_state(state, runtime)
    assert prepared is state[ui._REVIEW_PREPARED_KEY]
    ui._apply_review_session(runtime, export_review_session(session))
    assert tuple(runtime.reviews_by_key) == old_binding.exception_keys
    assert tuple(runtime.reviews_by_key.values()) == session.reviews
    assert all(
        new is not old for new, old in zip(runtime.snapshot().reviews, prior_objects, strict=True)
    )
    assert runtime.binding is old_binding and runtime.binding.result is old_result
    assert runtime.annotation_revision == 9 and runtime.editor_generation == 5
    assert ui._REVIEW_PREPARED_KEY not in state


def test_apply_rejects_loader_returning_a_different_equivalent_binding_atomically(monkeypatch):
    runtime, state = _fake_active(monkeypatch)
    prepared = _prepared_in_state(state, runtime)
    before = _committed(runtime)
    equivalent = _binding()
    assert equivalent.result_fingerprint == runtime.binding.result_fingerprint
    assert equivalent is not runtime.binding
    monkeypatch.setattr(
        ui, "load_review_session", lambda *a, **k: ReviewSession.unreviewed(equivalent)
    )
    with pytest.raises(ReviewSessionError):
        ui._apply_review_session(runtime, b"candidate")
    _assert_committed(runtime, before)
    assert state[ui._REVIEW_PREPARED_KEY] is prepared


@pytest.mark.parametrize("field", ("note", "disposition"))
def test_old_generation_callbacks_cannot_change_imported_authoritative_state(monkeypatch, field):
    runtime, state = _fake_active(monkeypatch)
    key = runtime.binding.exception_keys[0]
    fingerprint, generation = runtime.binding.result_fingerprint, runtime.editor_generation
    widget_key = _editor_key(runtime, field)
    imported = _mixed_session(runtime)
    ui._apply_review_session(runtime, export_review_session(imported))
    state[widget_key] = "stale overwrite" if field == "note" else ReviewDisposition.UNREVIEWED
    before = _committed(runtime)
    callback = ui._commit_review_note if field == "note" else ui._commit_review_disposition
    _invoke(callback, runtime, key, fingerprint=fingerprint, generation=generation)
    _assert_committed(runtime, before)


@pytest.mark.parametrize("field", ("note", "disposition"))
@pytest.mark.parametrize("changed", (False, True), ids=("noop", "commit"))
def test_field_callbacks_invalidate_prepared_state_only_on_actual_commit(
    monkeypatch, field, changed
):
    runtime, state = _fake_active(monkeypatch)
    prepared = _prepared_in_state(state, runtime)
    state[_editor_key(runtime, field)] = (
        ("new note" if changed else "")
        if field == "note"
        else (ReviewDisposition.REVIEW_COMPLETE if changed else ReviewDisposition.UNREVIEWED)
    )
    callback = ui._commit_review_note if field == "note" else ui._commit_review_disposition
    _invoke(callback, runtime)
    assert runtime.annotation_revision == int(changed) and runtime.editor_generation == 0
    if changed:
        assert ui._REVIEW_PREPARED_KEY not in state
    else:
        assert ui._current_prepared_review_downloads(runtime) is prepared


def test_persistence_controls_are_explicit_and_downloads_start_absent():
    app = _run_app()
    runtime = _assert_active(app)
    uploader = next(widget for widget in app.file_uploader if widget.label == UPLOAD_LABEL)
    assert uploader.value is None and uploader.key == ui._REVIEW_UPLOAD_KEY
    assert uploader.key != "profile_upload"
    assert uploader.key.startswith("review_")
    assert _button(app, APPLY_LABEL).disabled
    assert not _button(app, PREPARE_LABEL).disabled
    _assert_downloads(app, prepared=False)
    assert runtime.annotation_revision == runtime.editor_generation == 0
    assert any(
        "Review changes are kept in this session." in caption.value
        and "Prepare review downloads" in caption.value
        for caption in app.caption
    )
    assert not any("XLSX" in button.label for button in app.download_button)


@pytest.mark.parametrize("replacement", ("valid", "malformed", "remove"))
def test_upload_candidate_changes_nothing_and_never_calls_parser(monkeypatch, replacement):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "committed note")
    prepared = _prepare(app)
    before = _committed(runtime)
    selected = _exception_table(app).key
    filters = _filters(app)
    progress = [(metric.label, metric.value) for metric in app.metric]

    def unexpected(*args, **kwargs):
        raise AssertionError("candidate upload must not parse a review session")

    monkeypatch.setattr(tallydiff, "load_review_session", unexpected)
    _upload(app, export_review_session(_mixed_session(runtime)))
    assert not _button(app, APPLY_LABEL).disabled
    if replacement == "valid":
        _upload(app, export_review_session(runtime.snapshot()), name="replacement.json")
    elif replacement == "malformed":
        _upload(app, b"this is not JSON", name="bad.json")
    else:
        _upload(app, None)
        assert _button(app, APPLY_LABEL).disabled
    _assert_committed(runtime, before)
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    assert _exception_table(app).key == selected
    assert _widget(app, "Analyst note").value == "committed note"
    assert _filters(app) == filters
    assert [(metric.label, metric.value) for metric in app.metric] == progress
    _assert_downloads(app, prepared=True)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_valid_apply_replaces_complete_population_progress_and_editor_ownership(mode):
    app = _review_app(mode, tolerance="0.001")
    runtime = _assert_active(app)
    result, binding = runtime.binding.result, runtime.binding
    _select(app)
    _save_note(app, "old committed")
    _prepare(app)
    _save_note(app, "old invalid\0draft")
    old_note_key = _widget(app, "Analyst note").key
    old_disposition_key = _widget(app, "Analyst disposition").key
    old_table_key = _exception_table(app).key
    filters = _filters(app)
    revision, generation = runtime.annotation_revision, runtime.editor_generation
    imported = _mixed_session(runtime)
    _apply(app, export_review_session(imported))
    assert runtime.binding is binding and runtime.binding.result is result
    assert app.session_state["completed"][1] is result
    assert tuple(runtime.reviews_by_key) == binding.exception_keys
    assert tuple(runtime.reviews_by_key.values()) == imported.reviews
    assert runtime.annotation_revision == revision + 1
    assert runtime.editor_generation == generation + 1
    assert _filters(app) == filters
    assert any(item.value == "Review session applied." for item in app.success)
    assert not app.text_area
    assert not any(item.value.startswith("Selected key:") for item in app.text)
    assert old_note_key not in app.session_state and old_disposition_key not in app.session_state
    assert ui._REVIEW_NOTE_DRAFTS_KEY not in app.session_state
    assert ui._REVIEW_NOTE_ERRORS_KEY not in app.session_state
    assert ui._REVIEW_EDITOR_SELECTION_KEY not in app.session_state
    # The table may retain its view key when labels happen to match, but selection cannot survive.
    if old_table_key in app.session_state:
        assert not app.session_state[old_table_key]["selection"]["rows"]
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    _assert_downloads(app, prepared=False)
    _progress(
        app,
        unreviewed=imported.unreviewed_count,
        follow_up=imported.needs_follow_up_count,
        complete=imported.complete_count,
    )
    assert _exception_table(app).value["Review disposition"].tolist() == [
        REVIEW_DISPOSITION_LABELS[review.disposition] for review in imported.reviews
    ]
    _select(app)
    assert _widget(app, "Analyst note").value == imported.reviews[0].note
    assert _widget(app, "Analyst disposition").value is imported.reviews[0].disposition
    assert _widget(app, "Analyst note").key != old_note_key
    _save_note(app, "post-import note")
    assert runtime.reviews_by_key[imported.reviews[0].matching_key] == FindingReview(
        imported.reviews[0].matching_key, imported.reviews[0].disposition, "post-import note"
    )
    assert runtime.annotation_revision == revision + 2
    assert runtime.editor_generation == generation + 1
    _prepare(app)
    assert app.session_state[ui._REVIEW_PREPARED_KEY].session_json == export_review_session(
        runtime.snapshot()
    )


def test_applying_identical_session_still_advances_revision_generation_and_resets_selection():
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    before = export_review_session(runtime.snapshot())
    _prepare(app)
    _apply(app, before)
    assert export_review_session(runtime.snapshot()) == before
    assert runtime.annotation_revision == runtime.editor_generation == 1
    assert not app.text_area and not any(
        item.value.startswith("Selected key:") for item in app.text
    )
    _assert_downloads(app, prepared=False)


INVALID_IMPORTS = (
    "malformed",
    "fingerprint",
    "missing-review",
    "invalid-note",
    "duplicate-key",
    "version",
    "foreign-binding",
)


def _invalid_import(runtime, variant):
    if variant == "malformed":
        return b'{"format":'
    if variant == "foreign-binding":
        foreign = ui._adopt_review_binding(_binding(DATA_A.replace(b"A,one,3,3", b"A,one,4,4")))
        return export_review_session(foreign.snapshot())
    data = json.loads(export_review_session(_mixed_session(runtime)))
    if variant == "fingerprint":
        data["result_fingerprint"] = "f" * 64
    elif variant == "missing-review":
        data["reviews"].pop()
    elif variant == "invalid-note":
        data["reviews"][-1]["note"] = "invalid\0private note"
    elif variant == "duplicate-key":
        data["reviews"][-1]["matching_key"] = data["reviews"][0]["matching_key"]
    elif variant == "version":
        data["version"] = 99
    else:
        raise AssertionError(variant)
    return json.dumps(data).encode("utf-8")


@pytest.mark.parametrize("variant", INVALID_IMPORTS)
def test_invalid_apply_is_atomic_preserves_filters_editor_progress_and_prepared_bytes(variant):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.NEEDS_FOLLOW_UP).run()
    _save_note(app, "private authoritative note")
    _widget(app, "Exception sort order").set_value("absolute_delta_desc").run()
    _select(app)
    prepared = _prepare(app)
    before = _committed(runtime)
    filters = _filters(app)
    progress = [(metric.label, metric.value) for metric in app.metric]
    editor = (_widget(app, "Analyst disposition").value, _widget(app, "Analyst note").value)
    _apply(app, _invalid_import(runtime, variant))
    _assert_committed(runtime, before)
    assert _filters(app) == filters
    assert [(metric.label, metric.value) for metric in app.metric] == progress
    assert (_widget(app, "Analyst disposition").value, _widget(app, "Analyst note").value) == editor
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    _assert_downloads(app, prepared=True)
    assert any(error.value.startswith("Review session: ") for error in app.error)
    assert all(len(error.value) < 350 and "private" not in error.value for error in app.error)


def test_late_invalid_entry_never_partially_installs_valid_prefix(monkeypatch):
    data_a = ("id,amount\n" + "".join(f"K{index:02},3\n" for index in range(24))).encode()
    data_b = data_a.replace(b",3\n", b",1\n")
    runtime = ui._adopt_review_binding(_binding(data_a, data_b))
    state = {ui._REVIEW_STATE_KEY: runtime, ui._REVIEW_BINDING_KEY: runtime.binding}
    monkeypatch.setattr(ui.st, "session_state", state)
    prepared = _prepared_in_state(state, runtime)
    before = _committed(runtime)
    data = json.loads(export_review_session(_mixed_session(runtime)))
    data["reviews"][-1]["note"] = "late invalid\0note"
    with pytest.raises(ReviewSessionError):
        ui._apply_review_session(runtime, json.dumps(data).encode())
    _assert_committed(runtime, before)
    assert state[ui._REVIEW_PREPARED_KEY] is prepared


def test_renamed_identical_uploads_require_rerun_then_accept_saved_session():
    app = _run_app()
    original = _assert_active(app)
    _select(app)
    _save_note(app, "resumable renamed files 🧾")
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    saved = export_review_session(original.snapshot())
    original_fingerprint = original.binding.result_fingerprint
    original_reviews = original.snapshot().reviews
    _prepare(app)
    app.file_uploader(key="upload_A").set_value(("renamed_A.csv", DATA_A, "text/csv"))
    app.file_uploader(key="upload_B").set_value(("renamed_B.csv", DATA_B, "text/csv")).run()
    assert not app.exception
    assert "completed" not in app.session_state and ui._REVIEW_STATE_KEY not in app.session_state
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    assert not any(widget.label == UPLOAD_LABEL for widget in app.file_uploader)
    _simple_mapping(app)
    app.button(key="run").click().run()
    current = _assert_active(app)
    assert (
        current.binding is not original.binding
        and current.binding.result is not original.binding.result
    )
    assert current.binding.result_fingerprint == original_fingerprint
    assert current.snapshot().unreviewed_count == 2
    _apply(app, saved)
    assert current.snapshot().reviews == original_reviews
    assert current.annotation_revision == current.editor_generation == 1
    assert current.binding is app.session_state[ui._REVIEW_BINDING_KEY]


def test_prepare_installs_exact_stable_bytes_filename_mime_and_ignore_download_callbacks(downloads):
    app = _run_app()
    runtime = _assert_active(app)
    _assert_downloads(app, prepared=False)
    _select(app)
    _save_note(app, '=SUM(A1:A2)\n"exact",🧾')
    before = _committed(runtime)
    prepared = _prepare(app)
    _assert_committed(runtime, before)
    _assert_downloads(app, prepared=True)
    expected = {
        REVIEWED_LABEL: (
            prepared.reviewed_csv,
            "tallydiff_reviewed_exceptions.csv",
            "text/csv; charset=utf-8",
        ),
        SESSION_LABEL: (prepared.session_json, "tallydiff_review_session.json", "application/json"),
    }
    for label, (data, filename, mime) in expected.items():
        assert downloads[label]["data"] is data
        assert downloads[label]["file_name"] == filename
        assert downloads[label]["mime"] == mime
        assert downloads[label]["on_click"] == "ignore"
    assert prepared.session_json == export_review_session(runtime.snapshot())
    assert prepared.reviewed_csv == export_reviewed_exceptions_csv(
        runtime.binding, runtime.snapshot()
    )
    assert downloads["Download exception report"]["data"] == export_exceptions_csv(
        runtime.binding.result
    )


def test_native_form_prepare_commits_submitted_note_before_snapshot():
    """AppTest models the submitted server batch, not browser focus/event ordering."""

    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "baseline committed note")
    previous = _prepare(app)
    key = runtime.binding.exception_keys[0]
    prior_review = runtime.reviews_by_key[key]
    revision, generation = runtime.annotation_revision, runtime.editor_generation
    _widget(app, "Analyst note").set_value("=1+2\nFollow-up café")
    _button(app, PREPARE_LABEL).click().run()
    assert not app.exception
    assert runtime.annotation_revision == revision + 1
    assert runtime.editor_generation == generation
    assert runtime.reviews_by_key[key] is not prior_review
    assert runtime.reviews_by_key[key].disposition is prior_review.disposition
    prepared = app.session_state[ui._REVIEW_PREPARED_KEY]
    assert prepared is not previous
    assert prepared.annotation_revision == runtime.annotation_revision
    assert json.loads(prepared.session_json)["reviews"][0]["note"] == "=1+2\nFollow-up café"
    assert prepared.session_json == export_review_session(runtime.snapshot())
    assert prepared.reviewed_csv == export_reviewed_exceptions_csv(
        runtime.binding, runtime.snapshot()
    )
    assert _rows(prepared.reviewed_csv)[1][-1] == "Note: =1+2\nFollow-up café"


def test_note_save_invalidates_old_artifacts_then_prepare_captures_exact_note(downloads):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.NEEDS_FOLLOW_UP).run()
    _save_note(app, "baseline committed note")
    previous = _prepare(app)
    key = runtime.binding.exception_keys[0]
    binding, result = runtime.binding, runtime.binding.result
    findings = result.findings
    original_csv = export_exceptions_csv(result)
    revision, generation = runtime.annotation_revision, runtime.editor_generation
    prior_review = runtime.reviews_by_key[key]
    note = "=1+2\nFollow-up café"

    _save_note(app, note)
    assert not app.exception and not app.error
    assert any(caption.value == "Note saved." for caption in app.caption)
    assert runtime.reviews_by_key[key] == FindingReview(key, prior_review.disposition, note)
    assert runtime.reviews_by_key[key] is not prior_review
    assert runtime.annotation_revision == revision + 1
    assert runtime.editor_generation == generation
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    _assert_downloads(app, prepared=False)

    prepared = _prepare(app)
    assert prepared is not previous
    assert prepared.result_fingerprint == binding.result_fingerprint
    assert prepared.annotation_revision == runtime.annotation_revision == revision + 1
    assert runtime.editor_generation == generation
    assert prepared.session_json == export_review_session(runtime.snapshot())
    assert prepared.reviewed_csv == export_reviewed_exceptions_csv(binding, runtime.snapshot())
    saved = json.loads(prepared.session_json)
    assert saved["version"] == 1
    assert saved.keys() == json.loads(previous.session_json).keys()
    assert saved["reviews"][0] == {
        "matching_key": list(key),
        "disposition": prior_review.disposition.value,
        "note": note,
    }
    original_rows, reviewed_rows = _rows(original_csv), _rows(prepared.reviewed_csv)
    assert [row[:-2] for row in reviewed_rows] == original_rows
    assert reviewed_rows[1][-2:] == ["Needs follow-up", "Note: " + note]
    assert runtime.binding is binding and binding.result is result
    assert app.session_state["completed"][1] is result and result.findings is findings
    assert export_exceptions_csv(result) == original_csv
    assert downloads["Download exception report"]["data"] == original_csv


def test_selected_note_and_prepare_share_native_form_with_independent_disposition():
    app = _run_app()
    assert not any(button.label == SAVE_LABEL for button in app.button)
    assert _button(app, PREPARE_LABEL).key == ui._REVIEW_PREPARE_KEY
    assert not _button(app, PREPARE_LABEL).proto.form_id
    _select(app)
    note = _widget(app, "Analyst note")
    save, prepare = _button(app, SAVE_LABEL), _button(app, PREPARE_LABEL)
    assert note.proto.form_id
    assert save.proto.form_id == prepare.proto.form_id == note.proto.form_id
    assert save.proto.is_form_submitter and prepare.proto.is_form_submitter
    assert not save.disabled and not prepare.disabled
    assert len([button for button in app.button if button.label == PREPARE_LABEL]) == 1
    assert not _widget(app, "Analyst disposition").proto.form_id
    assert any(
        "Save note" in caption.value and "Prepare review downloads" in caption.value
        for caption in app.caption
    )


def test_identical_selected_form_prepare_preserves_committed_scope_and_equivalent_bytes():
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "=1+2\nFollow-up café")
    previous = _prepare(app)
    before = _committed(runtime)
    current = _prepare(app)
    _assert_committed(runtime, before)
    assert current.result_fingerprint == previous.result_fingerprint
    assert (
        current.annotation_revision == previous.annotation_revision == runtime.annotation_revision
    )
    assert current.session_json == previous.session_json
    assert current.reviewed_csv == previous.reviewed_csv
    _assert_downloads(app, prepared=True)


def test_unsubmitted_note_does_not_commit_on_disposition_change():
    """A server-side form candidate is not authoritative until a form submit."""

    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "authoritative committed note")
    prepared = _prepare(app)
    before = runtime.annotation_revision
    key = runtime.binding.exception_keys[0]
    _widget(app, "Analyst note").set_value("pending different note").run()
    assert not app.exception
    assert runtime.reviews_by_key[key].note == "authoritative committed note"
    assert runtime.annotation_revision == before
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    assert not app.exception
    assert runtime.reviews_by_key[key] == FindingReview(
        key, ReviewDisposition.REVIEW_COMPLETE, "authoritative committed note"
    )
    assert runtime.annotation_revision == before + 1
    assert runtime.editor_generation == 0
    assert ui._REVIEW_PREPARED_KEY not in app.session_state


def test_form_prepare_uses_authoritative_disposition_instead_of_stale_widget(monkeypatch):
    runtime, state = _fake_active(monkeypatch)
    key = runtime.binding.exception_keys[0]
    committed = FindingReview(key, ReviewDisposition.REVIEW_COMPLETE, "baseline")
    runtime.reviews_by_key[key] = committed
    original_csv = export_exceptions_csv(runtime.binding.result)
    _prepared_in_state(state, runtime)
    state[_editor_key(runtime, "note")] = "=1+2\nFollow-up café"
    state[_editor_key(runtime, "disposition")] = ReviewDisposition.UNREVIEWED
    _invoke(ui._commit_review_note_and_prepare, runtime)
    assert runtime.reviews_by_key[key] == FindingReview(
        key, committed.disposition, "=1+2\nFollow-up café"
    )
    assert runtime.annotation_revision == 1 and runtime.editor_generation == 0
    assert ui._REVIEW_PREPARED_KEY not in state
    ui._prepare_requested_review_downloads()
    prepared = state[ui._REVIEW_PREPARED_KEY]
    assert prepared.annotation_revision == runtime.annotation_revision == 1
    assert prepared.session_json == export_review_session(runtime.snapshot())
    assert prepared.reviewed_csv == export_reviewed_exceptions_csv(
        runtime.binding, runtime.snapshot()
    )
    assert export_exceptions_csv(runtime.binding.result) == original_csv


@pytest.mark.parametrize("note", INVALID_NOTES)
@pytest.mark.parametrize("prior_prepared", (False, True), ids=("unprepared", "prepared"))
def test_invalid_form_prepare_preserves_committed_state_and_draft_without_new_snapshot(
    monkeypatch, note, prior_prepared
):
    runtime, state = _fake_active(monkeypatch)
    key = runtime.binding.exception_keys[0]
    runtime.reviews_by_key[key] = FindingReview(
        key, ReviewDisposition.NEEDS_FOLLOW_UP, "valid authoritative baseline"
    )
    previous = _prepared_in_state(state, runtime) if prior_prepared else None
    before = _committed(runtime)
    widget_key = _editor_key(runtime, "note")
    state[widget_key] = note
    _invoke(ui._commit_review_note_and_prepare, runtime)
    ui._prepare_requested_review_downloads()
    _assert_committed(runtime, before)
    assert state.get(ui._REVIEW_PREPARED_KEY) is previous
    assert ui._REVIEW_PREPARE_REQUEST_KEY not in state
    assert state[widget_key] == state[ui._REVIEW_NOTE_DRAFTS_KEY][widget_key] == note
    error = state[ui._REVIEW_NOTE_ERRORS_KEY][widget_key]
    assert error.startswith("Analyst note: ") and len(error) < 350 and note not in error
    if previous is not None:
        assert previous.session_json == export_review_session(runtime.snapshot())
        assert previous.reviewed_csv == export_reviewed_exceptions_csv(
            runtime.binding, runtime.snapshot()
        )


def test_invalid_native_form_prepare_stays_editable_and_preserves_current_downloads():
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "committed baseline")
    previous = _prepare(app)
    before = _committed(runtime)
    draft = "invalid\0pending note"
    _widget(app, "Analyst note").set_value(draft)
    _button(app, PREPARE_LABEL).click().run()
    assert not app.exception and app.error
    _assert_committed(runtime, before)
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is previous
    assert _widget(app, "Analyst note").value == draft
    assert not any(caption.value == "Note saved." for caption in app.caption)
    assert all(len(error.value) < 350 and draft not in error.value for error in app.error)
    _assert_downloads(app, prepared=True)
    _save_note(app, "corrected note")
    assert not app.exception and not app.error
    assert runtime.annotation_revision == before[2] + 1
    assert runtime.editor_generation == before[3]
    assert ui._REVIEW_PREPARED_KEY not in app.session_state


@pytest.mark.parametrize("scope", ("fingerprint", "generation"))
def test_stale_form_prepare_cannot_commit_or_request_a_new_snapshot(monkeypatch, scope):
    runtime, state = _fake_active(monkeypatch)
    previous = _prepared_in_state(state, runtime)
    before = _committed(runtime)
    fingerprint = "f" * 64 if scope == "fingerprint" else runtime.binding.result_fingerprint
    generation = 1 if scope == "generation" else runtime.editor_generation
    key = runtime.binding.exception_keys[0]
    state[ui._review_widget_key("note", fingerprint, key, generation)] = "stale note"
    _invoke(
        ui._commit_review_note_and_prepare,
        runtime,
        fingerprint=fingerprint,
        generation=generation,
    )
    ui._prepare_requested_review_downloads()
    _assert_committed(runtime, before)
    assert state[ui._REVIEW_PREPARED_KEY] is previous
    assert ui._REVIEW_PREPARE_REQUEST_KEY not in state


@pytest.mark.parametrize("visible", ("one", "zero"))
def test_preparation_exports_complete_population_independent_of_filters(visible):
    app = _review_app()
    runtime = _assert_active(app)
    imported = _mixed_session(runtime)
    _apply(app, export_review_session(imported))
    _widget(app, "Search matching keys").set_value(
        "BIG" if visible == "one" else "NO-SUCH-KEY"
    ).run()
    assert (len(_exception_table(app).value) if _exception_table(app) else 0) == int(
        visible == "one"
    )
    prepared = _prepare(app)
    full_keys = runtime.binding.exception_keys
    assert (
        tuple(
            tuple(review["matching_key"]) for review in json.loads(prepared.session_json)["reviews"]
        )
        == full_keys
    )
    assert [row[1] for row in _rows(prepared.reviewed_csv)[1:]] == [
        " / ".join(key) for key in full_keys
    ]
    assert prepared.session_json == export_review_session(imported)
    assert prepared.reviewed_csv == export_reviewed_exceptions_csv(runtime.binding, imported)


def test_filters_navigation_and_downloads_preserve_prepared_object_without_regeneration(
    monkeypatch, downloads
):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "stable note")
    prepared = _prepare(app)
    before = _committed(runtime)

    def unexpected(*args, **kwargs):
        raise AssertionError("prepared artifacts must not regenerate on ordinary reruns")

    monkeypatch.setattr(tallydiff, "export_review_session", unexpected)
    monkeypatch.setattr(tallydiff, "export_reviewed_exceptions_csv", unexpected)
    changes = (
        ("Search matching keys", "A"),
        ("Search matching keys", ""),
        ("Exception sort order", "absolute_delta_desc"),
        ("Review disposition", [ReviewDisposition.UNREVIEWED]),
        ("Minimum absolute delta", "1"),
    )
    for label, value in changes:
        _widget(app, label).set_value(value).run()
        assert not app.exception
        assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
        _assert_committed(runtime, before)
        assert downloads[REVIEWED_LABEL]["data"] is prepared.reviewed_csv
        assert downloads[SESSION_LABEL]["data"] is prepared.session_json
    _select(app, 1)
    filters = _filters(app)
    table_key = _exception_table(app).key
    editor_note = _widget(app, "Analyst note").value
    for label in (REVIEWED_LABEL, SESSION_LABEL):
        next(button for button in app.download_button if button.label == label).click().run()
        assert not app.exception
        _assert_committed(runtime, before)
        assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
        assert _filters(app) == filters
        assert _exception_table(app).key == table_key
        assert _widget(app, "Analyst note").value == editor_note
        _assert_downloads(app, prepared=True)


@pytest.mark.parametrize("field", ("note", "disposition"))
def test_committed_edit_removes_artifacts_and_next_prepare_captures_the_change(field):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    before = _prepare(app)
    value = "changed committed note" if field == "note" else ReviewDisposition.REVIEW_COMPLETE
    if field == "note":
        _save_note(app, value)
    else:
        _widget(app, "Analyst disposition").set_value(value).run()
    assert not app.exception
    assert runtime.annotation_revision == 1 and runtime.editor_generation == 0
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    _assert_downloads(app, prepared=False)
    updated = _prepare(app)
    assert (
        updated.session_json != before.session_json and updated.reviewed_csv != before.reviewed_csv
    )
    assert updated.session_json == export_review_session(runtime.snapshot())
    assert updated.reviewed_csv == export_reviewed_exceptions_csv(
        runtime.binding, runtime.snapshot()
    )


@pytest.mark.parametrize("field", ("note", "disposition"))
def test_noop_edit_keeps_prepared_downloads_and_revision(field):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    prepared = _prepare(app)
    widget = _widget(app, "Analyst note" if field == "note" else "Analyst disposition")
    if field == "note":
        _save_note(app, widget.value)
    else:
        widget.set_value(widget.value).run()
    assert not app.exception
    assert runtime.annotation_revision == runtime.editor_generation == 0
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    _assert_downloads(app, prepared=True)


def test_invalid_draft_keeps_committed_artifacts_until_valid_correction(downloads):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "valid authoritative")
    prepared = _prepare(app)
    before = _committed(runtime)
    _save_note(app, "bad\0uncommitted draft")
    assert not app.exception and app.error
    _assert_committed(runtime, before)
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    assert downloads[REVIEWED_LABEL]["data"] is prepared.reviewed_csv
    assert downloads[SESSION_LABEL]["data"] is prepared.session_json
    _assert_downloads(app, prepared=True)
    _save_note(app, "valid correction")
    assert not app.exception and not app.error
    assert runtime.annotation_revision == before[2] + 1
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    _assert_downloads(app, prepared=False)


def test_successful_apply_invalidates_artifacts_and_reprepare_uses_imported_bytes():
    app = _run_app()
    runtime = _assert_active(app)
    prepared = _prepare(app)
    imported = _mixed_session(runtime)
    _apply(app, export_review_session(imported))
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    _assert_downloads(app, prepared=False)
    updated = _prepare(app)
    assert updated is not prepared
    assert updated.annotation_revision == 1
    assert updated.session_json == export_review_session(imported)
    assert updated.reviewed_csv == export_reviewed_exceptions_csv(runtime.binding, imported)


def test_run_click_always_clears_prepared_artifacts_but_preserves_equivalent_annotations():
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _save_note(app, "preserve annotations on equivalent Run")
    prepared = _prepare(app)
    before = _committed(runtime)
    app.button(key="run").click().run()
    adopted = _assert_active(app)
    assert adopted is not runtime and adopted.binding is not runtime.binding
    assert adopted.binding.result is not runtime.binding.result
    assert adopted.binding.result_fingerprint == prepared.result_fingerprint
    assert export_review_session(adopted.snapshot()) == before[0]
    assert tuple(adopted.reviews_by_key.items()) == before[1]
    assert adopted.annotation_revision == before[2] and adopted.editor_generation == before[3]
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    _assert_downloads(app, prepared=False)


@pytest.mark.parametrize("mismatch", ("fingerprint", "revision"))
def test_renderer_clears_and_suppresses_stale_prepared_metadata(mismatch):
    app = _run_app()
    runtime = _assert_active(app)
    prepared = _prepare(app)
    app.session_state[ui._REVIEW_PREPARED_KEY] = replace(
        prepared,
        result_fingerprint="f" * 64 if mismatch == "fingerprint" else prepared.result_fingerprint,
        annotation_revision=1 if mismatch == "revision" else prepared.annotation_revision,
    )
    before = _committed(runtime)
    app.run()
    assert not app.exception
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    _assert_downloads(app, prepared=False)
    _assert_committed(runtime, before)


@pytest.mark.parametrize(
    "corruption", ("missing", "reordered", "invalid-note", "negative-revision", "wrong-binding")
)
def test_prepare_corrupt_runtime_reports_error_without_silent_stage5_recovery(corruption):
    app = _run_app()
    runtime = _assert_active(app)
    binding, result = runtime.binding, runtime.binding.result
    _select(app)
    _prepare(app)
    _corrupt_runtime(runtime, corruption)
    reviews = runtime.reviews_by_key
    items = tuple(reviews.items())
    revision, generation = runtime.annotation_revision, runtime.editor_generation
    _button(app, PREPARE_LABEL).click().run()
    assert not app.exception and app.error
    assert app.session_state[ui._REVIEW_STATE_KEY] is runtime
    assert runtime.binding is (None if corruption == "wrong-binding" else binding)
    assert app.session_state["completed"][1] is result
    assert runtime.reviews_by_key is reviews and tuple(reviews.items()) == items
    assert (runtime.annotation_revision, runtime.editor_generation) == (revision, generation)
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    assert not any(
        button.label in (REVIEWED_LABEL, SESSION_LABEL) for button in app.download_button
    )
    assert all(len(error.value) < 350 for error in app.error)
    app.run()
    assert not app.exception and app.error
    assert app.session_state[ui._REVIEW_STATE_KEY] is runtime
    assert runtime.reviews_by_key is reviews and tuple(reviews.items()) == items
    assert (runtime.annotation_revision, runtime.editor_generation) == (revision, generation)
    assert not any(
        button.label in (REVIEWED_LABEL, SESSION_LABEL) for button in app.download_button
    )


@pytest.mark.parametrize("exporter", ("export_review_session", "export_reviewed_exceptions_csv"))
@pytest.mark.parametrize("expected", (False, True), ids=("programming-error", "integrity-error"))
def test_prepare_catches_only_domain_integrity_errors(monkeypatch, exporter, expected):
    app = _run_app()
    runtime = _assert_active(app)
    _prepare(app)
    before = _committed(runtime)

    def failure(*args, **kwargs):
        raise (
            ReviewSessionError("test review integrity failure")
            if expected
            else RuntimeError("test programmer defect")
        )

    monkeypatch.setattr(tallydiff, exporter, failure)
    _button(app, PREPARE_LABEL).click().run()
    _assert_committed(runtime, before)
    if expected:
        assert not app.exception
        assert any("test review integrity failure" in error.value for error in app.error)
        assert ui._REVIEW_PREPARED_KEY not in app.session_state
        _assert_downloads(app, prepared=False)
    else:
        assert app.exception
        assert any("test programmer defect" in error.message for error in app.exception)


@pytest.mark.parametrize("corruption", ("invalid-note", "wrong-binding"))
def test_failed_apply_preserves_corrupt_runtime_without_stage5_repair_on_later_reruns(corruption):
    app = _run_app()
    runtime = _assert_active(app)
    binding, result = runtime.binding, runtime.binding.result
    completed = app.session_state["completed"]
    original_csv = export_exceptions_csv(result)
    _select(app)
    _save_note(app, "committed before corruption")
    _upload(app, export_review_session(_mixed_session(runtime)))
    prepared = _prepare(app)
    selection = app.session_state[ui._REVIEW_EDITOR_SELECTION_KEY]
    _corrupt_runtime(runtime, corruption)
    reviews = runtime.reviews_by_key
    items = tuple(reviews.items())
    revision, generation = runtime.annotation_revision, runtime.editor_generation
    _button(app, APPLY_LABEL).click().run()
    for rerun in (False, True):
        if rerun:
            app.run()
        assert not app.exception
        assert any(error.value.startswith("Review session: ") for error in app.error)
        assert all(len(error.value) < 350 for error in app.error)
        assert app.session_state[ui._REVIEW_STATE_KEY] is runtime
        assert runtime.binding is (None if corruption == "wrong-binding" else binding)
        assert app.session_state[ui._REVIEW_BINDING_KEY] is binding
        assert app.session_state["completed"] is completed
        assert binding.result is result
        assert runtime.reviews_by_key is reviews and tuple(reviews.items()) == items
        assert all(runtime.reviews_by_key[key] is review for key, review in items)
        assert (runtime.annotation_revision, runtime.editor_generation) == (revision, generation)
        assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
        assert app.session_state[ui._REVIEW_EDITOR_SELECTION_KEY] == selection
        assert export_exceptions_csv(result) == original_csv
        _assert_downloads(app, prepared=False)


def test_apply_does_not_hide_unrelated_loader_programming_errors(monkeypatch):
    app = _run_app()
    runtime = _assert_active(app)
    before = _committed(runtime)

    def failure(*args, **kwargs):
        raise RuntimeError("test loader programmer defect")

    monkeypatch.setattr(tallydiff, "load_review_session", failure)
    _upload(app, export_review_session(_mixed_session(runtime)))
    _button(app, APPLY_LABEL).click().run()
    assert app.exception
    assert any("test loader programmer defect" in error.message for error in app.exception)
    _assert_committed(runtime, before)


def test_apply_filters_use_imported_population_and_field_edits_keep_imported_counterpart():
    app = _run_app()
    runtime = _assert_active(app)
    _widget(app, "Review disposition").set_value([ReviewDisposition.REVIEW_COMPLETE]).run()
    imported = _mixed_session(runtime)
    _apply(app, export_review_session(imported))
    assert _widget(app, "Review disposition").value == [ReviewDisposition.REVIEW_COMPLETE]
    assert _exception_table(app).value["Matching key"].tolist() == ["B"]
    _select(app)
    key = imported.reviews[1].matching_key
    assert _widget(app, "Analyst note").value == imported.reviews[1].note
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.NEEDS_FOLLOW_UP).run()
    assert runtime.reviews_by_key[key] == FindingReview(
        key, ReviewDisposition.NEEDS_FOLLOW_UP, imported.reviews[1].note
    )
    assert runtime.annotation_revision == 2 and runtime.editor_generation == 1
    assert not app.text_area and _exception_table(app) is None


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_clean_completed_runs_omit_all_new_persistence_controls(mode):
    app = _uploaded_app(b"id,amount\nCLEAN,1\n", b"id,amount\nCLEAN,1\n")
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    assert runtime.binding.exception_keys == () and runtime.snapshot().reviews == ()
    assert not any(widget.label == UPLOAD_LABEL for widget in app.file_uploader)
    assert not any(
        button.label in (APPLY_LABEL, PREPARE_LABEL, SAVE_LABEL) for button in app.button
    )
    assert not any(
        button.label in (REVIEWED_LABEL, SESSION_LABEL) for button in app.download_button
    )
    assert not any(metric.label == "Review items" for metric in app.metric)


def test_persistence_never_rebinds_reingests_reconciles_normalizes_or_researches_subsets(
    monkeypatch,
):
    app = _run_app(mode=ReconciliationMode.BOUNDED_ONE_TO_MANY, comparisons=True)
    runtime = _assert_active(app)
    imported = _mixed_session(runtime)

    def unexpected(*args, **kwargs):
        raise AssertionError("persistence must use stored reconciliation evidence only")

    for module, names in (
        (tallydiff, ("bind_review_result", "reconcile", "ingest_csv", "ingest_xlsx")),
        (engine, ("normalize_key", "analyze_bounded_one_to_many", "_compare_fields")),
        (ingest, ("ingest_csv",)),
        (normalization, ("normalize_key",)),
        (subset_matching, ("analyze_bounded_one_to_many",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, unexpected)
    _apply(app, export_review_session(imported))
    prepared = _prepare(app)
    for label in (REVIEWED_LABEL, SESSION_LABEL):
        next(button for button in app.download_button if button.label == label).click().run()
        assert not app.exception
    assert runtime.snapshot().reviews == imported.reviews
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared


def test_import_prepare_and_download_preserve_every_piece_of_reconciliation_evidence(downloads):
    data_a = b"id,amount,currency\nGROUP,300,USD\nORDINARY,10,USD\nTOLERATED,1,USD\nCLEAN,2,USD\n"
    data_b = (
        b"id,amount,currency\nGROUP,100,USD\nGROUP,200,USD\nGROUP,7,USD\n"
        b"ORDINARY,9,CAD\nTOLERATED,1.01,USD\nCLEAN,2,USD\n"
    )
    app = _uploaded_app(data_a, data_b)
    _simple_mapping(app)
    app.button(key="add_comparison").click().run()
    for side in ("a", "b"):
        app.selectbox(key=f"map_comparison_{side}_0").set_value("currency")
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.BOUNDED_ONE_TO_MANY)
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    result, binding = runtime.binding.result, runtime.binding
    findings, exceptions, tolerated = result.findings, result.exceptions, result.tolerated_findings
    assert any(finding.correspondence_analysis is not None for finding in findings)
    assert any(finding.field_comparisons for finding in findings)
    assert tolerated
    fingerprint = binding.result_fingerprint
    original_csv = export_exceptions_csv(result)
    evidence = [
        (
            finding,
            finding.field_comparisons,
            finding.correspondence_analysis,
            tuple(
                (row, row.raw_fields, tuple(row.raw_fields.items()))
                for row in (*finding.rows_a, *finding.rows_b)
            ),
        )
        for finding in findings
    ]
    totals = (result.total_a, result.total_b, result.control_difference)
    _apply(app, export_review_session(_mixed_session(runtime)))
    prepared = _prepare(app)
    for label in (REVIEWED_LABEL, SESSION_LABEL):
        next(button for button in app.download_button if button.label == label).click().run()
        assert not app.exception
    assert runtime.binding is binding and binding.result is result
    assert result.findings is findings
    assert result.exceptions == exceptions and result.tolerated_findings == tolerated
    assert all(current is old for current, old in zip(result.exceptions, exceptions, strict=True))
    assert (result.total_a, result.total_b, result.control_difference) == totals
    assert binding.result_fingerprint == fingerprint
    for finding, comparisons, correspondence, raw in evidence:
        assert finding.field_comparisons is comparisons
        assert finding.correspondence_analysis is correspondence
        for row, raw_fields, items in raw:
            assert row.raw_fields is raw_fields and tuple(row.raw_fields.items()) == items
    assert export_exceptions_csv(result) == original_csv
    assert downloads["Download exception report"]["data"] == original_csv
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    assert len(_rows(prepared.reviewed_csv)) == len(exceptions) + 1
    assert _exception_downloads(app)
