"""Discovery is cached evidence until an analyst explicitly adopts a candidate."""

from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from test_app import _exception_downloads, _simple_mapping, _uploaded_app, _worksheet

import tallydiff.app as ui
import tallydiff.discovery as discovery
from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    ReconciliationMode,
    Source,
    export_mapping_profile,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ("casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros")
DATA_A = b"id,amount,notes\nINV,10,left\n"
DATA_B = b"id,amount,notes\nINV,9,right\n"


def _sample_app():
    return _uploaded_app(
        (ROOT / "sample_data/file_a.csv").read_bytes(),
        (ROOT / "sample_data/file_b.csv").read_bytes(),
    )


def _bundle(app):
    return app.session_state["_discovery_pair"]


def _key_selector(app):
    return app.selectbox(key="discovery_key_candidate")


def _amount_selector(app):
    return app.selectbox(key="discovery_amount_candidate")


def _select_key(app, pairs):
    index = next(
        index
        for index, candidate in enumerate(_bundle(app).keys.suggestions)
        if candidate.key_pairs == tuple(pairs)
    )
    _key_selector(app).set_value(index).run()
    assert not app.exception
    return index


def _select_amount(app, column_a, column_b):
    index = next(
        index
        for index, candidate in enumerate(_bundle(app).amounts.suggestions)
        if (candidate.column_a, candidate.column_b) == (column_a, column_b)
    )
    _amount_selector(app).set_value(index).run()
    assert not app.exception
    return index


def _map_values(app):
    return {widget.key: widget.value for widget in app.selectbox if widget.key.startswith("map_")}


def _messages(app):
    return "\n".join(
        element.value
        for element in (*app.caption, *app.text, *app.info, *app.warning, *app.success)
    )


def _assert_no_result(app):
    assert not app.exception
    assert "completed" not in app.session_state
    assert not app.metric
    assert not _exception_downloads(app)


def _set_pairs(app, pairs, *, amount_a, amount_b):
    while app.session_state["key_count"] < len(pairs):
        app.button(key="add_key").click().run()
    for index, (left, right) in enumerate(pairs):
        app.selectbox(key=f"map_key_a_{index}").set_value(left)
        app.selectbox(key=f"map_key_b_{index}").set_value(right)
    app.selectbox(key="map_amount_a").set_value(amount_a)
    app.selectbox(key="map_amount_b").set_value(amount_b)
    app.run()
    assert not app.exception


def _comparison(app, left, right):
    app.button(key="add_comparison").click().run()
    app.selectbox(key="map_comparison_a_0").set_value(left)
    app.selectbox(key="map_comparison_b_0").set_value(right).run()


@pytest.fixture
def discovery_calls(monkeypatch):
    calls = Counter()
    for name in (
        "discover_csv",
        "discover_xlsx",
        "suggest_key_mappings",
        "suggest_amount_mappings",
    ):
        original = getattr(discovery, name)

        def record(*args, _name=name, _original=original, **kwargs):
            calls[(_name, kwargs.get("source"))] += 1
            if _name.startswith("suggest_"):
                assert kwargs["max_suggestions"] == 8
            return _original(*args, **kwargs)

        monkeypatch.setattr(discovery, name, record)
    return calls


def test_sample_upload_exposes_discovery_without_selecting_mapping_or_running():
    app = _sample_app()
    assert not app.exception
    assert [header.value for header in app.header] == [
        "1. Choose files",
        "2. Map columns",
        "3. Run reconciliation",
    ]
    subheaders = [header.value for header in app.subheader]
    discovery_index = next(
        index for index, text in enumerate(subheaders) if text.startswith("Discovery")
    )
    assert discovery_index < subheaders.index("Mapping profile")
    assert "Key mapping candidates" in subheaders
    assert "Amount mapping candidates" in subheaders
    assert all(value is None for value in _map_values(app).values())
    assert _key_selector(app).value is None
    assert _amount_selector(app).value is None
    assert app.button(key="discovery_apply_key").disabled
    assert app.button(key="discovery_apply_amount").disabled
    assert app.button(key="run").disabled
    assert not app.dataframe
    assert app.table
    assert "automatically" in _messages(app).lower()
    assert "_key_rows" not in _messages(app)
    _assert_no_result(app)


