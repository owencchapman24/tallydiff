"""Private review ownership follows completed Streamlit reconciliation lifecycles."""

from dataclasses import fields, replace
from decimal import Decimal
from importlib.metadata import version

import pytest
from test_app import (
    _exception_downloads,
    _exception_table,
    _review_widget,
    _simple_mapping,
    _uploaded_app,
    _worksheet,
)
from test_app import review_downloads as review_downloads

import tallydiff
import tallydiff.app as ui
from tallydiff import (
    ColumnMapping,
    FindingCategory,
    FindingReview,
    IngestionError,
    KeyNormalizationError,
    NormalizationCollisionError,
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
    export_review_session,
    ingest_csv,
    reconcile,
)

DATA_A = (
    b"id,other,amount,alternate,department,currency\n"
    b"A,one,3,3,Sales,USD\nB,two,5,5,Sales,USD\n"
    b"CLEAN,three,2,2,Sales,USD\nTOLERATED,four,2,2,Sales,USD\n"
)
DATA_B = (
    DATA_A.replace(b"A,one,3,3", b"A,one,1,1")
    .replace(b"B,two,5,5", b"B,two,3,3")
    .replace(b"TOLERATED,four,2,2", b"TOLERATED,four,1.99,1.99")
)
MAPPING = ColumnMapping((("id", "id"),), "amount", "amount")
MODE = ReconciliationMode.BOUNDED_ONE_TO_MANY


def _binding(data_a=DATA_A, data_b=DATA_B, **kwargs):
    result = reconcile(
        ingest_csv(data_a.decode(), source=Source.A, key_columns=("id",), amount_column="amount"),
        ingest_csv(data_b.decode(), source=Source.B, key_columns=("id",), amount_column="amount"),
        amount_tolerance=Decimal("0.01"),
        **kwargs,
    )
    return bind_review_result(
        result, data_a=data_a, data_b=data_b, mapping=MAPPING, format_a="csv", format_b="csv"
    )


def _assert_fresh(runtime):
    assert tuple(runtime.reviews_by_key) == runtime.binding.exception_keys
    assert runtime.annotation_revision == runtime.editor_generation == 0
    snapshot = runtime.snapshot()
    assert isinstance(snapshot, ReviewSession)
    assert snapshot.binding is runtime.binding
    assert (
        tuple(review.matching_key for review in snapshot.reviews) == runtime.binding.exception_keys
    )
    for key, review in runtime.reviews_by_key.items():
        assert isinstance(review, FindingReview)
        assert review.matching_key == key
        assert review.disposition is ReviewDisposition.UNREVIEWED and review.note == ""
    return snapshot


def _assert_active(app):
    assert not app.exception
    completed = app.session_state["completed"]
    assert isinstance(completed, tuple) and len(completed) == 2
    identity, result = completed
    assert isinstance(identity, str) and isinstance(result, ReconciliationResult)
    runtime = app.session_state[ui._REVIEW_STATE_KEY]
    assert isinstance(runtime.binding, ReviewBinding)
    assert runtime.binding is app.session_state[ui._REVIEW_BINDING_KEY]
    assert runtime.binding.result is result
    assert runtime.binding.exception_keys == tuple(finding.key for finding in result.exceptions)
    assert tuple(runtime.reviews_by_key) == runtime.binding.exception_keys
    assert runtime.snapshot().binding is runtime.binding
    return runtime


def _seed(runtime):
    key = runtime.binding.exception_keys[0]
    review = FindingReview(key, ReviewDisposition.NEEDS_FOLLOW_UP, "Original 🧾 note\t\r\n=1+2")
    runtime.reviews_by_key[key] = review
    runtime.annotation_revision = 7
    runtime.editor_generation = 3
    return review


def _run_app(*, mode=ReconciliationMode.UNIQUE, comparisons=False, two_keys=False):
    app = _uploaded_app(DATA_A, DATA_B)
    if two_keys:
        app.button(key="add_key").click().run()
        for side in ("a", "b"):
            app.selectbox(key=f"map_key_{side}_1").set_value("other")
    _simple_mapping(app)
    if comparisons:
        app.button(key="add_comparison").click().run()
        for side in ("a", "b"):
            app.selectbox(key=f"map_comparison_{side}_0").set_value("department")
    app.radio(key="reconciliation_mode").set_value(mode)
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    _assert_fresh(_assert_active(app))
    return app


