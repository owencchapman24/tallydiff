"""Comparison editing, source resets, and atomic portable profile state."""

import json
from decimal import Decimal

import pytest
from test_app import _assert_result_cleared, _simple_mapping, _uploaded_app
from test_app import review_downloads as review_downloads

from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    export_mapping_profile,
)

NORMALIZATION_FIELDS = (
    "casefold",
    "collapse_whitespace",
    "remove_punctuation",
    "strip_leading_zeros",
)
DATA = b"id,department,currency,amount\nINV,Sales,USD,10\n"


def _app():
    app = _uploaded_app(DATA, DATA.replace(b",10\n", b",9\n"))
    _simple_mapping(app)
    return app


def _set_comparisons(app, pairs):
    while app.session_state["comparison_count"] < len(pairs):
        app.button(key="add_comparison").click().run()
    for index, (left, right) in enumerate(pairs):
        app.selectbox(key=f"map_comparison_a_{index}").set_value(left)
        app.selectbox(key=f"map_comparison_b_{index}").set_value(right)
    app.run()


def _pairs(app):
    return [
        (
            app.selectbox(key=f"map_comparison_a_{index}").value,
            app.selectbox(key=f"map_comparison_b_{index}").value,
        )
        for index in range(app.session_state["comparison_count"])
    ]


def _upload_profile(app, data, name="profile.json"):
    app.file_uploader(key="profile_upload").set_value((name, data, "application/json")).run()


def _editing_state(app):
    state = {
        "key_count": app.session_state["key_count"],
        "comparison_count": app.session_state["comparison_count"],
        "amount_tolerance": app.text_input(key="amount_tolerance").value,
        "reconciliation_mode": app.radio(key="reconciliation_mode").value,
    }
    state.update(
        {widget.key: widget.value for widget in app.selectbox if widget.key.startswith("map_")}
    )
    state.update(
        {widget.key: widget.value for widget in app.checkbox if widget.key.startswith("norm_")}
    )
    return state


def _assert_no_comparison_keys(app):
    for index in range(3):
        for side in ("a", "b"):
            assert f"map_comparison_{side}_{index}" not in app.session_state


def test_comparison_count_defaults_adds_unselected_fields_and_never_resurrects() -> None:
    app = _app()
    assert app.session_state["comparison_count"] == 0
    assert app.button(key="remove_comparison").disabled
    assert not any(widget.key.startswith("map_comparison_") for widget in app.selectbox)
    assert not app.button(key="run").disabled
    app.button(key="add_comparison").click().run()
    assert app.session_state["comparison_count"] == 1
    assert _pairs(app) == [(None, None)]
    assert app.selectbox(key="map_comparison_a_0").label == "File A comparison field 1"
    assert app.selectbox(key="map_comparison_b_0").label == "File B comparison field 1"
    assert app.button(key="run").disabled
    assert not any(button.key == "download_profile" for button in app.download_button)
    _set_comparisons(app, [("department", "department")])
    app.button(key="add_comparison").click().run()
    assert _pairs(app) == [("department", "department"), (None, None)]
    _set_comparisons(app, [("department", "department"), ("currency", "currency")])
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    assert _pairs(app) == [("department", "department"), ("currency", "currency")]
    app.button(key="run").click().run()
    assert "completed" in app.session_state
    app.button(key="remove_comparison").click().run()
    _assert_result_cleared(app)
    assert _pairs(app) == [("department", "department")]
    for side in ("a", "b"):
        assert f"map_comparison_{side}_1" not in app.session_state
    app.button(key="add_comparison").click().run()
    assert _pairs(app) == [("department", "department"), (None, None)]
    app.button(key="remove_comparison").click().run()
    app.button(key="remove_comparison").click().run()
    assert app.session_state["comparison_count"] == 0
    assert app.button(key="remove_comparison").disabled
    _assert_no_comparison_keys(app)
    app.button(key="add_comparison").click().run()
    assert _pairs(app) == [(None, None)]


def test_comparison_add_has_a_column_derived_bound() -> None:
    app = _uploaded_app(b"id,amount\nINV,1\n", b"id,amount\nINV,1\n")
    _simple_mapping(app)
    for _ in range(3):
        if app.button(key="add_comparison").disabled:
            break
        app.button(key="add_comparison").click().run()
    assert app.button(key="add_comparison").disabled
    assert 1 <= app.session_state["comparison_count"] <= 2
    assert not app.exception