def test_merely_selecting_candidates_changes_neither_mapping_nor_run_state():
    app = _sample_app()
    before = _map_values(app)
    _select_key(app, (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")))
    _select_amount(app, "Invoice Amount", "Gross Amount")
    assert _map_values(app) == before
    assert app.session_state["key_count"] == 1
    assert not app.button(key="discovery_apply_key").disabled
    assert not app.button(key="discovery_apply_amount").disabled
    _assert_no_result(app)


def test_profile_tables_display_exact_counts_and_only_complete_candidate_totals():
    data = b"id,amount,mixed,blank\n A ,0.1,1,\nA,0.2,no, \nB,0.3,2,\n"
    app = _uploaded_app(data, data)
    profiles = [table.value for table in app.table if "Blank rows" in table.value.columns]
    assert len(profiles) == 2
    for profile in profiles:
        assert profile["Column"].tolist() == ["id", "amount", "mixed", "blank"]
        rows = profile.set_index("Column")
        assert int(rows.loc["blank", "Blank rows"]) == 3
        assert int(rows.loc["id", "Distinct nonblank evidence"]) == 3
        assert int(rows.loc["id", "Key-eligible rows"]) == 3
        assert int(rows.loc["id", "Distinct baseline keys"]) == 2
        assert int(rows.loc["id", "Duplicate excess"]) == 1
        assert int(rows.loc["mixed", "Amount-parseable rows"]) == 2
        assert Decimal(rows.loc["amount", "Candidate amount total"]) == Decimal("0.6")
        assert rows.loc["mixed", "Candidate amount total"] == "—"
        assert rows.loc["blank", "Candidate amount total"] == "—"
    messages = _messages(app)
    assert "File A: 3 rows" in messages and "File B: 3 rows" in messages
    assert "4 columns" in messages
    assert "every row" in messages.lower()
    assert "amount field" in messages.lower()


def test_mixed_csv_xlsx_discovery_keeps_candidate_invalid_cells_advisory(xlsx_bytes):
    data = xlsx_bytes({"Data": [["id", "amount", "notes"], ["INV", 9, True], ["OTHER", 3, "=1+1"]]})
    app = _uploaded_app(DATA_A, data, name_b="ledger.xlsx")
    assert not app.exception
    assert _worksheet(app, "B").value == "Data"
    assert _key_selector(app).value is None
    assert _amount_selector(app).value is None
    assert all(value is None for value in _map_values(app).values())
    assert "File B: 2 rows" in _messages(app)
    assert not app.error


@pytest.mark.parametrize("bad", [b"id,amount\nA,1\nB\n", b'id,amount\nA,1\n"B,2\n'])
def test_csv_structural_errors_surface_before_mapping_without_traceback(bad):
    app = _uploaded_app(bad, DATA_B)
    assert not app.exception
    assert any("File A" in error.value and "source record 3" in error.value for error in app.error)
    assert not any(widget.key.startswith("map_") for widget in app.selectbox)
    assert not any(button.key == "run" for button in app.button)
    assert "completed" not in app.session_state


def test_xlsx_structural_errors_surface_before_mapping_without_traceback(xlsx_bytes):
    data = xlsx_bytes({"Data": [["id", "amount"], ["A", 1], [], ["B", 2]]})
    app = _uploaded_app(DATA_A, data, name_b="bad.xlsx")
    assert not app.exception
    assert any("blank interior" in error.value for error in app.error)
    assert not any(widget.key.startswith("map_") for widget in app.selectbox)


def test_partially_invalid_amount_columns_still_allow_manual_mapping():
    data = b"id,amount,notes\nA,word,\nB,2,free text\n"
    app = _uploaded_app(data, data)
    assert not app.exception
    assert not app.error
    assert _bundle(app).amounts.suggestions == ()
    assert any("amount" in message.value.lower() for message in app.info)
    assert app.selectbox(key="map_amount_a").options == ["id", "amount", "notes"]
    assert app.selectbox(key="map_amount_a").value is None
    _assert_no_result(app)


def test_explicit_composite_adoption_resets_all_normalization_and_preserves_other_controls():
    app = _sample_app()
    _set_pairs(
        app,
        (
            ("Vendor ID", "Supplier"),
            ("Invoice Number", "Invoice Ref"),
            ("Invoice Amount", "Gross Amount"),
        ),
        amount_a="Invoice Amount",
        amount_b="Gross Amount",
    )
    for index in range(3):
        for rule in RULES:
            app.checkbox(key=f"norm_{rule}_{index}").set_value(True)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY)
    app.text_input(key="amount_tolerance").set_value("0.0100").run()
    _comparison(app, "Vendor ID", "Supplier")
    app.button(key="run").click().run()
    assert "completed" in app.session_state
    previous_bundle = _bundle(app)
    selected = _select_key(app, (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")))
    app.button(key="discovery_apply_key").click().run()
    assert app.session_state["key_count"] == 2
    for index, (left, right) in enumerate(
        (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref"))
    ):
        assert app.selectbox(key=f"map_key_a_{index}").value == left
        assert app.selectbox(key=f"map_key_b_{index}").value == right
        assert all(not app.checkbox(key=f"norm_{rule}_{index}").value for rule in RULES)
    assert "map_key_a_2" not in app.session_state and "map_key_b_2" not in app.session_state
    assert all(f"norm_{rule}_2" not in app.session_state for rule in RULES)
    assert app.selectbox(key="map_amount_a").value == "Invoice Amount"
    assert app.selectbox(key="map_amount_b").value == "Gross Amount"
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert app.text_input(key="amount_tolerance").value == "0.0100"
    assert app.selectbox(key="map_comparison_a_0").value == "Vendor ID"
    assert app.selectbox(key="map_comparison_b_0").value == "Supplier"
    assert _bundle(app) is previous_bundle
    assert _key_selector(app).value == selected
    assert any("Review or edit" in message.value for message in app.success)
    _assert_no_result(app)
    app.selectbox(key="map_key_a_0").set_value("Invoice Amount").run()
    app.run()
    assert app.selectbox(key="map_key_a_0").value == "Invoice Amount"
    assert _bundle(app) is previous_bundle


def test_amount_adoption_changes_only_amount_fields_and_leaves_secondary_conflict_visible():
    app = _sample_app()
    _set_pairs(app, (("Vendor ID", "Supplier"),), amount_a="Invoice Number", amount_b="Invoice Ref")
    app.checkbox(key="norm_casefold_0").set_value(True)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY)
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    _comparison(app, "Invoice Amount", "Gross Amount")
    app.button(key="run").click().run()
    assert "completed" in app.session_state
    _select_amount(app, "Invoice Amount", "Gross Amount")
    app.button(key="discovery_apply_amount").click().run()
    assert app.selectbox(key="map_amount_a").value == "Invoice Amount"
    assert app.selectbox(key="map_amount_b").value == "Gross Amount"
    assert app.selectbox(key="map_key_a_0").value == "Vendor ID"
    assert app.selectbox(key="map_key_b_0").value == "Supplier"
    assert app.checkbox(key="norm_casefold_0").value
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert app.text_input(key="amount_tolerance").value == "0.01"
    assert app.selectbox(key="map_comparison_a_0").value == "Invoice Amount"
    assert app.selectbox(key="map_comparison_b_0").value == "Gross Amount"
    assert "must not use the amount column" in _messages(app)
    assert app.button(key="run").disabled
    _assert_no_result(app)
    app.selectbox(key="map_amount_a").set_value("Invoice Number").run()
    app.run()
    assert app.selectbox(key="map_amount_a").value == "Invoice Number"


def test_key_and_amount_ties_and_bounded_search_are_visibly_disclosed():
    data_a = b"Left0,Left1,Left2\n1,1,1\n2,2,2\n"
    data_b = b"Right0,Right1,Right2\n1,1,1\n2,2,2\n"
    app = _uploaded_app(data_a, data_b)
    result = _bundle(app)
    assert result.keys.leading_count == result.amounts.leading_count == 9
    messages = _messages(app).lower()
    assert messages.count("9 candidates") >= 2
    assert "tied" in messages
    assert "deterministic only" in messages
    assert "bounded" in messages
    assert "truncated" in messages
    assert str(result.keys.evaluated_single_count) in messages
    assert str(result.keys.evaluated_composite_count) in messages
    assert "control differences" in messages and "ordering" in messages
    assert _key_selector(app).value is _amount_selector(app).value is None


def test_unique_leader_wording_disclaims_proof_and_nontruncated_search_is_not_exhaustive():
    app = _uploaded_app(b"id,Amount\nINV,10\n", b"id,amount\nINV,9\n")
    assert _bundle(app).keys.leading_count == 1
    assert _bundle(app).amounts.leading_count == 1
    assert not _bundle(app).keys.composite_search_truncated
    messages = _messages(app).lower()
    assert "one candidate leads" in messages
    assert "not proof" in messages
    assert "amount semantics" in messages
    assert "bounded" in messages
    assert "all possible" not in messages and "exhaustive" not in messages


def test_upload_bytes_reset_old_candidate_indices_and_manual_mapping():
    app = _sample_app()
    _select_key(app, (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")))
    _select_amount(app, "Invoice Amount", "Gross Amount")
    app.button(key="discovery_apply_key").click().run()
    old = _bundle(app)
    data = (ROOT / "sample_data/file_a.csv").read_bytes().replace(b"1250", b"1251")
    app.file_uploader(key="upload_A").set_value(("file_a.csv", data, "text/csv")).run()
    assert not app.exception
    assert _bundle(app).identity != old.identity
    assert _key_selector(app).value is None and _amount_selector(app).value is None
    assert app.button(key="discovery_apply_key").disabled
    assert app.button(key="discovery_apply_amount").disabled
    assert all(value is None for value in _map_values(app).values())
    assert app.session_state["key_count"] == 1
    assert not app.success


def test_multi_worksheet_choice_is_explicit_and_changes_reset_candidates(xlsx_bytes):
    data = xlsx_bytes(
        {"One": [["id", "amount"], ["INV", 1]], "Two": [["other", "gross"], ["INV", 2]]}
    )
    app = _uploaded_app(data, DATA_B, name_a="multi.xlsx")
    assert _worksheet(app, "A").value is None
    assert not any(widget.key == "discovery_key_candidate" for widget in app.selectbox)
    _worksheet(app, "A").set_value("One").run()
    _select_key(app, (("id", "id"),))
    _select_amount(app, "amount", "amount")
    old = _bundle(app)
    _worksheet(app, "A").set_value("Two").run()
    assert not app.exception
    assert _bundle(app).identity != old.identity
    assert _key_selector(app).value is _amount_selector(app).value is None
    assert app.selectbox(key="map_key_a_0").options == ["other", "gross"]
    assert all(value is None for value in _map_values(app).values())
    assert app.button(key="discovery_apply_key").disabled


def test_mapping_reruns_reuse_session_snapshots_and_pair_suggestions(discovery_calls):
    app = _uploaded_app(DATA_A, DATA_B)
    expected = {
        ("discover_csv", Source.A): 1,
        ("discover_csv", Source.B): 1,
        ("suggest_key_mappings", None): 1,
        ("suggest_amount_mappings", None): 1,
    }
    assert discovery_calls == expected
    bundle = _bundle(app)
    _simple_mapping(app)
    app.checkbox(key="norm_casefold_0").set_value(True).run()
    app.text_input(key="amount_tolerance").set_value("0.0100").run()
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    app.run()
    assert discovery_calls == expected
    assert _bundle(app) is bundle


def test_byte_changes_recompute_one_side_and_pair_but_filename_changes_do_not(discovery_calls):
    app = _uploaded_app(DATA_A, DATA_B)
    original = dict(discovery_calls)
    bundle = _bundle(app)
    app.file_uploader(key="upload_A").set_value(("renamed.csv", DATA_A, "text/csv")).run()
    assert discovery_calls == original
    assert _bundle(app) is bundle
    app.file_uploader(key="upload_A").set_value(
        ("renamed.csv", DATA_A.replace(b",10,", b",11,"), "text/csv")
    ).run()
    assert discovery_calls[("discover_csv", Source.A)] == 2
    assert discovery_calls[("discover_csv", Source.B)] == 1
    assert discovery_calls[("suggest_key_mappings", None)] == 2
    assert discovery_calls[("suggest_amount_mappings", None)] == 2
    source_cache_keys = [
        key for key in app.session_state.keys() if key.startswith("_discovery_source_")
    ]
    assert set(source_cache_keys) == {"_discovery_source_A", "_discovery_source_B"}


def test_worksheet_change_recomputes_only_selected_source_and_current_pair(
    xlsx_bytes, discovery_calls
):
    data = xlsx_bytes(
        {"One": [["id", "amount"], ["INV", 1]], "Two": [["id", "amount"], ["INV", 2]]}
    )
    app = _uploaded_app(DATA_A, data, name_b="multi.xlsx")
    assert discovery_calls[("discover_xlsx", Source.B)] == 0
    _worksheet(app, "B").set_value("One").run()
    assert discovery_calls[("discover_xlsx", Source.B)] == 1
    assert discovery_calls[("suggest_key_mappings", None)] == 1
    _simple_mapping(app)
    app.checkbox(key="norm_casefold_0").set_value(True).run()
    assert discovery_calls[("discover_xlsx", Source.B)] == 1
    assert discovery_calls[("suggest_key_mappings", None)] == 1
    assert discovery_calls[("suggest_amount_mappings", None)] == 1
    _worksheet(app, "B").set_value("Two").run()
    assert discovery_calls[("discover_csv", Source.A)] == 1
    assert discovery_calls[("discover_xlsx", Source.B)] == 2
    assert discovery_calls[("suggest_key_mappings", None)] == 2
    assert discovery_calls[("suggest_amount_mappings", None)] == 2


def test_viewing_selecting_and_noop_adoption_preserve_completed_review_and_downloads():
    app = _uploaded_app(DATA_A, DATA_B)
    _simple_mapping(app)
    app.button(key="run").click().run()
    app.button(key=ui._REVIEW_PREPARE_KEY).click().run()
    completed = app.session_state["completed"]
    runtime = app.session_state[ui._REVIEW_STATE_KEY]
    prepared = app.session_state[ui._REVIEW_PREPARED_KEY]
    _select_key(app, (("id", "id"),))
    _select_amount(app, "amount", "amount")
    app.run()
    assert app.session_state["completed"][1] is completed[1]
    assert app.session_state[ui._REVIEW_STATE_KEY] is runtime
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    app.button(key="discovery_apply_key").click().run()
    app.button(key="discovery_apply_amount").click().run()
    assert app.session_state["completed"][1] is completed[1]
    assert app.session_state[ui._REVIEW_STATE_KEY] is runtime
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    _select_key(app, (("amount", "amount"),))
    app.button(key="discovery_apply_key").click().run()
    _assert_no_result(app)
    assert ui._REVIEW_STATE_KEY not in app.session_state
    assert ui._REVIEW_PREPARED_KEY not in app.session_state
    assert ui._REVIEW_BINDING_KEY not in app.session_state


def test_mapping_profile_application_keeps_discovery_facts_and_allows_later_explicit_adoption(
    discovery_calls,
):
    app = _sample_app()
    bundle = _bundle(app)
    calls = dict(discovery_calls)
    mapping = ColumnMapping(
        (("Vendor ID", "Supplier"),),
        "Invoice Amount",
        "Gross Amount",
        comparison_fields=(ComparisonFieldMapping("Invoice Number", "Invoice Ref"),),
    )
    data = export_mapping_profile(mapping, amount_tolerance=Decimal("0.0100"))
    app.file_uploader(key="profile_upload").set_value(
        ("mapping.json", data, "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    assert not app.exception
    assert _bundle(app) is bundle
    assert discovery_calls == calls
    assert app.selectbox(key="map_key_a_0").value == "Vendor ID"
    assert app.selectbox(key="map_amount_b").value == "Gross Amount"
    assert any("Mapping profile applied" in message.value for message in app.success)
    _select_key(app, (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")))
    app.button(key="discovery_apply_key").click().run()
    assert app.session_state["key_count"] == 2
    assert app.selectbox(key="map_comparison_a_0").value == "Invoice Number"
    assert _bundle(app) is bundle
    assert discovery_calls == calls


def test_candidate_tables_and_selectors_preserve_stage_four_order_and_control_total_disclaimer():
    app = _sample_app()
    bundle = _bundle(app)
    assert _key_selector(app).options == [
        f"{index}. {ui._key_candidate_label(value)}"
        for index, value in enumerate(bundle.keys.suggestions, start=1)
    ]
    assert len(_amount_selector(app).options) == len(bundle.amounts.suggestions)
    for index, suggestion in enumerate(bundle.amounts.suggestions):
        label = _amount_selector(app).options[index]
        assert suggestion.column_a in label and suggestion.column_b in label
    messages = _messages(app).lower()
    assert "control differences" in messages
    assert "do not affect suggestion ordering" in messages
    assert not any(
        "score" in str(column).lower() for table in app.table for column in table.value.columns
    )


def test_run_uses_the_visible_manual_mapping_after_explicit_adoption():
    app = _uploaded_app(DATA_A, DATA_B)
    _select_key(app, (("id", "id"),))
    app.button(key="discovery_apply_key").click().run()
    _select_amount(app, "amount", "amount")
    app.button(key="discovery_apply_amount").click().run()
    app.selectbox(key="map_key_a_0").set_value("amount")
    app.selectbox(key="map_key_b_0").set_value("amount").run()
    assert not app.button(key="run").disabled
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert {finding.key for finding in result.findings} == {("10",), ("9",)}
    assert app.selectbox(key="map_key_a_0").value == "amount"
    assert _key_selector(app).value is not None


def _configuration_evidence(app):
    return app.session_state["_configuration_evidence"]


def _has_configuration_diagnostics(app):
    return any(header.value == "Configuration diagnostics — advisory" for header in app.subheader)


def _configuration_key_table(app):
    return next(
        table.value
        for table in app.table
        if "Key mapping" in table.value.columns and "Candidate" not in table.value.columns
    )


def _configuration_amount_table(app):
    return next(
        table.value
        for table in app.table
        if "Parseable A / total A" in table.value.columns and "Candidate" not in table.value.columns
    )


def _configuration_warning(app, text):
    return [warning.value for warning in app.warning if text in warning.value.lower()]


def _key_data(keys):
    return ("id,amount\n" + "".join(f"{key},1\n" for key in keys)).encode()


@pytest.fixture
def selected_evidence_calls(monkeypatch):
    calls = Counter()
    for name in ("compare_key_mapping", "compare_amount_columns"):
        original = getattr(discovery, name)

        def record(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(discovery, name, record)
    return calls


def test_configuration_diagnostics_require_a_complete_valid_mapping_and_precede_run():
    app = _uploaded_app(DATA_A, DATA_B)
    assert not _has_configuration_diagnostics(app)
    assert "_configuration_evidence" not in app.session_state
    app.selectbox(key="map_key_a_0").set_value("id").run()
    assert not _has_configuration_diagnostics(app)
    _simple_mapping(app)
    assert _has_configuration_diagnostics(app)
    positions = {}
    diagnostic_position = None
    for index, element in enumerate(app._tree):
        key = getattr(element, "key", None)
        if key:
            positions[key] = index
        if (
            getattr(element, "type", None) == "subheader"
            and element.value == "Configuration diagnostics — advisory"
        ):
            diagnostic_position = index
    assert (
        positions["reconciliation_mode"]
        < positions["amount_tolerance"]
        < diagnostic_position
        < positions["run"]
    )
    app.button(key="add_key").click().run()
    assert not _has_configuration_diagnostics(app)
    assert "_configuration_evidence" not in app.session_state
    assert app.button(key="run").disabled


def test_manual_three_component_diagnostics_use_selected_mapping_and_exact_directional_facts():
    app = _uploaded_app(
        b"vendor,invoice,batch,amount\nV1,100,X,10\nV1,100,Y,20\nV1,100,Y,30\nV2,200,Z,40\n",
        b"supplier,ref,run,gross\nV1,100,X,11\nV1,100,Y,20\nV3,300,W,40\n",
    )
    pairs = (("vendor", "supplier"), ("invoice", "ref"), ("batch", "run"))
    _set_pairs(app, pairs, amount_a="amount", amount_b="gross")
    evidence = _configuration_evidence(app)
    assert evidence.key.key_pairs == pairs
    assert len(evidence.key.key_pairs) > 2
    row = _configuration_key_table(app).iloc[0]
    assert row["Eligible A / total A"] == "4 / 4"
    assert row["Eligible B / total B"] == "3 / 3"
    assert row["Overlap rows A / eligible A"] == "3 / 4"
    assert row["Overlap rows B / eligible B"] == "2 / 3"
    assert int(row["Distinct overlapping keys"]) == 2
    assert int(row["Duplicate excess A"]) == 1
    assert int(row["Duplicate excess B"]) == 0
    amount = _configuration_amount_table(app).iloc[0]
    assert amount["File A column"] == "amount"
    assert amount["File B column"] == "gross"
    assert amount["Parseable A / total A"] == "4 / 4"
    assert amount["Parseable B / total B"] == "3 / 3"
    assert "duplicate excess" in _messages(app).lower()
    assert not app.button(key="run").disabled
    _assert_no_result(app)


@pytest.mark.parametrize("method", ["adoption", "profile"])
def test_configuration_diagnostics_follow_explicit_suggestions_and_mapping_profiles(method):
    app = _sample_app()
    pairs = (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref"))
    if method == "adoption":
        _select_key(app, pairs)
        app.button(key="discovery_apply_key").click().run()
        _select_amount(app, "Invoice Amount", "Gross Amount")
        app.button(key="discovery_apply_amount").click().run()
    else:
        profile = export_mapping_profile(ColumnMapping(pairs, "Invoice Amount", "Gross Amount"))
        app.file_uploader(key="profile_upload").set_value(
            ("mapping.json", profile, "application/json")
        ).run()
        app.button(key="apply_profile").click().run()
    assert not app.exception
    assert _has_configuration_diagnostics(app)
    assert _configuration_evidence(app).key.key_pairs == pairs
    assert _configuration_evidence(app).amount.column_a == "Invoice Amount"
    assert _configuration_evidence(app).amount.column_b == "Gross Amount"
    _assert_no_result(app)


@pytest.mark.parametrize("side", ["A", "B"])
def test_zero_eligible_rows_identify_the_side_without_zero_overlap_warning(side):
    data = (_key_data(["", ""]), _key_data(["K1", "K2"]))
    app = _uploaded_app(*(data if side == "A" else tuple(reversed(data))))
    _simple_mapping(app)
    warnings = _configuration_warning(app, "no eligible baseline key rows")
    assert len(warnings) == 1
    assert f"File {side}" in warnings[0]
    assert not _configuration_warning(app, "no exact baseline overlap")
    assert not _configuration_warning(app, "very low baseline overlap")
    assert not app.button(key="run").disabled


def test_zero_overlap_is_advisory_and_can_become_an_exact_match_with_explicit_normalization():
    app = _uploaded_app(b"id,amount\nABC,1\n", b"id,amount\nabc,1\n")
    _simple_mapping(app)
    evidence = _configuration_evidence(app)
    assert evidence.key.overlap_distinct_count == 0
    assert _configuration_warning(app, "no exact baseline overlap")
    assert not _configuration_warning(app, "no eligible baseline key rows")
    assert not app.button(key="run").disabled
    app.checkbox(key="norm_casefold_0").set_value(True).run()
    assert _configuration_evidence(app) is evidence
    messages = _messages(app).lower()
    assert "baseline" in messages and "normalization" in messages
    assert "may differ" in messages
    assert "zero baseline overlap does not guarantee zero final matches" in messages
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert len(result.findings) == 1
    assert result.findings[0].key == ("abc",)
    assert result.findings[0].delta == Decimal("0")
    assert result.exceptions == ()
    assert _configuration_evidence(app) is evidence


@pytest.mark.parametrize(
    "rows_a,rows_b,expected", [(11, 11, True), (10, 10, False), (11, 10, False)]
)
def test_low_overlap_warning_has_strict_ten_percent_boundary_on_both_sides(
    rows_a, rows_b, expected
):
    app = _uploaded_app(
        _key_data(["shared", *[f"A{index}" for index in range(rows_a - 1)]]),
        _key_data(["shared", *[f"B{index}" for index in range(rows_b - 1)]]),
    )
    _simple_mapping(app)
    assert bool(_configuration_warning(app, "very low baseline overlap")) is expected
    assert _configuration_key_table(app).iloc[0]["Overlap rows A / eligible A"] == f"1 / {rows_a}"
    assert _configuration_key_table(app).iloc[0]["Overlap rows B / eligible B"] == f"1 / {rows_b}"
    assert not app.button(key="run").disabled


@pytest.mark.parametrize("eligible,expected", [(8, True), (9, False), (10, False)])
def test_poor_key_eligibility_warning_has_strict_ninety_percent_boundary(eligible, expected):
    app = _uploaded_app(
        _key_data([*[f"K{index}" for index in range(eligible)], *[""] * (10 - eligible)]),
        _key_data([f"K{index}" for index in range(10)]),
    )
    _simple_mapping(app)
    warnings = _configuration_warning(app, "poor baseline key eligibility")
    assert bool(warnings) is expected
    if expected:
        assert "File A" in warnings[0]
        assert str(eligible) in warnings[0] and "10" in warnings[0]
    assert _configuration_key_table(app).iloc[0]["Eligible A / total A"] == f"{eligible} / 10"
    assert not app.button(key="run").disabled


def test_duplicate_warning_tracks_mode_without_recounting_factual_evidence(selected_evidence_calls):
    app = _uploaded_app(_key_data(["K", "K", "K"]), _key_data(["K", "K"]))
    _simple_mapping(app)
    evidence = _configuration_evidence(app)
    assert evidence.key.duplicate_excess_count_a == 2
    assert evidence.key.duplicate_excess_count_b == 1
    assert _configuration_warning(app, "duplicate keys may produce ambiguous findings")
    calls = dict(selected_evidence_calls)
    for mode in (ReconciliationMode.GROUPED_BY_KEY, ReconciliationMode.BOUNDED_ONE_TO_MANY):
        app.radio(key="reconciliation_mode").set_value(mode).run()
        assert _configuration_evidence(app) is evidence
        assert not _configuration_warning(app, "duplicate keys may produce ambiguous findings")
        assert int(_configuration_key_table(app).iloc[0]["Duplicate excess A"]) == 2
        assert selected_evidence_calls == calls
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.UNIQUE).run()
    assert _configuration_warning(app, "duplicate keys may produce ambiguous findings")
    assert selected_evidence_calls == calls


def test_invalid_selected_amounts_warn_without_adding_any_new_run_block():
    app = _uploaded_app(b"id,amount\nA,word\nB,2\n", b"id,amount\nA,1\nB,3\n")
    _simple_mapping(app)
    warnings = _configuration_warning(app, "values incompatible with amount parsing")
    assert len(warnings) == 1 and "File A" in warnings[0]
    assert "1" in warnings[0] and "2" in warnings[0]
    assert _configuration_amount_table(app).iloc[0]["Parseable A / total A"] == "1 / 2"
    assert not app.button(key="run").disabled
    app.button(key="run").click().run()
    assert not app.exception
    assert any(
        "File A" in message.value and "column 'amount'" in message.value for message in app.error
    )
    assert "completed" not in app.session_state


def test_empty_tables_have_no_misleading_eligibility_overlap_or_amount_warnings():
    app = _uploaded_app(b"id,amount\n", b"id,amount\n")
    _simple_mapping(app)
    assert _has_configuration_diagnostics(app)
    for phrase in (
        "no eligible baseline key rows",
        "no exact baseline overlap",
        "very low baseline overlap",
        "poor baseline key eligibility",
        "values incompatible with amount parsing",
    ):
        assert not _configuration_warning(app, phrase)
    assert _configuration_key_table(app).iloc[0]["Eligible A / total A"] == "0 / 0"
    assert _configuration_amount_table(app).iloc[0]["Parseable B / total B"] == "0 / 0"
    assert "no baseline warnings" in _messages(app).lower()
    assert not app.button(key="run").disabled


def test_selected_evidence_cache_ignores_mode_tolerance_normalization_and_secondary_edits(
    selected_evidence_calls,
):
    app = _uploaded_app(DATA_A, DATA_B)
    _simple_mapping(app)
    _comparison(app, "notes", "notes")
    evidence = _configuration_evidence(app)
    suggestions = _bundle(app)
    selected_evidence_calls.clear()
    app.run()
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    app.text_input(key="amount_tolerance").set_value("0.0100").run()
    app.checkbox(key="norm_casefold_0").set_value(True).run()
    app.selectbox(key="map_comparison_a_0").set_value("id")
    app.selectbox(key="map_comparison_b_0").set_value("id").run()
    assert _configuration_evidence(app) is evidence
    assert _bundle(app) is suggestions
    assert selected_evidence_calls == {}
    app.text_input(key="amount_tolerance").set_value("invalid").run()
    assert _has_configuration_diagnostics(app)
    assert _configuration_evidence(app) is evidence
    assert selected_evidence_calls == {}
    assert app.button(key="run").disabled


def test_key_and_amount_mapping_changes_replace_one_cache_and_invalid_selections_clear_it(
    selected_evidence_calls,
):
    app = _uploaded_app(DATA_A, DATA_B)
    _simple_mapping(app)
    original = _configuration_evidence(app)
    selected_evidence_calls.clear()
    app.selectbox(key="map_key_a_0").set_value("notes").run()
    key_changed = _configuration_evidence(app)
    assert key_changed is not original
    assert key_changed.key.key_pairs == (("notes", "id"),)
    assert selected_evidence_calls == {"compare_key_mapping": 1, "compare_amount_columns": 1}
    app.selectbox(key="map_amount_a").set_value("id").run()
    amount_changed = _configuration_evidence(app)
    assert amount_changed is not key_changed
    assert amount_changed.amount.column_a == "id"
    assert selected_evidence_calls == {"compare_key_mapping": 2, "compare_amount_columns": 2}
    assert [key for key in app.session_state if key.startswith("_configuration_")] == [
        "_configuration_evidence"
    ]
    app.selectbox(key="map_key_a_0").set_value(None).run()
    assert not _has_configuration_diagnostics(app)
    assert "_configuration_evidence" not in app.session_state


def test_source_and_worksheet_changes_clear_then_recompute_selected_evidence(xlsx_bytes):
    data = xlsx_bytes(
        {"One": [["id", "amount"], ["INV", 1]], "Two": [["id", "amount"], ["INV", 2]]}
    )
    app = _uploaded_app(DATA_A, data, name_b="multi.xlsx")
    _worksheet(app, "B").set_value("One").run()
    _simple_mapping(app)
    first = _configuration_evidence(app)
    _worksheet(app, "B").set_value("Two").run()
    assert "_configuration_evidence" not in app.session_state
    assert not _has_configuration_diagnostics(app)
    _simple_mapping(app)
    second = _configuration_evidence(app)
    assert second.identity != first.identity
    assert second.identity[1][-1] == "Two"
    app.file_uploader(key="upload_A").set_value(
        ("file_a.csv", DATA_A.replace(b",10,", b",11,"), "text/csv")
    ).run()
    assert "_configuration_evidence" not in app.session_state
    _simple_mapping(app)
    assert _configuration_evidence(app).identity != second.identity
    assert _configuration_evidence(app).amount.total_a == Decimal("11")


def test_displaying_selected_diagnostics_never_ingests_or_reconciles(monkeypatch):
    app = _uploaded_app(DATA_A, DATA_B)

    def fail(*args, **kwargs):
        raise AssertionError("diagnostic display must not ingest or reconcile")

    for name in ("ingest_csv", "ingest_xlsx", "reconcile"):
        monkeypatch.setattr(f"tallydiff.{name}", fail)
    _simple_mapping(app)
    assert _has_configuration_diagnostics(app)
    assert all(app.selectbox(key=f"map_key_{side}_0").value == "id" for side in ("a", "b"))
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.UNIQUE
    assert app.text_input(key="amount_tolerance").value == "0"
    assert all(not app.checkbox(key=f"norm_{name}_0").value for name in RULES)
    _assert_no_result(app)


def test_selected_diagnostic_recount_preserves_completed_review_and_prepared_downloads():
    app = _uploaded_app(DATA_A, DATA_B)
    _simple_mapping(app)
    app.button(key="run").click().run()
    app.button(key=ui._REVIEW_PREPARE_KEY).click().run()
    completed = app.session_state["completed"]
    runtime = app.session_state[ui._REVIEW_STATE_KEY]
    binding = app.session_state[ui._REVIEW_BINDING_KEY]
    prepared = app.session_state[ui._REVIEW_PREPARED_KEY]
    values = _map_values(app)
    prior = _configuration_evidence(app)
    del app.session_state["_configuration_evidence"]
    app.run()
    assert not app.exception
    assert _configuration_evidence(app) is not prior
    assert _configuration_evidence(app).key == prior.key
    assert app.session_state["completed"][1] is completed[1]
    assert app.session_state[ui._REVIEW_STATE_KEY] is runtime
    assert app.session_state[ui._REVIEW_BINDING_KEY] is binding
    assert app.session_state[ui._REVIEW_PREPARED_KEY] is prepared
    assert _map_values(app) == values


def test_profile_changing_only_mode_and_tolerance_reuses_selected_baseline_evidence(
    selected_evidence_calls,
):
    app = _uploaded_app(DATA_A, DATA_B)
    _simple_mapping(app)
    evidence = _configuration_evidence(app)
    selected_evidence_calls.clear()
    profile = export_mapping_profile(
        ColumnMapping((("id", "id"),), "amount", "amount"),
        amount_tolerance=Decimal("0.0100"),
        reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY,
    )
    app.file_uploader(key="profile_upload").set_value(
        ("mapping.json", profile, "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    assert not app.exception
    assert _configuration_evidence(app) is evidence
    assert selected_evidence_calls == {}
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert app.text_input(key="amount_tolerance").value == "0.0100"