def _assert_cleared(app):
    assert "completed" not in app.session_state
    assert ui._REVIEW_STATE_KEY not in app.session_state
    assert ui._REVIEW_BINDING_KEY not in app.session_state
    assert not app.metric and not _exception_downloads(app)


def test_private_holder_shape_initial_population_and_immutable_snapshot():
    binding = _binding()
    runtime = ui._adopt_review_binding(binding)
    assert [field.name for field in fields(runtime)] == [
        "binding",
        "reviews_by_key",
        "annotation_revision",
        "editor_generation",
    ]
    snapshot = _assert_fresh(runtime)
    review = _seed(runtime)
    assert snapshot.reviews[0].disposition is ReviewDisposition.UNREVIEWED
    updated = runtime.snapshot()
    assert updated.reviews[0] is review
    assert updated.reviews is not runtime.reviews_by_key
    assert tuple(runtime.reviews_by_key) == (("A",), ("B",))


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("data", [b"id,amount\n", b"id,amount\nCLEAN,1\n"], ids=["empty", "clean"])
def test_helper_zero_exception_bindings_have_empty_valid_snapshots(mode, data):
    binding = _binding(data, data, mode=mode)
    runtime = ui._adopt_review_binding(binding)
    snapshot = _assert_fresh(runtime)
    assert runtime.reviews_by_key == {} and snapshot.reviews == ()


CORRUPTIONS = [
    "missing",
    "extra",
    "reordered",
    "wrong-key",
    "wrong-value",
    "wrong-dict",
    "wrong-binding",
    "custom-disposition",
    "invalid-note",
    "negative-revision",
    "boolean-generation",
]


def _corrupt_runtime(runtime, corruption):
    first, second = runtime.binding.exception_keys
    if corruption == "missing":
        runtime.reviews_by_key.pop(first)
    elif corruption == "extra":
        runtime.reviews_by_key[("EXTRA",)] = FindingReview(("EXTRA",), ReviewDisposition.UNREVIEWED)
    elif corruption == "reordered":
        runtime.reviews_by_key = dict(reversed(runtime.reviews_by_key.items()))
    elif corruption == "wrong-key":
        runtime.reviews_by_key[first] = FindingReview(second, ReviewDisposition.REVIEW_COMPLETE)
    elif corruption == "wrong-value":
        runtime.reviews_by_key[first] = "annotation"
    elif corruption == "wrong-dict":
        runtime.reviews_by_key = tuple(runtime.reviews_by_key.values())
    elif corruption == "wrong-binding":
        runtime.binding = None
    elif corruption == "custom-disposition":
        object.__setattr__(runtime.reviews_by_key[first], "disposition", "review_complete")
    elif corruption == "invalid-note":
        object.__setattr__(runtime.reviews_by_key[first], "note", "invalid\0note")
    elif corruption == "negative-revision":
        runtime.annotation_revision = -1
    elif corruption == "boolean-generation":
        runtime.editor_generation = True
    else:
        raise AssertionError(corruption)


@pytest.mark.parametrize("corruption", CORRUPTIONS)
def test_snapshot_rejects_corrupted_runtime_and_adoption_discards_it(corruption):
    binding = _binding()
    runtime = ui._adopt_review_binding(binding)
    _seed(runtime)
    _corrupt_runtime(runtime, corruption)
    with pytest.raises(ReviewSessionError):
        runtime.snapshot()
    adopted = ui._adopt_review_binding(_binding(), runtime)
    _assert_fresh(adopted)


def test_snapshot_delegates_domain_validation_to_public_review_session(monkeypatch):
    runtime = ui._adopt_review_binding(_binding())
    seen = []
    original = ui.ReviewSession

    def tracked(binding, reviews):
        seen.append((binding, reviews))
        return original(binding, reviews)

    monkeypatch.setattr(ui, "ReviewSession", tracked)
    snapshot = runtime.snapshot()
    assert seen == [(runtime.binding, snapshot.reviews)]


