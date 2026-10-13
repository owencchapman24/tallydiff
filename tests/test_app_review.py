"""Analyst edits own workflow metadata without changing reconciliation evidence."""

import csv
from decimal import Decimal
from io import StringIO
from itertools import product

import pytest
from test_app import (
    _exception_downloads,
    _exception_table,
    _review_widget,
    _simple_mapping,
    _uploaded_app,
)
from test_app import (
    _review_app as _financial_review_app,
)
from test_app import review_downloads as review_downloads
from test_app_comparisons import (
    _comparisons,
    _secondary_checkbox,
)
from test_app_comparisons import (
    _review_app as _secondary_app,
)
from test_app_review_state import _assert_active, _binding, _corrupt_runtime, _run_app

import tallydiff
import tallydiff.app as ui
from tallydiff import (
    ColumnMapping,
    FindingCategory,
    FindingReview,
    ReconciliationMode,
    ReviewDisposition,
    ReviewSessionError,
    Source,
    SourceRecord,
    bind_review_result,
    export_exceptions_csv,
    export_review_session,
    export_reviewed_exceptions_csv,
    reconcile,
)
from tallydiff.presentation import finding_rows

DISPOSITIONS = tuple(ReviewDisposition)
LABELS = ("Unreviewed", "Needs follow-up", "Review complete")
PROGRESS_LABELS = ("Review items", *LABELS)


def _widget(app, label):
    return next(
        widget
        for widget in (*app.selectbox, *app.multiselect, *app.text_area, *app.text_input)
        if widget.label == label
    )


def _select(app, index=0):
    table = _exception_table(app)
    app.session_state[table.key] = {"selection": {"rows": [index], "columns": [], "cells": []}}
    app.run()
    assert not app.exception
    return table.key


def _progress(app, *, unreviewed, follow_up, complete):
    total = unreviewed + follow_up + complete
    actual = [
        (metric.label, metric.value) for metric in app.metric if metric.label in PROGRESS_LABELS
    ]
    assert actual == list(
        zip(PROGRESS_LABELS, map(str, (total, unreviewed, follow_up, complete)), strict=True)
    )
    assert any(
        caption.value
        == f"Reviewed: {follow_up + complete} · Open review items: {unreviewed + follow_up}"
        for caption in app.caption
    )


def _editor_key(runtime, field, key=None):
    return ui._review_widget_key(
        field,
        runtime.binding.result_fingerprint,
        key or runtime.binding.exception_keys[0],
        runtime.editor_generation,
    )


def _fake_active(monkeypatch):
    runtime = ui._adopt_review_binding(_binding())
    state = {
        "completed": ("configuration", runtime.binding.result),
        ui._REVIEW_STATE_KEY: runtime,
        ui._REVIEW_BINDING_KEY: runtime.binding,
    }
    monkeypatch.setattr(ui.st, "session_state", state)
    return runtime, state


def _invoke(callback, runtime, key=None, *, fingerprint=None, generation=None):
    callback(
        runtime.binding.result_fingerprint if fingerprint is None else fingerprint,
        runtime.binding.exception_keys[0] if key is None else key,
        runtime.editor_generation if generation is None else generation,
    )


def _rows(data):
    return list(csv.reader(StringIO(data.decode("utf-8"), newline="")))


def test_editor_keys_use_all_scope_components_and_distinguish_display_collisions():
    keys = (("A", "B / C"), ("A / B", "C"))
    combinations = list(product(("disposition", "note"), ("a" * 64, "b" * 64), keys, (0, 1)))
    generated = [ui._review_widget_key(*args) for args in combinations]
    assert len(set(generated)) == len(combinations)
    assert all(key.startswith("review_") and key.isascii() and len(key) < 240 for key in generated)
    assert generated == [ui._review_widget_key(*args) for args in combinations]
    assert " / ".join(keys[0]) == " / ".join(keys[1])


@pytest.mark.parametrize(
    ("old", "new"),
    list(product(DISPOSITIONS, repeat=2)),
    ids=[f"{old.value}-to-{new.value}" for old, new in product(DISPOSITIONS, repeat=2)],
)
def test_disposition_callback_commits_new_value_with_authoritative_note(monkeypatch, old, new):
    runtime, state = _fake_active(monkeypatch)
    first, second = runtime.binding.exception_keys
    committed = FindingReview(first, old, " authoritative\t\r\n=1+2 🧾 ")
    runtime.reviews_by_key[first] = committed
    untouched = runtime.reviews_by_key[second]
    snapshot = runtime.snapshot()
    state[_editor_key(runtime, "disposition")] = new
    state[_editor_key(runtime, "note")] = "invalid uncommitted\0note"
    _invoke(ui._commit_review_disposition, runtime)
    updated = runtime.reviews_by_key[first]
    assert updated == FindingReview(first, new, committed.note)
    assert runtime.annotation_revision == int(old is not new)
    assert runtime.editor_generation == 0
    assert tuple(runtime.reviews_by_key) == runtime.binding.exception_keys
    assert runtime.reviews_by_key[second] is untouched
    assert snapshot.reviews[0] is committed and snapshot.reviews[0].disposition is old
    if old is not new:
        assert updated is not committed
    _invoke(ui._commit_review_disposition, runtime)
    assert runtime.annotation_revision == int(old is not new)