def test_new_comparison_choices_exclude_amount_and_other_fields_but_allow_keys() -> None:
    app = _app()
    _set_comparisons(app, [("department", "department")])
    app.button(key="add_comparison").click().run()
    for side in ("a", "b"):
        first = app.selectbox(key=f"map_comparison_{side}_0")
        second = app.selectbox(key=f"map_comparison_{side}_1")
        assert first.value == "department" and "department" in first.options
        assert "amount" not in first.options and "amount" not in second.options
        assert "department" not in second.options
        assert "id" in first.options and "id" in second.options
        second.set_value("id")
    app.run()
    for side in ("a", "b"):
        assert "id" not in app.selectbox(key=f"map_comparison_{side}_0").options
    assert not app.button(key="run").disabled
    app.button(key="run").click().run()
    assert [field.file_a for field in app.session_state["completed"][1].comparison_fields] == [
        "department",
        "id",
    ]


@pytest.mark.parametrize("side", ["a", "b"])
def test_own_comparison_choice_survives_amount_conflict_and_visibly_blocks(side) -> None:
    app = _app()
    _set_comparisons(app, [("department", "department")])
    app.button(key="run").click().run()
    app.selectbox(key=f"map_amount_{side}").set_value("department").run()
    _assert_result_cleared(app)
    field = app.selectbox(key=f"map_comparison_{side}_0")
    assert field.value == "department" and "department" in field.options
    assert app.button(key="run").disabled
    assert not any(button.key == "download_profile" for button in app.download_button)
    assert any(
        "must not use the amount column" in message.value for message in (*app.info, *app.warning)
    )
    app.run()
    assert app.selectbox(key=f"map_comparison_{side}_0").value == "department"


@pytest.mark.parametrize("side", ["a", "b"])
def test_duplicate_editing_state_is_retained_and_authoritatively_blocked(side) -> None:
    app = _app()
    _set_comparisons(app, [("department", "department"), ("currency", "currency")])
    key = f"map_comparison_{side}_1"
    widget_id = app.selectbox(key=key).id
    widget_states = app._tree.get_widget_states()
    for index, widget in enumerate(widget_states.widgets):
        if widget.id == widget_id:
            del widget_states.widgets[index]
            break
    app.session_state[key] = "department"
    # Inject invalid server editing state while preserving the uploaded files.
    # The previous client options cannot serialize this deliberately blocked value.
    app._run(widget_states)
    assert not app.exception
    for index in range(2):
        widget = app.selectbox(key=f"map_comparison_{side}_{index}")
        assert widget.value == "department" and "department" in widget.options
    assert app.button(key="run").disabled
    assert not any(button.key == "download_profile" for button in app.download_button)
    assert any(
        "comparison column must be selected only once" in item.value
        for item in (*app.info, *app.warning)
    )


@pytest.mark.parametrize("side", ["a", "b"])
def test_each_directional_comparison_edit_invalidates_completed_even_when_reverted(side) -> None:
    app = _app()
    _set_comparisons(app, [("department", "department")])
    app.button(key="run").click().run()
    identity = app.session_state["completed"][0]
    app.selectbox(key=f"map_comparison_{side}_0").set_value("currency").run()
    _assert_result_cleared(app)
    app.selectbox(key=f"map_comparison_{side}_0").set_value("department").run()
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] == identity


def test_count_changes_invalidate_zero_comparison_results_without_identity_drift() -> None:
    app = _app()
    app.button(key="run").click().run()
    identity = app.session_state["completed"][0]
    app.button(key="add_comparison").click().run()
    _assert_result_cleared(app)
    app.button(key="remove_comparison").click().run()
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] == identity
    assert app.session_state["completed"][1].comparison_fields == ()


@pytest.mark.parametrize("source", ["A", "B"])
def test_replacement_source_clears_every_comparison_and_existing_mapping(source) -> None:
    app = _app()
    _set_comparisons(app, [("department", "department"), ("currency", "currency")])
    app.checkbox(key="norm_casefold_0").set_value(True).run()
    app.button(key="run").click().run()
    app.file_uploader(key=f"upload_{source}").set_value(
        (f"file_{source.lower()}.csv", DATA.replace(b"INV", b"NEW"), "text/csv")
    ).run()
    _assert_result_cleared(app)
    assert app.session_state["comparison_count"] == 0
    assert app.session_state["key_count"] == 1
    assert all(widget.value is None for widget in app.selectbox)
    assert not app.checkbox(key="norm_casefold_0").value
    _assert_no_comparison_keys(app)
    app.button(key="add_comparison").click().run()
    assert _pairs(app) == [(None, None)]