@pytest.mark.parametrize("compatible", [True, False], ids=["equivalent", "different-fingerprint"])
def test_adoption_uses_new_binding_and_preserves_only_verified_complete_annotations(compatible):
    runtime = ui._adopt_review_binding(_binding())
    review = _seed(runtime)
    new_binding = _binding(data_a=DATA_A if compatible else DATA_A.replace(b"A,one,3", b"A,one,4"))
    adopted = ui._adopt_review_binding(new_binding, runtime)
    assert adopted is not runtime
    assert adopted.binding is new_binding and new_binding is not runtime.binding
    assert new_binding.result is not runtime.binding.result
    if compatible:
        assert adopted.reviews_by_key is not runtime.reviews_by_key
        assert all(
            adopted.reviews_by_key[key] is prior for key, prior in runtime.reviews_by_key.items()
        )
        assert adopted.reviews_by_key[review.matching_key] is review
        assert adopted.annotation_revision == 7 and adopted.editor_generation == 3
    else:
        _assert_fresh(adopted)


def test_adoption_checks_population_independently_of_fingerprint():
    old = ui._adopt_review_binding(_binding())
    _seed(old)
    different = _binding(DATA_A.replace(b"A,one", b"Z,one"), DATA_B.replace(b"A,one", b"Z,one"))
    same_digest = replace(different, result_fingerprint=old.binding.result_fingerprint)
    assert same_digest.exception_keys != old.binding.exception_keys
    _assert_fresh(ui._adopt_review_binding(same_digest, old))


def test_runtime_identity_uses_canonical_tuples_when_display_keys_collide():
    keys = (("A", "B / C"), ("A / B", "C"))
    rows = tuple(
        SourceRecord(Source.A, index, key, Decimal("1")) for index, key in enumerate(keys, 2)
    )
    result = reconcile(rows, ())
    binding = bind_review_result(
        result,
        data_a=b"A",
        data_b=b"B",
        mapping=ColumnMapping((("one", "one"), ("two", "two")), "amount", "amount"),
        format_a="csv",
        format_b="csv",
    )
    runtime = ui._adopt_review_binding(binding)
    review = _seed(runtime)
    snapshot = runtime.snapshot()
    assert " / ".join(keys[0]) == " / ".join(keys[1])
    assert snapshot.reviews[0] is review
    assert snapshot.reviews[1].disposition is ReviewDisposition.UNREVIEWED


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_successful_run_creates_binding_and_complete_runtime_for_all_modes(mode):
    app = _uploaded_app(
        b"id,amount\nINFERRED,3\nORDINARY,5\nCLEAN,2\n",
        b"id,amount\nINFERRED,1\nINFERRED,2\nORDINARY,4\nCLEAN,2\n",
    )
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    _assert_fresh(runtime)
    if mode is MODE:
        finding = runtime.binding.result.exceptions[0]
        assert (
            finding.category is FindingCategory.EXACT_MATCH and finding.has_inferred_correspondence
        )
        assert finding.key in runtime.reviews_by_key


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_exact_same_run_transfers_annotations_to_new_authoritative_result_and_binding(mode):
    app = _run_app(mode=mode)
    old = _assert_active(app)
    annotation = _seed(old)
    previous_session = export_review_session(old.snapshot())
    app.button(key="run").click().run()
    new = _assert_active(app)
    assert new is not old and new.binding is not old.binding
    assert new.binding.result is not old.binding.result
    assert new.binding.result_fingerprint == old.binding.result_fingerprint
    assert new.binding.exception_keys == old.binding.exception_keys
    assert new.reviews_by_key[annotation.matching_key] is annotation
    assert new.annotation_revision == old.annotation_revision == 7
    assert new.editor_generation == old.editor_generation == 3
    assert export_review_session(new.snapshot()) == previous_session


def test_ordinary_rerun_reuses_runtime_and_binding_without_rebinding(monkeypatch):
    calls = []
    original = tallydiff.bind_review_result

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(tallydiff, "bind_review_result", tracked)
    app = _run_app()
    runtime = _assert_active(app)
    annotation = _seed(runtime)
    app.run()
    assert _assert_active(app) is runtime
    assert runtime.reviews_by_key[annotation.matching_key] is annotation
    assert runtime.annotation_revision == 7 and runtime.editor_generation == 3
    assert len(calls) == 1