NOTES = [
    pytest.param("Ordinary analyst note", id="prose"),
    pytest.param("", id="empty"),
    pytest.param(" leading and trailing ", id="spaces"),
    pytest.param(" \t ", id="whitespace"),
    pytest.param("left\tright", id="tab"),
    pytest.param("left\nright", id="lf"),
    pytest.param("left\rright", id="cr"),
    pytest.param("left\r\nright", id="crlf"),
    pytest.param("第一行\n第二行", id="unicode"),
    pytest.param("🧾 🧠", id="emoji"),
    pytest.param("e\u0301", id="combining"),
    pytest.param('comma, "quotes" \\ backslash', id="csv-text"),
    pytest.param("=SUM(A1:A2)", id="equals"),
    pytest.param("+1+2", id="plus"),
    pytest.param("-1+2", id="minus"),
    pytest.param("@SUM(A1:A2)", id="at"),
    pytest.param(" \t=1+2", id="leading-formula"),
    pytest.param('"\r\nInjected,=1+2\r\n"', id="csv-breakout-text"),
    pytest.param("a" * 16_384, id="ascii-byte-limit"),
    pytest.param("🧾" * 4_096, id="utf8-byte-limit"),
]


@pytest.mark.parametrize("note", NOTES)
def test_note_callback_preserves_exact_text_and_authoritative_disposition(monkeypatch, note):
    runtime, state = _fake_active(monkeypatch)
    first, second = runtime.binding.exception_keys
    committed = FindingReview(first, ReviewDisposition.REVIEW_COMPLETE, "previous")
    runtime.reviews_by_key[first] = committed
    untouched = runtime.reviews_by_key[second]
    state[_editor_key(runtime, "note")] = note
    state[_editor_key(runtime, "disposition")] = ReviewDisposition.UNREVIEWED
    _invoke(ui._commit_review_note, runtime)
    assert runtime.reviews_by_key[first] == FindingReview(first, committed.disposition, note)
    assert runtime.reviews_by_key[second] is untouched
    assert runtime.annotation_revision == 1 and runtime.editor_generation == 0
    assert tuple(runtime.reviews_by_key) == runtime.binding.exception_keys
    _invoke(ui._commit_review_note, runtime)
    assert runtime.annotation_revision == 1
    session_bytes = export_review_session(runtime.snapshot())
    assert session_bytes == export_review_session(runtime.snapshot())
    reviewed = _rows(export_reviewed_exceptions_csv(runtime.binding, runtime.snapshot()))
    assert reviewed[1][-2:] == ["Review complete", "Note: " + note if note else ""]


INVALID_NOTES = [
    pytest.param("invalid\0note", id="nul"),
    pytest.param("invalid\x01note", id="c0-control"),
    pytest.param("invalid\x7fnote", id="del"),
    pytest.param("invalid\x85note", id="c1-control"),
    pytest.param("invalid\ud800note", id="surrogate"),
    pytest.param("a" * 16_385, id="ascii-too-long"),
    pytest.param("🧾" * 4_097, id="utf8-too-long"),
]


@pytest.mark.parametrize("note", INVALID_NOTES)
def test_invalid_note_callback_retains_draft_without_changing_committed_exports(monkeypatch, note):
    runtime, state = _fake_active(monkeypatch)
    key = runtime.binding.exception_keys[0]
    committed = FindingReview(key, ReviewDisposition.NEEDS_FOLLOW_UP, "valid authoritative note")
    runtime.reviews_by_key[key] = committed
    note_key = _editor_key(runtime, "note")
    before_json = export_review_session(runtime.snapshot())
    before_csv = export_reviewed_exceptions_csv(runtime.binding, runtime.snapshot())
    state[note_key] = note
    _invoke(ui._commit_review_note, runtime)
    assert runtime.reviews_by_key[key] is committed
    assert runtime.annotation_revision == runtime.editor_generation == 0
    assert state[note_key] == note
    assert state[ui._REVIEW_NOTE_DRAFTS_KEY][note_key] == note
    error = state[ui._REVIEW_NOTE_ERRORS_KEY][note_key]
    assert isinstance(error, str) and error and len(error) < 350
    assert note not in error
    assert export_review_session(runtime.snapshot()) == before_json
    assert export_reviewed_exceptions_csv(runtime.binding, runtime.snapshot()) == before_csv
    state[_editor_key(runtime, "disposition")] = ReviewDisposition.REVIEW_COMPLETE
    _invoke(ui._commit_review_disposition, runtime)
    assert runtime.reviews_by_key[key] == FindingReview(
        key, ReviewDisposition.REVIEW_COMPLETE, committed.note
    )
    assert state[note_key] == note and state[ui._REVIEW_NOTE_DRAFTS_KEY][note_key] == note
    assert runtime.annotation_revision == 1
    state[note_key] = " corrected\t🧾\r\n "
    _invoke(ui._commit_review_note, runtime)
    assert runtime.reviews_by_key[key] == FindingReview(
        key, ReviewDisposition.REVIEW_COMPLETE, state[note_key]
    )
    assert runtime.annotation_revision == 2
    assert note_key not in state.get(ui._REVIEW_NOTE_ERRORS_KEY, {})
    assert note_key not in state.get(ui._REVIEW_NOTE_DRAFTS_KEY, {})