@pytest.mark.parametrize("source", ["A", "B"])
def test_worksheet_change_clears_comparisons_and_completed_result(xlsx_bytes, source) -> None:
    workbook = xlsx_bytes(
        {
            "One": [["id", "department", "currency", "amount"], ["INV", "Sales", "USD", 10]],
            "Two": [["id", "department", "currency", "amount"], ["INV", "Other", "USD", 9]],
        }
    )
    app = _uploaded_app(
        workbook if source == "A" else DATA,
        workbook if source == "B" else DATA,
        name_a="book.xlsx" if source == "A" else "file_a.csv",
        name_b="book.xlsx" if source == "B" else "file_b.csv",
    )
    next(box for box in app.selectbox if box.label == f"File {source} worksheet").set_value(
        "One"
    ).run()
    _simple_mapping(app)
    _set_comparisons(app, [("department", "department"), ("currency", "currency")])
    app.checkbox(key="norm_casefold_0").set_value(True).run()
    app.button(key="run").click().run()
    next(box for box in app.selectbox if box.label == f"File {source} worksheet").set_value(
        "Two"
    ).run()
    _assert_result_cleared(app)
    assert app.session_state["comparison_count"] == 0
    assert app.session_state["key_count"] == 1
    assert not app.checkbox(key="norm_casefold_0").value
    for key in ("map_key_a_0", "map_key_b_0", "map_amount_a", "map_amount_b"):
        assert app.selectbox(key=key).value is None
    _assert_no_comparison_keys(app)


def test_v4_profile_restores_order_all_configuration_and_filename_stays_outside_identity(
    review_downloads, caplog
) -> None:
    app = _app()
    _set_comparisons(app, [("department", "department"), ("currency", "currency"), ("id", "id")])
    app.button(key="run").click().run()
    previous = app.session_state["completed"]
    normalization = KeyNormalizationConfig(
        (KeyNormalizationRules(casefold=True), KeyNormalizationRules(remove_punctuation=True))
    )
    mapping = ColumnMapping(
        (("id", "id"), ("currency", "currency")),
        "amount",
        "amount",
        comparison_fields=(
            ComparisonFieldMapping("currency", "department"),
            ComparisonFieldMapping("department", "currency"),
        ),
    )
    data = export_mapping_profile(
        mapping,
        amount_tolerance=Decimal("0.0100"),
        reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY,
        key_normalization=normalization,
    )
    editing = _editing_state(app)
    _upload_profile(app, data, "first.json")
    assert _editing_state(app) == editing
    assert app.session_state["completed"][1] is previous[1]
    app.button(key="apply_profile").click().run()
    _assert_result_cleared(app)
    assert app.session_state["key_count"] == app.session_state["comparison_count"] == 2
    assert _pairs(app) == [("currency", "department"), ("department", "currency")]
    for side in ("a", "b"):
        assert f"map_comparison_{side}_2" not in app.session_state
        assert app.selectbox(key=f"map_key_{side}_0").value == "id"
        assert app.selectbox(key=f"map_key_{side}_1").value == "currency"
        assert app.selectbox(key=f"map_amount_{side}").value == "amount"
    assert app.text_input(key="amount_tolerance").value == "0.0100"
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert app.checkbox(key="norm_casefold_0").value
    assert app.checkbox(key="norm_remove_punctuation_1").value
    assert all(
        app.checkbox(key=f"norm_{field}_{index}").value
        == getattr(normalization.component_rules[index], field)
        for index in range(2)
        for field in NORMALIZATION_FIELDS
    )
    assert json.loads(review_downloads["Download mapping profile"]) == json.loads(data)
    assert "created with a default value" not in caplog.text
    app.button(key="run").click().run()
    identity, result = app.session_state["completed"]
    assert result.comparison_fields == mapping.comparison_fields
    assert result.key_normalization == normalization
    _upload_profile(app, data, "different_filename.json")
    assert app.session_state["completed"][0] == identity
    assert app.session_state["completed"][1] is result
    app.button(key="apply_profile").click().run()
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] == identity


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_legacy_and_empty_v4_profiles_explicitly_clear_all_comparison_state(version) -> None:
    app = _app()
    _set_comparisons(app, [("department", "department"), ("currency", "currency")])
    app.button(key="run").click().run()
    profile = json.loads(export_mapping_profile(ColumnMapping((("id", "id"),), "amount", "amount")))
    profile["version"] = version
    if version < 4:
        profile.pop("comparison_fields")
    if version < 3:
        profile.pop("key_normalization")
    if version == 1:
        profile.pop("reconciliation_mode")
    _upload_profile(app, json.dumps(profile).encode())
    app.button(key="apply_profile").click().run()
    _assert_result_cleared(app)
    assert app.session_state["comparison_count"] == 0
    _assert_no_comparison_keys(app)
    app.button(key="add_comparison").click().run()
    assert _pairs(app) == [(None, None)]
    app.button(key="remove_comparison").click().run()
    app.button(key="run").click().run()
    assert app.session_state["completed"][1].comparison_fields == ()