def test_review_state_is_app_only_and_public_contracts_and_release_version_match():
    assert "_ReviewRuntimeState" not in tallydiff.__all__
    assert not hasattr(tallydiff, "_ReviewRuntimeState")
    for model in (ReconciliationResult, ReconciliationFinding):
        assert not any(
            "review" in field.name or "annotation" in field.name for field in fields(model)
        )
    assert version("tallydiff") == "0.8.0"


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("empty", [False, True], ids=["clean", "headers-only"])
def test_zero_exception_app_runs_still_own_empty_review_state(mode, empty):
    data = b"id,amount\n" if empty else b"id,amount\nCLEAN,1\n"
    app = _uploaded_app(data, data)
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    assert _assert_fresh(runtime).reviews == ()
    assert runtime.reviews_by_key == {}
    assert not _exception_downloads(app)


def test_secondary_only_exact_and_tolerated_exceptions_are_owned():
    app = _uploaded_app(
        b"id,amount,department\nEXACT,3,Sales\nWITHIN,3,Sales\n",
        b"id,amount,department\nEXACT,3,Support\nWITHIN,2.99,Support\n",
    )
    _simple_mapping(app)
    app.button(key="add_comparison").click().run()
    for side in ("a", "b"):
        app.selectbox(key=f"map_comparison_{side}_0").set_value("department")
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    _assert_fresh(runtime)
    assert runtime.binding.exception_keys == (("EXACT",), ("WITHIN",))
    assert [finding.category for finding in runtime.binding.result.exceptions] == [
        FindingCategory.EXACT_MATCH,
        FindingCategory.WITHIN_TOLERANCE,
    ]
    assert runtime.binding.result.tolerated_findings == ()