@pytest.mark.parametrize("field", ("disposition", "note"))
@pytest.mark.parametrize(
    "stale",
    ("fingerprint", "generation", "unknown-key", "no-runtime", "no-result", "replaced-result"),
)
def test_stale_callbacks_are_safe_noops(monkeypatch, field, stale):
    runtime, state = _fake_active(monkeypatch)
    key = runtime.binding.exception_keys[0]
    fingerprint = runtime.binding.result_fingerprint
    generation = runtime.editor_generation
    if stale == "fingerprint":
        fingerprint = "f" * 64
    elif stale == "generation":
        generation += 1
    elif stale == "unknown-key":
        key = ("NOT-AN-EXCEPTION",)
    elif stale == "no-runtime":
        state.pop(ui._REVIEW_STATE_KEY)
    elif stale == "no-result":
        state.pop("completed")
    elif stale == "replaced-result":
        replacement = ui._adopt_review_binding(
            _binding(data_a=b"id,amount\nOTHER,1\n", data_b=b"id,amount\n")
        )
        state[ui._REVIEW_STATE_KEY] = replacement
        state[ui._REVIEW_BINDING_KEY] = replacement.binding
        state["completed"] = ("other", replacement.binding.result)
    widget_key = ui._review_widget_key(field, fingerprint, key, generation)
    state[widget_key] = (
        ReviewDisposition.REVIEW_COMPLETE if field == "disposition" else "stale note"
    )
    reviews = tuple(runtime.reviews_by_key.items())
    callback = ui._commit_review_disposition if field == "disposition" else ui._commit_review_note
    _invoke(callback, runtime, key, fingerprint=fingerprint, generation=generation)
    assert tuple(runtime.reviews_by_key.items()) == reviews
    assert runtime.annotation_revision == runtime.editor_generation == 0


@pytest.mark.parametrize("field", ("disposition", "note"))
@pytest.mark.parametrize(
    "corruption",
    (
        "extra",
        "reordered",
        "wrong-key",
        "wrong-value",
        "custom-disposition",
        "invalid-note",
        "negative-revision",
    ),
)
def test_active_callback_corruption_is_not_silently_repaired(monkeypatch, field, corruption):
    runtime, state = _fake_active(monkeypatch)
    state[_editor_key(runtime, field)] = (
        ReviewDisposition.REVIEW_COMPLETE if field == "disposition" else "candidate"
    )
    _corrupt_runtime(runtime, corruption)
    callback = ui._commit_review_disposition if field == "disposition" else ui._commit_review_note
    with pytest.raises(ReviewSessionError):
        _invoke(callback, runtime)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_progress_and_table_defaults_preserve_financial_cells_in_all_modes(mode):
    app = _run_app(mode=mode)
    runtime = _assert_active(app)
    _progress(app, unreviewed=2, follow_up=0, complete=0)
    assert any(header.value == "Review progress" for header in app.subheader)
    table = _exception_table(app).value
    original = finding_rows(runtime.binding.result.exceptions)
    assert list(table.columns) == [*original[0], "Review disposition"]
    assert table.drop(columns="Review disposition").to_dict("records") == original
    assert table["Review disposition"].tolist() == ["Unreviewed", "Unreviewed"]
    assert "Analyst note" not in table and "Review note" not in table
    filter_widget = _widget(app, "Review disposition")
    assert filter_widget.options == list(LABELS)
    assert filter_widget.value == list(DISPOSITIONS)
    assert not app.text_area
    _select(app)
    assert _widget(app, "Analyst disposition").options == list(LABELS)
    assert _widget(app, "Analyst disposition").value is ReviewDisposition.UNREVIEWED
    assert _widget(app, "Analyst note").value == ""
    assert _widget(app, "Analyst disposition").key == _editor_key(runtime, "disposition")
    assert _widget(app, "Analyst note").key == _editor_key(runtime, "note")
    assert any(header.value == "Analyst review" for header in app.subheader)
    assert any("Analyst review does not change" in item.value for item in app.caption)
    assert not any(button.label == "Save" for button in app.button)