@pytest.mark.parametrize(
    "failure", ["missing_a", "missing_b", "amount", "duplicate", "incomplete", "malformed"]
)
def test_invalid_v4_profile_keeps_all_configuration_and_completed_result_atomic(failure) -> None:
    app = _app()
    _set_comparisons(app, [("department", "department"), ("currency", "currency")])
    app.checkbox(key="norm_casefold_0").set_value(True)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY)
    app.text_input(key="amount_tolerance").set_value("0.0100").run()
    app.button(key="run").click().run()
    before = _editing_state(app)
    identity, result = app.session_state["completed"]
    invalid = json.loads(
        export_mapping_profile(
            ColumnMapping(
                (("id", "id"),),
                "amount",
                "amount",
                comparison_fields=(ComparisonFieldMapping("id", "id"),),
            )
        )
    )
    if failure in ("missing_a", "missing_b"):
        invalid["comparison_fields"][0]["file_" + failure[-1]] = "missing"
    elif failure == "amount":
        invalid["comparison_fields"][0]["file_a"] = "amount"
    elif failure == "duplicate":
        invalid["comparison_fields"].append({"file_a": "id", "file_b": "department"})
    elif failure == "incomplete":
        invalid["comparison_fields"][0]["file_b"] = None
    data = b"{" if failure == "malformed" else json.dumps(invalid).encode()
    _upload_profile(app, data)
    app.button(key="apply_profile").click().run()
    assert not app.exception and app.error
    assert _editing_state(app) == before
    assert app.session_state["completed"][0] == identity
    assert app.session_state["completed"][1] is result
    assert app.metric


def test_manual_profile_download_preserves_exact_names_order_and_readiness(
    review_downloads,
) -> None:
    data = b"id, department ,currency,amount\nINV,Sales,USD,10\n"
    app = _uploaded_app(data, data)
    _simple_mapping(app)
    app.button(key="add_comparison").click().run()
    app.selectbox(key="map_comparison_a_0").set_value("currency").run()
    assert app.button(key="run").disabled
    assert not any(button.key == "download_profile" for button in app.download_button)
    _set_comparisons(app, [("currency", "currency"), (" department ", " department ")])
    profile = json.loads(review_downloads["Download mapping profile"])
    assert profile["version"] == 4
    assert profile["comparison_fields"] == [
        {"file_a": "currency", "file_b": "currency"},
        {"file_a": " department ", "file_b": " department "},
    ]
    app.button(key="run").click().run()
    assert not app.exception
    assert [
        (mapping.file_a, mapping.file_b)
        for mapping in app.session_state["completed"][1].comparison_fields
    ] == [("currency", "currency"), (" department ", " department ")]


def test_remove_and_readd_with_changed_order_requires_fresh_result_and_changes_identity() -> None:
    app = _app()
    _set_comparisons(app, [("department", "department"), ("currency", "currency")])
    app.button(key="run").click().run()
    original_identity = app.session_state["completed"][0]
    app.button(key="remove_comparison").click().run()
    _assert_result_cleared(app)
    app.button(key="remove_comparison").click().run()
    _assert_result_cleared(app)
    _set_comparisons(app, [("currency", "currency"), ("department", "department")])
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] != original_identity
    assert [mapping.file_a for mapping in app.session_state["completed"][1].comparison_fields] == [
        "currency",
        "department",
    ]