@pytest.mark.parametrize(
    "formats", [("csv", "csv"), ("csv", "xlsx"), ("xlsx", "csv"), ("xlsx", "xlsx")]
)
def test_binding_receives_exact_successful_run_inputs_and_explicit_formats(
    monkeypatch, xlsx_bytes, formats
):
    observed = []
    ingestion_calls = []
    original = tallydiff.bind_review_result

    def tracked(*args, **kwargs):
        observed.append((args[0], kwargs))
        return original(*args, **kwargs)

    def ingestion_tracker(implementation):
        def wrapped(*args, **kwargs):
            ingestion_calls.append(kwargs["source"])
            return implementation(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(tallydiff, "bind_review_result", tracked)
    monkeypatch.setattr(tallydiff, "ingest_csv", ingestion_tracker(tallydiff.ingest_csv))
    monkeypatch.setattr(tallydiff, "ingest_xlsx", ingestion_tracker(tallydiff.ingest_xlsx))
    data = []
    sheets = []
    for index, input_format in enumerate(formats):
        if input_format == "csv":
            data.append(b"id,amount\nK,3\n" if index == 0 else b"id,amount\nK,2\n")
            sheets.append(None)
        else:
            sheet = " Ledger é " if index == 0 else "财务"
            data.append(xlsx_bytes({sheet: [["id", "amount"], ["K", 3 - index]]}))
            sheets.append(sheet)
    app = _uploaded_app(*data, name_a=f"original-A.{formats[0]}", name_b=f"original-B.{formats[1]}")
    _simple_mapping(app)
    app.button(key="run").click().run()
    runtime = _assert_active(app)
    assert len(observed) == 1 and ingestion_calls == [Source.A, Source.B]
    result, options = observed[0]
    assert result is runtime.binding.result
    assert options == dict(
        data_a=data[0],
        data_b=data[1],
        mapping=MAPPING,
        format_a=formats[0],
        format_b=formats[1],
        worksheet_a=sheets[0],
        worksheet_b=sheets[1],
    )
    assert not any("name" in option for option in options)


def _assert_preserved(app, runtime, annotation):
    assert _assert_active(app) is runtime
    assert runtime.reviews_by_key[annotation.matching_key] is annotation
    assert runtime.annotation_revision == 7 and runtime.editor_generation == 3


def test_views_selection_and_downloads_preserve_annotations_without_rebinding(
    monkeypatch, review_downloads
):
    calls = []
    original = tallydiff.bind_review_result

    def tracked(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(tallydiff, "bind_review_result", tracked)
    app = _run_app(comparisons=True)
    runtime = _assert_active(app)
    annotation = _seed(runtime)
    saved = export_review_session(runtime.snapshot())
    original_csv = review_downloads["Download exception report"]
    original_profile = review_downloads["Download mapping profile"]
    app.run()
    _assert_preserved(app, runtime, annotation)
    metrics = {metric.label: metric.value for metric in app.metric}
    for label, value in (
        ("Search matching keys", "B"),
        ("Search matching keys", ""),
        ("Exception categories", []),
        ("Exception categories", [FindingCategory.AMOUNT_MISMATCH]),
        ("Minimum absolute delta", "3"),
        ("Minimum absolute delta", "0"),
        ("Exception sort order", "absolute_delta_desc"),
    ):
        _review_widget(app, label).set_value(value).run()
        _assert_preserved(app, runtime, annotation)
    secondary = next(
        widget for widget in app.checkbox if widget.label == "Has secondary field mismatch"
    )
    secondary.set_value(True).run()
    _assert_preserved(app, runtime, annotation)
    next(
        widget for widget in app.checkbox if widget.label == "Has secondary field mismatch"
    ).set_value(False).run()
    for row in (0, 1):
        app.session_state[_exception_table(app).key] = {
            "selection": {"rows": [row], "columns": [], "cells": []}
        }
        app.run()
        _assert_preserved(app, runtime, annotation)
        assert any("Selected key:" in text.value for text in app.text)
    _exception_downloads(app)[0].click().run()
    _assert_preserved(app, runtime, annotation)
    app.download_button(key="download_profile").click().run()
    _assert_preserved(app, runtime, annotation)
    assert len(calls) == 1
    assert export_review_session(runtime.snapshot()) == saved
    assert (
        review_downloads["Download exception report"]
        == original_csv
        == export_exceptions_csv(runtime.binding.result)
    )
    assert review_downloads["Download mapping profile"] == original_profile
    assert {metric.label: metric.value for metric in app.metric} == metrics


EDIT_PATHS = [
    pytest.param("selectbox", "map_key_a_0", "other", id="key-a"),
    pytest.param("selectbox", "map_key_b_0", "other", id="key-b"),
    pytest.param("selectbox", "map_amount_a", "alternate", id="amount-a"),
    pytest.param("selectbox", "map_amount_b", "alternate", id="amount-b"),
    pytest.param("selectbox", "map_key_a_0", None, id="incomplete-key"),
    pytest.param("button", "add_key", None, id="add-key"),
    pytest.param("button", "remove_key", None, id="remove-key"),
    pytest.param("selectbox", "map_comparison_a_0", "currency", id="comparison-a"),
    pytest.param("selectbox", "map_comparison_b_0", "currency", id="comparison-b"),
    pytest.param("button", "add_comparison", None, id="add-comparison"),
    pytest.param("button", "remove_comparison", None, id="remove-comparison"),
    pytest.param("checkbox", "norm_casefold_0", True, id="casefold"),
    pytest.param("checkbox", "norm_collapse_whitespace_0", True, id="whitespace"),
    pytest.param("checkbox", "norm_remove_punctuation_0", True, id="punctuation"),
    pytest.param("checkbox", "norm_strip_leading_zeros_0", True, id="leading-zeros"),
    pytest.param("text_input", "amount_tolerance", "0.02", id="tolerance"),
    pytest.param("text_input", "amount_tolerance", "NaN", id="invalid-tolerance"),
    pytest.param("radio", "reconciliation_mode", MODE, id="mode"),
]


@pytest.mark.parametrize("widget,key,value", EDIT_PATHS)
def test_mapping_callbacks_and_configuration_mismatch_invalidate_review_state(widget, key, value):
    app = _run_app(comparisons=True, two_keys=key == "remove_key")
    _seed(_assert_active(app))
    # Future result-scoped keys are cleared centrally alongside current filters.
    app.session_state["review_editor_future"] = "stale note"
    if widget == "button":
        app.button(key=key).click().run()
    else:
        getattr(app, widget)(key=key).set_value(value).run()
    assert not app.exception
    _assert_cleared(app)
    assert "review_editor_future" not in app.session_state
    assert app.file_uploader(key="upload_A").value is not None
    assert app.file_uploader(key="upload_B").value is not None


@pytest.mark.parametrize("side", ["A", "B"])
@pytest.mark.parametrize(
    "change",
    ["bytes", "rename", "remove", "invalid"],
    ids=["changed-bytes", "renamed", "removed", "invalid-input"],
)
def test_file_changes_and_removal_invalidate_runtime_and_rename_does_not_change_persistent_identity(
    side, change
):
    app = _run_app()
    runtime = _assert_active(app)
    _seed(runtime)
    old_fingerprint = runtime.binding.result_fingerprint
    data = DATA_A if side == "A" else DATA_B
    name = f"file_{side.lower()}.csv"
    if change == "bytes":
        data = data.replace(b"Sales", b"Changed raw evidence")
    elif change == "rename":
        name = "byte-identical-renamed.csv"
    elif change == "invalid":
        data = b"\xff\xfe\x00"
    app.file_uploader(key=f"upload_{side}").set_value(
        None if change == "remove" else (name, data, "text/csv")
    ).run()
    assert not app.exception
    _assert_cleared(app)
    if change == "rename":
        assert _binding().result_fingerprint == old_fingerprint
        _simple_mapping(app)
        app.text_input(key="amount_tolerance").set_value("0.01").run()
        app.button(key="run").click().run()
        fresh = _assert_active(app)
        _assert_fresh(fresh)
        assert fresh.binding.result_fingerprint == old_fingerprint


def test_invalidating_and_restoring_configuration_or_source_never_resurrects_annotations():
    app = _run_app()
    original = _assert_active(app)
    old_identity = app.session_state["completed"][0]
    _seed(original)
    app.text_input(key="amount_tolerance").set_value("0.02").run()
    _assert_cleared(app)
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    _assert_cleared(app)
    app.button(key="run").click().run()
    fresh = _assert_active(app)
    _assert_fresh(fresh)
    assert app.session_state["completed"][0] == old_identity
    assert fresh.binding.result_fingerprint == original.binding.result_fingerprint
    _seed(fresh)
    app.file_uploader(key="upload_A").set_value(
        ("file_a.csv", DATA_A.replace(b"Sales", b"Different"), "text/csv")
    ).run()
    _assert_cleared(app)
    app.file_uploader(key="upload_A").set_value(("file_a.csv", DATA_A, "text/csv")).run()
    _simple_mapping(app)
    _assert_cleared(app)
    app.button(key="run").click().run()
    _assert_fresh(_assert_active(app))


@pytest.mark.parametrize("identical", [True, False], ids=["identical-profile", "different-profile"])
def test_successful_profile_apply_always_invalidates_review_state(review_downloads, identical):
    app = _run_app()
    previous = _assert_active(app)
    _seed(previous)
    old_identity = app.session_state["completed"][0]
    data = review_downloads["Download mapping profile"]
    if not identical:
        profile = tallydiff.load_mapping_profile(data)
        data = tallydiff.export_mapping_profile(profile.mapping, amount_tolerance=Decimal("0.02"))
    app.file_uploader(key="profile_upload").set_value(
        ("renamed-profile.json", data, "application/json")
    ).run()
    assert _assert_active(app) is previous
    app.button(key="apply_profile").click().run()
    assert not app.exception
    _assert_cleared(app)
    app.button(key="run").click().run()
    _assert_fresh(_assert_active(app))
    assert (app.session_state["completed"][0] == old_identity) == identical


def test_failed_profile_apply_preserves_active_annotations():
    app = _run_app()
    runtime = _assert_active(app)
    annotation = _seed(runtime)
    app.file_uploader(key="profile_upload").set_value(
        ("invalid.json", b"{}", "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    assert app.error and not app.exception
    _assert_preserved(app, runtime, annotation)


@pytest.mark.parametrize("side", ["A", "B"])
def test_worksheet_change_and_missing_selection_clear_runtime(xlsx_bytes, side):
    workbook = xlsx_bytes(
        {
            "One": [["id", "amount"], ["K", 3]],
            "Two": [["id", "amount"], ["K", 4]],
        }
    )
    data = (workbook, b"id,amount\nK,2\n") if side == "A" else (b"id,amount\nK,2\n", workbook)
    app = _uploaded_app(*data, **{"name_a" if side == "A" else "name_b": "multi.xlsx"})
    _worksheet(app, side).set_value("One").run()
    _simple_mapping(app)
    app.button(key="run").click().run()
    _seed(_assert_active(app))
    _worksheet(app, side).set_value("Two").run()
    _assert_cleared(app)
    _worksheet(app, side).set_value("One").run()
    _simple_mapping(app)
    _assert_cleared(app)
    app.button(key="run").click().run()
    _assert_fresh(_assert_active(app))
    _worksheet(app, side).set_value(None).run()
    _assert_cleared(app)


@pytest.mark.parametrize("side", list(Source))
def test_ingestion_failure_on_same_run_clears_previous_active_annotations(monkeypatch, side):
    app = _run_app()
    _seed(_assert_active(app))
    original = tallydiff.ingest_csv

    def failed(*args, **kwargs):
        if kwargs["source"] is side:
            raise IngestionError(side, "synthetic ingestion failure", source_row=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(tallydiff, "ingest_csv", failed)
    app.button(key="run").click().run()
    assert not app.exception and any(
        "synthetic ingestion failure" in error.value for error in app.error
    )
    _assert_cleared(app)


@pytest.mark.parametrize(
    "failure,message",
    [
        (
            ReconciliationIntegrityError("synthetic integrity failure"),
            "Reconciliation integrity check failed",
        ),
        (KeyNormalizationError("synthetic normalization failure"), "Key normalization blocked"),
        (NormalizationCollisionError((), ()), "Normalization collisions block reconciliation"),
    ],
    ids=["integrity", "normalization", "collision"],
)
def test_failed_reconciliation_rerun_clears_previous_review_state(monkeypatch, failure, message):
    app = _run_app()
    _seed(_assert_active(app))

    def failed(*args, **kwargs):
        raise failure

    monkeypatch.setattr(tallydiff, "reconcile", failed)
    app.button(key="run").click().run()
    assert not app.exception and any(message in error.value for error in app.error)
    _assert_cleared(app)


def test_new_review_binding_failure_is_visible_and_leaves_no_completed_or_review_state(monkeypatch):
    app = _run_app()
    _seed(_assert_active(app))
    calls = []

    def failed(result, **kwargs):
        calls.append(result)
        raise ReviewSessionError("synthetic invalid completed evidence")

    monkeypatch.setattr(tallydiff, "bind_review_result", failed)
    app.button(key="run").click().run()
    assert len(calls) == 1 and not app.exception
    assert [error.value for error in app.error] == [
        "Review binding integrity check failed: synthetic invalid completed evidence"
    ]
    _assert_cleared(app)


@pytest.mark.parametrize("failure_type", [RuntimeError, AssertionError])
def test_unrelated_binding_programmer_defects_are_not_swallowed(monkeypatch, failure_type):
    app = _run_app()
    _seed(_assert_active(app))

    def failed(*args, **kwargs):
        raise failure_type("synthetic programmer defect")

    monkeypatch.setattr(tallydiff, "bind_review_result", failed)
    app.button(key="run").click().run()
    assert app.exception and "synthetic programmer defect" in app.exception[0].message
    assert not app.error
    _assert_cleared(app)


def test_different_successful_binding_starts_fresh_even_with_same_configuration(monkeypatch):
    app = _run_app()
    old = _assert_active(app)
    _seed(old)
    original = tallydiff.bind_review_result

    def changed(*args, **kwargs):
        return replace(original(*args, **kwargs), result_fingerprint="0" * 64)

    monkeypatch.setattr(tallydiff, "bind_review_result", changed)
    app.button(key="run").click().run()
    new = _assert_active(app)
    assert new.binding.result_fingerprint != old.binding.result_fingerprint
    assert new.binding.result is not old.binding.result
    _assert_fresh(new)


def test_corrupted_previous_runtime_does_not_prevent_a_new_successful_run():
    app = _run_app()
    previous = _assert_active(app)
    _seed(previous)
    previous.reviews_by_key = dict(reversed(previous.reviews_by_key.items()))
    app.button(key="run").click().run()
    new = _assert_active(app)
    assert new.binding is not previous.binding
    _assert_fresh(new)


def _forbid_binding(*args, **kwargs):
    raise AssertionError("ordinary rerun must reuse the stored successful-run binding")


@pytest.mark.parametrize(
    "damage", ["missing", "wrong-result", "missing-review", "wrong-key", "invalid-note"]
)
def test_missing_or_corrupted_runtime_recovers_fresh_from_cached_binding_without_rebinding(
    monkeypatch, damage
):
    app = _run_app()
    previous = _assert_active(app)
    _seed(previous)
    cached_binding = previous.binding
    if damage == "missing":
        del app.session_state[ui._REVIEW_STATE_KEY]
    elif damage == "wrong-result":
        previous.binding = _binding(DATA_A.replace(b"A,one,3", b"A,one,4"), DATA_B)
    else:
        _corrupt_runtime(previous, "missing" if damage == "missing-review" else damage)
    monkeypatch.setattr(tallydiff, "bind_review_result", _forbid_binding)
    app.run()
    recovered = _assert_active(app)
    assert recovered is not previous
    assert recovered.binding is cached_binding
    _assert_fresh(recovered)


def test_lost_cached_binding_is_recovered_from_valid_active_runtime_without_rebinding(monkeypatch):
    app = _run_app()
    runtime = _assert_active(app)
    annotation = _seed(runtime)
    del app.session_state[ui._REVIEW_BINDING_KEY]
    monkeypatch.setattr(tallydiff, "bind_review_result", _forbid_binding)
    app.run()
    _assert_preserved(app, runtime, annotation)


def test_orphan_review_state_is_cleared_when_completed_result_is_missing():
    app = _run_app()
    _seed(_assert_active(app))
    del app.session_state["completed"]
    app.run()
    assert not app.exception
    _assert_cleared(app)


def test_completed_result_with_no_successful_run_binding_fails_visibly_without_rebinding(
    monkeypatch,
):
    app = _run_app()
    del app.session_state[ui._REVIEW_STATE_KEY]
    del app.session_state[ui._REVIEW_BINDING_KEY]
    monkeypatch.setattr(tallydiff, "bind_review_result", _forbid_binding)
    app.run()
    assert not app.exception
    assert any("completed run has no matching binding" in error.value for error in app.error)
    _assert_cleared(app)


def test_central_cleanup_clears_only_result_scoped_review_and_selection_keys(monkeypatch):
    cleared_keys = (
        "completed",
        ui._REVIEW_STATE_KEY,
        ui._REVIEW_BINDING_KEY,
        "review_search_old",
        "review_categories_old",
        "review_secondary_old",
        "review_minimum_old",
        "review_sort_old",
        "review_editor_future",
        "review_session_future",
        "_review_download_future",
        "exceptions_old_view",
        "tolerated_old",
        "exact_old",
    )
    retained = {
        key: object()
        for key in (
            "upload_A",
            "upload_B",
            "map_amount_a",
            "map_key_a_0",
            "norm_casefold_0",
            "profile_upload",
            "profile_notice",
            "amount_tolerance",
            "reconciliation_mode",
            "unrelated",
        )
    }
    state = dict.fromkeys(cleared_keys, "old state") | retained
    monkeypatch.setattr(ui.st, "session_state", state)
    ui._clear_result()
    assert state == retained


def test_review_progress_filter_and_persistence_controls_precede_prepared_downloads():
    app = _run_app()
    _assert_active(app)
    assert {widget.label for widget in app.button} == {
        "Use selected key mapping",
        "Use selected amount mapping",
        "Apply profile",
        "+ Add key field",
        "Remove last key field",
        "+ Add comparison field",
        "Remove last comparison field",
        "Run reconciliation",
        "Apply review session",
        "Prepare review downloads",
    }
    assert {widget.label for widget in app.file_uploader} == {
        "File A CSV or XLSX",
        "File B CSV or XLSX",
        "Mapping profile JSON",
        "Review session JSON",
    }
    assert {widget.label for widget in app.download_button} == {
        "Download mapping profile",
        "Download exception report",
    }
    assert next(widget for widget in app.button if widget.label == "Apply review session").disabled
    assert {widget.label for widget in app.text_input} == {
        "Amount tolerance",
        "Search matching keys",
        "Minimum absolute delta",
    }
    assert not app.text_area
    assert not any(button.label == "Save note" for button in app.button)
    assert {
        widget.label
        for widget in (*app.radio, *app.selectbox, *app.multiselect)
        if "disposition" in widget.label.lower()
    } == {"Review disposition"}
    assert {metric.label for metric in app.metric} == {
        "File A control total",
        "File B control total",
        "Net difference (A - B)",
        "Exact match",
        "Within tolerance",
        "Amount mismatch",
        "File A only",
        "File B only",
        "Duplicate / ambiguous",
        "Net accepted variance (A - B)",
        "Review items",
        "Unreviewed",
        "Needs follow-up",
        "Review complete",
    }
    exception_table = _exception_table(app)
    assert exception_table.value.columns[-1] == "Review disposition"
    assert exception_table.value["Review disposition"].tolist() == ["Unreviewed", "Unreviewed"]
    assert not any("Review note" in frame.value.columns for frame in app.dataframe)
    assert not any(
        "Review disposition" in frame.value.columns
        for frame in app.dataframe
        if frame.key != exception_table.key
    )