def test_disposition_edits_each_transition_and_noop_keep_canonical_ownership():
    app = _run_app()
    runtime = _assert_active(app)
    binding = runtime.binding
    first, second = binding.exception_keys
    untouched = runtime.reviews_by_key[second]
    _select(app)
    _widget(app, "Analyst note").set_value(" exact\t🧾\n ").run()
    revision = 1
    old = ReviewDisposition.UNREVIEWED
    for new in (*DISPOSITIONS, *reversed(DISPOSITIONS), *DISPOSITIONS):
        _widget(app, "Analyst disposition").set_value(new).run()
        assert not app.exception
        revision += int(old is not new)
        assert runtime.annotation_revision == revision
        assert runtime.reviews_by_key[first] == FindingReview(first, new, " exact\t🧾\n ")
        assert runtime.reviews_by_key[second] is untouched
        assert runtime.editor_generation == 0 and runtime.binding is binding
        assert tuple(runtime.reviews_by_key) == binding.exception_keys
        counts = [int(new is disposition) for disposition in DISPOSITIONS]
        _progress(app, unreviewed=counts[0] + 1, follow_up=counts[1], complete=counts[2])
        old = new
    app.run()
    assert runtime.annotation_revision == revision
    assert app.session_state["completed"][1] is binding.result


def test_note_editor_commits_exact_valid_text_including_empty_and_formula_text():
    app = _run_app()
    runtime = _assert_active(app)
    key = runtime.binding.exception_keys[0]
    _select(app)
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.NEEDS_FOLLOW_UP).run()
    notes = (
        "ordinary",
        " leading\ttrailing ",
        "LF\nCR\rCRLF\r\n",
        '🧾 e\u0301 "quote" \\',
        " \t=SUM(A1:A2)",
        "",
    )
    for revision, note in enumerate(notes, 2):
        _widget(app, "Analyst note").set_value(note).run()
        assert not app.exception and not app.error
        assert _widget(app, "Analyst note").value == note
        assert runtime.reviews_by_key[key] == FindingReview(
            key, ReviewDisposition.NEEDS_FOLLOW_UP, note
        )
        assert runtime.annotation_revision == revision
    _widget(app, "Analyst note").set_value("").run()
    assert runtime.annotation_revision == 1 + len(notes)


@pytest.mark.parametrize(
    "draft",
    [
        pytest.param("bad\0note", id="nul"),
        pytest.param("bad\x01note", id="control"),
        pytest.param("🧾" * 4_097, id="utf8-too-long"),
    ],
)
def test_invalid_editor_draft_survives_navigation_and_disposition_then_correction(draft):
    app = _run_app()
    runtime = _assert_active(app)
    first = runtime.binding.exception_keys[0]
    _select(app)
    _widget(app, "Analyst note").set_value("committed note").run()
    committed = runtime.reviews_by_key[first]
    before_json = export_review_session(runtime.snapshot())
    before_csv = export_reviewed_exceptions_csv(runtime.binding, runtime.snapshot())
    note_key = _widget(app, "Analyst note").key
    _widget(app, "Analyst note").set_value(draft).run()
    assert not app.exception and app.error
    assert runtime.reviews_by_key[first] is committed and runtime.annotation_revision == 1
    assert _widget(app, "Analyst note").value == draft
    assert all(len(error.value) < 350 and draft not in error.value for error in app.error)
    assert export_review_session(runtime.snapshot()) == before_json
    assert export_reviewed_exceptions_csv(runtime.binding, runtime.snapshot()) == before_csv
    _select(app, 1)
    assert _widget(app, "Analyst note").value == ""
    _select(app, 0)
    assert _widget(app, "Analyst note").key == note_key
    assert _widget(app, "Analyst note").value == draft and app.error
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    assert runtime.reviews_by_key[first] == FindingReview(
        first, ReviewDisposition.REVIEW_COMPLETE, "committed note"
    )
    assert runtime.annotation_revision == 2 and _widget(app, "Analyst note").value == draft
    _widget(app, "Analyst note").set_value("corrected\t🧾").run()
    assert not app.exception and not app.error
    assert runtime.reviews_by_key[first] == FindingReview(
        first, ReviewDisposition.REVIEW_COMPLETE, "corrected\t🧾"
    )
    assert runtime.annotation_revision == 3
    _select(app, 1)
    _select(app, 0)
    assert _widget(app, "Analyst note").value == "corrected\t🧾"


def test_progress_filters_and_navigation_preserve_full_authoritative_population(
    monkeypatch, review_downloads
):
    calls = []
    original = tallydiff.bind_review_result

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(tallydiff, "bind_review_result", tracked)
    app = _secondary_app()
    runtime = _assert_active(app)
    result = runtime.binding.result
    first = runtime.binding.exception_keys[0]
    _select(app)
    _widget(app, "Analyst note").set_value("preserved note").run()
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.NEEDS_FOLLOW_UP).run()
    committed = runtime.reviews_by_key[first]
    revision = runtime.annotation_revision
    before_csv = review_downloads["Download exception report"]
    before_profile = review_downloads["Download mapping profile"]
    before_session = export_review_session(runtime.snapshot())
    changes = (
        ("Search matching keys", "exact"),
        ("Search matching keys", ""),
        ("Exception categories", [FindingCategory.EXACT_MATCH]),
        ("Exception categories", list(_widget(app, "Exception categories").value)),
        ("Review disposition", [ReviewDisposition.UNREVIEWED]),
        ("Review disposition", list(DISPOSITIONS)),
        ("Minimum absolute delta", "0.01"),
        ("Minimum absolute delta", "0"),
        ("Exception sort order", "absolute_delta_desc"),
        ("Exception sort order", "matching_key"),
    )
    for label, value in changes:
        _widget(app, label).set_value(value).run()
        assert not app.exception
        _progress(app, unreviewed=5, follow_up=1, complete=0)
        assert runtime.reviews_by_key[first] is committed
        assert runtime.annotation_revision == revision
        assert app.session_state["completed"][1] is result
        assert export_review_session(runtime.snapshot()) == before_session
        assert review_downloads["Download exception report"] == before_csv
        assert review_downloads["Download mapping profile"] == before_profile
    for value in (True, False):
        _secondary_checkbox(app).set_value(value).run()
        _progress(app, unreviewed=5, follow_up=1, complete=0)
        assert runtime.annotation_revision == revision
    _select(app, 1)
    app.run()
    _progress(app, unreviewed=5, follow_up=1, complete=0)
    assert runtime.annotation_revision == revision and len(calls) == 1
    app.button(key="run").click().run()
    assert len(calls) == 2
    adopted = _assert_active(app)
    assert adopted.binding is not runtime.binding and adopted.binding.result is not result
    assert adopted.binding.result_fingerprint == runtime.binding.result_fingerprint
    assert adopted.reviews_by_key[first] is committed
    assert adopted.annotation_revision == revision


def test_invalid_draft_survives_filter_navigation_and_is_cleared_with_the_result():
    app = _run_app()
    runtime = _assert_active(app)
    first = runtime.binding.exception_keys[0]
    _select(app)
    _widget(app, "Analyst note").set_value("authoritative note").run()
    _widget(app, "Analyst note").set_value("uncommitted\0draft").run()
    note_key = _widget(app, "Analyst note").key
    _widget(app, "Search matching keys").set_value("missing").run()
    assert not app.exception and _exception_table(app) is None
    assert runtime.reviews_by_key[first].note == "authoritative note"
    assert runtime.annotation_revision == 1
    _widget(app, "Search matching keys").set_value("").run()
    _select(app)
    assert _widget(app, "Analyst note").value == "uncommitted\0draft" and app.error
    assert _widget(app, "Analyst note").key == note_key
    app.text_input(key="amount_tolerance").set_value("0.02").run()
    assert not app.exception and "completed" not in app.session_state
    assert ui._REVIEW_STATE_KEY not in app.session_state
    assert ui._REVIEW_NOTE_DRAFTS_KEY not in app.session_state
    assert ui._REVIEW_NOTE_ERRORS_KEY not in app.session_state
    assert note_key not in app.session_state


def test_renderer_rejects_equivalent_result_object_without_binding_ownership():
    runtime = ui._adopt_review_binding(_binding())
    rebuilt = _binding().result
    assert rebuilt == runtime.binding.result and rebuilt is not runtime.binding.result
    with pytest.raises(ReviewSessionError):
        ui._show_results(
            rebuilt,
            ColumnMapping((("id", "id"),), "amount", "amount"),
            "configuration",
            "file_a.csv",
            "file_b.csv",
            review_state=runtime,
        )


@pytest.mark.parametrize(
    "selection",
    [
        pytest.param([ReviewDisposition.UNREVIEWED], id="unreviewed"),
        pytest.param([ReviewDisposition.NEEDS_FOLLOW_UP], id="followup"),
        pytest.param([ReviewDisposition.REVIEW_COMPLETE], id="complete"),
        pytest.param(
            [ReviewDisposition.UNREVIEWED, ReviewDisposition.REVIEW_COMPLETE], id="combination"
        ),
        pytest.param([], id="empty"),
    ],
)
def test_disposition_filter_is_independent_and_keeps_global_counts(selection):
    app = _financial_review_app()
    runtime = _assert_active(app)
    for index, disposition in enumerate(DISPOSITIONS):
        key = runtime.binding.exception_keys[index]
        runtime.reviews_by_key[key] = FindingReview(key, disposition)
    app.run()
    _widget(app, "Review disposition").set_value(selection).run()
    expected = [
        finding.key[0]
        for finding in runtime.binding.result.exceptions
        if runtime.reviews_by_key[finding.key].disposition in selection
    ]
    table = _exception_table(app)
    assert (table.value["Matching key"].tolist() if table else []) == expected
    _progress(app, unreviewed=5, follow_up=1, complete=1)
    assert any(
        caption.value == f"Showing {len(expected)} of 7 exception groups."
        for caption in app.caption
    )
    if not expected:
        assert any("No exception groups match" in item.value for item in app.info)
    _review_widget(app, "Exception categories").set_value([FindingCategory.A_ONLY]).run()
    expected = [
        finding.key[0]
        for finding in runtime.binding.result.exceptions
        if finding.category is FindingCategory.A_ONLY
        and runtime.reviews_by_key[finding.key].disposition in selection
    ]
    table = _exception_table(app)
    assert (table.value["Matching key"].tolist() if table else []) == expected
    _progress(app, unreviewed=5, follow_up=1, complete=1)


@pytest.mark.parametrize("minimum", ["invalid", "NaN", "Infinity", "-1"])
def test_invalid_minimum_keeps_progress_annotations_and_disposition_filter(minimum):
    app = _run_app()
    runtime = _assert_active(app)
    _select(app)
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.NEEDS_FOLLOW_UP).run()
    _widget(app, "Review disposition").set_value([ReviewDisposition.NEEDS_FOLLOW_UP]).run()
    before = export_review_session(runtime.snapshot())
    _widget(app, "Minimum absolute delta").set_value(minimum).run()
    assert not app.exception and app.error and _exception_table(app) is None
    _progress(app, unreviewed=1, follow_up=1, complete=0)
    assert _widget(app, "Review disposition").value == [ReviewDisposition.NEEDS_FOLLOW_UP]
    assert runtime.annotation_revision == 1 and export_review_session(runtime.snapshot()) == before
    _widget(app, "Minimum absolute delta").set_value("0").run()
    assert not app.exception and not app.error
    assert _exception_table(app).value["Matching key"].tolist() == ["A"]


def test_disposition_removal_changes_table_identity_without_selecting_successor():
    app = _run_app()
    runtime = _assert_active(app)
    first, second = runtime.binding.exception_keys
    _widget(app, "Review disposition").set_value([ReviewDisposition.UNREVIEWED]).run()
    old_table = _select(app)
    old_editor = _widget(app, "Analyst disposition").key
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    assert not app.exception
    assert runtime.reviews_by_key[first].disposition is ReviewDisposition.REVIEW_COMPLETE
    assert runtime.reviews_by_key[second].disposition is ReviewDisposition.UNREVIEWED
    assert _exception_table(app).key != old_table
    assert _exception_table(app).value["Matching key"].tolist() == ["B"]
    assert not any(text.value.startswith("Selected key:") for text in app.text)
    assert not app.text_area
    assert not any(widget.key == old_editor for widget in app.selectbox)
    assert runtime.annotation_revision == 1
    _select(app)
    assert _widget(app, "Analyst disposition").value is ReviewDisposition.UNREVIEWED
    assert any(text.value == "Selected key: B" for text in app.text)
    assert runtime.annotation_revision == 1


def test_note_and_disposition_remaining_visible_preserve_table_identity_and_selection():
    app = _run_app()
    runtime = _assert_active(app)
    key = _select(app)
    _widget(app, "Analyst note").set_value("table identity excludes note contents").run()
    assert _exception_table(app).key == key
    assert any(text.value == "Selected key: A" for text in app.text)
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    assert _exception_table(app).key == key
    assert any(text.value == "Selected key: A" for text in app.text)
    assert runtime.annotation_revision == 2
    _widget(app, "Exception sort order").set_value("absolute_delta_desc").run()
    assert _exception_table(app).key != key
    assert not any(text.value.startswith("Selected key:") for text in app.text)
    assert runtime.annotation_revision == 2


@pytest.mark.parametrize("index", [-1, 2, 1_000_000])
def test_out_of_bounds_selection_never_crashes_or_selects_a_different_finding(index):
    app = _run_app()
    runtime = _assert_active(app)
    before = export_review_session(runtime.snapshot())
    _select(app, index)
    assert not any(text.value.startswith("Selected key:") for text in app.text)
    assert not app.text_area and runtime.annotation_revision == 0
    assert export_review_session(runtime.snapshot()) == before


def test_colliding_display_keys_have_distinct_editors_and_canonical_annotations():
    app = _uploaded_app(b"id,other,amount\nA,B / C,1\nA / B,C,2\n", b"id,other,amount\n")
    app.button(key="add_key").click().run()
    for side in ("a", "b"):
        app.selectbox(key=f"map_key_{side}_1").set_value("other")
    _simple_mapping(app)
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    first, second = runtime.binding.exception_keys
    assert first != second and " / ".join(first) == " / ".join(second)
    assert _exception_table(app).value["Matching key"].tolist() == ["A / B / C"] * 2
    _select(app, 0)
    first_editor = _widget(app, "Analyst note").key
    _widget(app, "Analyst note").set_value("first canonical key").run()
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    _select(app, 1)
    assert _widget(app, "Analyst note").key != first_editor
    assert _widget(app, "Analyst note").value == ""
    _widget(app, "Analyst note").set_value("second canonical key").run()
    assert runtime.reviews_by_key[first] == FindingReview(
        first, ReviewDisposition.REVIEW_COMPLETE, "first canonical key"
    )
    assert runtime.reviews_by_key[second] == FindingReview(
        second, ReviewDisposition.UNREVIEWED, "second canonical key"
    )
    _select(app, 0)
    assert _widget(app, "Analyst note").value == "first canonical key"
    assert runtime.annotation_revision == 3


def test_inferred_exact_completion_preserves_truth_evidence_and_wording(review_downloads):
    app = _uploaded_app(b"id,amount\nINFERRED,3\n", b"id,amount\nINFERRED,1\nINFERRED,2\n")
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.BOUNDED_ONE_TO_MANY).run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    result = runtime.binding.result
    finding = result.exceptions[0]
    evidence = finding.correspondence_analysis
    original = review_downloads["Download exception report"]
    _select(app)
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    assert finding.category is FindingCategory.EXACT_MATCH
    assert finding.has_inferred_correspondence and finding.is_exception
    assert result.exceptions == (finding,) and finding.correspondence_analysis is evidence
    assert _exception_table(app).value["Category"].tolist() == ["Exact match"]
    assert _exception_table(app).value["Review disposition"].tolist() == ["Review complete"]
    assert any(
        header.value == "**Inferred exact correspondence — reconciliation exception**"
        for header in app.markdown
    )
    assert not any("review required" in header.value.lower() for header in app.markdown)
    assert any(
        "reconciliation exception" in caption.value and "Inferred correspondence" in caption.value
        for caption in app.caption
    )
    assert review_downloads["Download exception report"] == original
    _progress(app, unreviewed=0, follow_up=0, complete=1)
    _widget(app, "Review disposition").set_value([ReviewDisposition.REVIEW_COMPLETE]).run()
    assert _exception_table(app).value["Matching key"].tolist() == ["INFERRED"]
    _widget(app, "Exception categories").set_value([FindingCategory.EXACT_MATCH]).run()
    assert len(_exception_table(app).value) == 1


@pytest.mark.parametrize(
    "key,category",
    [("EXACT", FindingCategory.EXACT_MATCH), ("TOLERATED", FindingCategory.WITHIN_TOLERANCE)],
)
def test_secondary_only_exceptions_can_complete_and_filter_without_reclassification(key, category):
    app = _secondary_app()
    runtime = _assert_active(app)
    finding = next(
        finding for finding in runtime.binding.result.exceptions if finding.key == (key,)
    )
    _widget(app, "Search matching keys").set_value(key).run()
    _select(app)
    _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    _widget(app, "Review disposition").set_value([ReviewDisposition.REVIEW_COMPLETE]).run()
    _widget(app, "Exception categories").set_value([category]).run()
    _secondary_checkbox(app).set_value(True).run()
    assert not app.exception
    assert finding.category is category and finding.is_exception
    assert runtime.reviews_by_key[(key,)].disposition is ReviewDisposition.REVIEW_COMPLETE
    assert _exception_table(app).value["Matching key"].tolist() == [key]
    assert finding in runtime.binding.result.exceptions
    assert finding not in runtime.binding.result.tolerated_findings


def test_tolerated_and_grouped_exact_tables_have_no_review_column_or_editor():
    app = _financial_review_app(ReconciliationMode.GROUPED_BY_KEY, tolerance="0.001")
    runtime = _assert_active(app)
    generic = [frame for frame in app.dataframe if frame.key.startswith(("tolerated_", "exact_"))]
    assert len(generic) == 2
    for frame in generic:
        assert "Review disposition" not in frame.value
        app.session_state[frame.key] = {"selection": {"rows": [0], "columns": [], "cells": []}}
        app.run()
        assert not app.exception
        assert not app.text_area
        assert not any(widget.label == "Analyst disposition" for widget in app.selectbox)
        app.session_state[frame.key] = {"selection": {"rows": [], "columns": [], "cells": []}}
        app.run()
    assert runtime.annotation_revision == 0


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("kind", ("empty", "exact", "tolerated"))
def test_zero_exceptions_have_no_review_ui_but_valid_empty_runtime(mode, kind):
    data_a = b"id,amount\n" if kind == "empty" else b"id,amount\nCLEAN,1\n"
    data_b = data_a if kind != "tolerated" else b"id,amount\nCLEAN,1.01\n"
    app = _uploaded_app(data_a, data_b)
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode)
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    assert runtime.snapshot().reviews == ()
    assert not any(metric.label in PROGRESS_LABELS for metric in app.metric)
    assert not any(
        header.value in ("Review progress", "Analyst review") for header in app.subheader
    )
    assert not any(
        widget.label in ("Review disposition", "Analyst disposition", "Analyst note")
        for widget in (*app.multiselect, *app.selectbox, *app.text_area)
    )


def test_many_edits_leave_complete_result_binding_source_and_original_csv_unchanged(
    review_downloads,
):
    app = _uploaded_app(
        b"id,amount,department\nINFERRED,3,Sales\nSECONDARY,5,Sales\nTOLERATED,2,Sales\nCLEAN,4,Sales\n",
        b"id,amount,department\nINFERRED,1,Sales\nINFERRED,2,Support\nSECONDARY,5,Support\nTOLERATED,1.99,Sales\nCLEAN,4,Sales\n",
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.BOUNDED_ONE_TO_MANY)
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    binding = runtime.binding
    result = binding.result
    findings, exceptions, tolerated = result.findings, result.exceptions, result.tolerated_findings
    evidence = tuple(
        (
            finding.field_comparisons,
            finding.correspondence_analysis,
            finding.amount_a,
            finding.amount_b,
            finding.delta,
            finding.is_exception,
            tuple(
                (row.source, row.source_row, row.key, row.amount, dict(row.raw_fields))
                for row in (*finding.rows_a, *finding.rows_b)
            ),
        )
        for finding in findings
    )
    totals = result.total_a, result.total_b, result.control_difference
    fingerprint = binding.result_fingerprint
    original = export_exceptions_csv(result)
    old_session = runtime.snapshot()
    old_json = export_review_session(old_session)
    for index in range(len(exceptions)):
        _select(app, index)
        _widget(app, "Analyst note").set_value(f" note {index}\t🧾\r\n=1+2 ").run()
        _widget(app, "Analyst disposition").set_value(ReviewDisposition.REVIEW_COMPLETE).run()
    assert not app.exception
    assert runtime.binding is binding and app.session_state["completed"][1] is result
    assert result.findings is findings
    assert result.exceptions == exceptions and result.tolerated_findings == tolerated
    assert (
        tuple(
            (
                finding.field_comparisons,
                finding.correspondence_analysis,
                finding.amount_a,
                finding.amount_b,
                finding.delta,
                finding.is_exception,
                tuple(
                    (row.source, row.source_row, row.key, row.amount, dict(row.raw_fields))
                    for row in (*finding.rows_a, *finding.rows_b)
                ),
            )
            for finding in result.findings
        )
        == evidence
    )
    assert (result.total_a, result.total_b, result.control_difference) == totals
    assert binding.result_fingerprint == fingerprint
    assert (
        export_exceptions_csv(result) == original == review_downloads["Download exception report"]
    )
    assert export_review_session(old_session) == old_json
    new_json = export_review_session(runtime.snapshot())
    assert new_json != old_json and new_json == export_review_session(runtime.snapshot())
    reviewed = _rows(export_reviewed_exceptions_csv(binding, runtime.snapshot()))
    plain = _rows(original)
    assert [row[:-2] for row in reviewed] == plain
    assert all(
        row[-2] == "Review complete" and row[-1].startswith("Note:  note ") for row in reviewed[1:]
    )
    assert runtime.annotation_revision == 2 * len(exceptions)


def test_review_persistence_is_explicit_and_only_original_download_precedes_prepare():
    app = _run_app()
    _select(app)
    controls = {
        "Review session JSON",
        "Apply review session",
        "Prepare review downloads",
    }
    prohibited = {
        "Download review session",
        "Download reviewed exception report",
        "Save",
        "Autosave",
        "Merge review session",
        "Download reviewed XLSX",
    }
    labels = {widget.label for widget in (*app.file_uploader, *app.button, *app.download_button)}
    assert controls.issubset(labels)
    assert not labels.intersection(prohibited)
    assert next(widget for widget in app.button if widget.label == "Apply review session").disabled
    assert len(_exception_downloads(app)) == 1
    assert _exception_downloads(app)[0].label == "Download exception report"


def test_direct_callbacks_edit_canonical_display_collision_without_alias(monkeypatch):
    keys = (("A", "B / C"), ("A / B", "C"))
    result = reconcile(
        tuple(SourceRecord(Source.A, row, key, Decimal("1")) for row, key in enumerate(keys, 2)), ()
    )
    binding = bind_review_result(
        result,
        data_a=b"A",
        data_b=b"B",
        mapping=ColumnMapping((("one", "one"), ("two", "two")), "amount", "amount"),
        format_a="csv",
        format_b="csv",
    )
    runtime = ui._adopt_review_binding(binding)
    state = {
        "completed": ("configuration", result),
        ui._REVIEW_STATE_KEY: runtime,
        ui._REVIEW_BINDING_KEY: binding,
    }
    monkeypatch.setattr(ui.st, "session_state", state)
    state[_editor_key(runtime, "note", keys[1])] = "second tuple"
    _invoke(ui._commit_review_note, runtime, keys[1])
    assert runtime.reviews_by_key[keys[0]] == FindingReview(keys[0], ReviewDisposition.UNREVIEWED)
    assert runtime.reviews_by_key[keys[1]] == FindingReview(
        keys[1], ReviewDisposition.UNREVIEWED, "second tuple"
    )
    assert runtime.annotation_revision == 1
