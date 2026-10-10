"""Bounded correspondence is reviewable through the existing Streamlit workflow."""

import csv
import json
from dataclasses import replace
from decimal import Decimal
from io import StringIO

import pytest
from test_app import (
    _assert_result_cleared,
    _exception_downloads,
    _exception_table,
    _review_widget,
    _simple_mapping,
    _uploaded_app,
)
from test_app import review_downloads as review_downloads
from test_app_comparison_state import _editing_state
from test_app_comparisons import _comparisons

import tallydiff
from tallydiff import (
    EXACT_UNIQUE_ONE_TO_MANY_POLICY,
    ColumnMapping,
    CorrespondenceReason,
    CorrespondenceStatus,
    FindingCategory,
    ReconciliationMode,
    Source,
    export_exceptions_csv,
    export_mapping_profile,
    reconcile,
)
from tallydiff.presentation import CORRESPONDENCE_REASON_LABELS, CORRESPONDENCE_STATUS_LABELS

MODE = ReconciliationMode.BOUNDED_ONE_TO_MANY
S = CorrespondenceStatus
R = CorrespondenceReason


def _csv(rows):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("id", "amount", "note"))
    writer.writerows(rows)
    return output.getvalue().encode()


def _data(amounts, key="INV", note="Sales"):
    return _csv((key, amount, note) for amount in amounts)


def _run(data_a, data_b, *, tolerance="0", comparisons=False):
    app = _uploaded_app(data_a, data_b)
    _simple_mapping(app)
    if comparisons:
        _comparisons(app, ("note", "note"))
    app.radio(key="reconciliation_mode").set_value(MODE)
    app.text_input(key="amount_tolerance").set_value(tolerance).run()
    app.button(key="run").click().run()
    assert not app.exception and not app.error
    return app


def _group(a, b, source=Source.A, **kwargs):
    data = (_data(a), _data(b)) if source is Source.A else (_data(b), _data(a))
    return _run(*data, **kwargs)


def _texts(app):
    return "\n".join(item.value for item in (*app.text, *app.caption, *app.markdown))


def _metrics(app):
    return {metric.label: metric.value for metric in app.metric}


def _references(app):
    return [frame.value for frame in app.dataframe if "Source row" in frame.value]


def _raw(app):
    return [frame.value for frame in app.dataframe if "Original value" in frame.value]


def _select(app, index=0):
    app.session_state[_exception_table(app).key] = {
        "selection": {"rows": [index], "columns": [], "cells": []}
    }
    app.run()
    assert not app.exception


def _assert_parent_evidence(app, finding):
    expected = [list(row.raw_fields.values()) for row in (*finding.rows_a, *finding.rows_b)]
    assert [frame["Original value"].tolist() for frame in _raw(app)] == expected
    headings = [item.value for item in app.subheader]
    assert headings.index("Correspondence analysis") < headings.index("File A evidence")
    assert headings.index("Correspondence analysis") < headings.index("File B evidence")
    for frame in _references(app):
        assert list(frame) == ["Source", "Source row", "Amount", "Original key"]


def test_bounded_mode_captions_disclose_exact_search_tolerance_and_secondary_scope():
    app = _uploaded_app(_data(("300",)), _data(("100", "200")))
    _simple_mapping(app)
    disclosure = "Amount tolerance applies only to ordinary one-row-per-side comparisons."
    assert disclosure not in _texts(app)
    app.radio(key="reconciliation_mode").set_value(MODE).run()
    text = _texts(app)
    assert "uniquely determined exact subset" in text and "within fixed search limits" in text
    assert "Inferred correspondence always requires review." in text
    assert disclosure in text and "Subset inference requires exact Decimal equality." in text
    assert "remain Amount mismatch even when their residual delta is within tolerance" in text
    assert (
        "Ordinary deterministic one-row-per-side groups retain normal secondary comparison" in text
    )
    assert "NOT_COMPARABLE" in text
    assert "Secondary fields do not select, rank, or disambiguate financial subsets" in text
    app.text_input(key="amount_tolerance").set_value("0.0100").run()
    assert not app.button(key="run").disabled
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    assert disclosure not in _texts(app)


@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize("residual", [False, True])
def test_inferred_exact_and_residual_groups_are_exceptions_with_structural_details(
    source, residual
):
    candidates = ("100.00", "200.00") + (("0.01",) if residual else ())
    app = _group(("300.00",), candidates, source, tolerance="10")
    result = app.session_state["completed"][1]
    finding = result.findings[0]
    metrics = _metrics(app)
    assert metrics["Analyzed duplicate groups"] == metrics["Inferred correspondences"] == "1"
    assert metrics["Amount mismatch" if residual else "Exact match"] == "1"
    assert result.one_to_many_policy is EXACT_UNIQUE_ONE_TO_MANY_POLICY
    assert "Reconciliation mode: Bounded one-to-many" in _texts(app)
    assert "Inferred correspondence policy: exact_unique_v1" in _texts(app)
    assert "Maximum candidate rows: 12" in _texts(app)
    assert "Maximum planned combinations per run: 1,000,000" in _texts(app)
    assert "Exact amounts only: Yes" in _texts(app)
    assert "Physical-row uniqueness: Yes" in _texts(app)
    assert "Singleton alternatives count as rivals: Yes" in _texts(app)
    assert (
        "does not establish which source is authoritative or prove business transaction identity"
        in _texts(app)
    )
    table = _exception_table(app).value
    assert table["Category"].tolist() == ["Amount mismatch" if residual else "Exact match"]
    assert table["Correspondence status"].tolist() == ["Unique exact subset"]
    assert len(_exception_downloads(app)) == 1
    assert not any("No exceptions to review" in item.value for item in app.success)
    if not residual:
        assert "Exact match" in _review_widget(app, "Exception categories").options
        assert any("Control totals agree" in item.value for item in app.warning)
    _select(app)
    assert "Inferred exact correspondence — review required" in _texts(app)
    references = _references(app)
    assert references[0].to_dict("records") == [
        {
            "Source": f"File {source.value}",
            "Source row": 2,
            "Amount": "300.00",
            "Original key": '["INV"]',
        }
    ]
    opposite = Source.B if source is Source.A else Source.A
    assert references[1]["Source"].tolist() == [f"File {opposite.value}"] * 2
    assert references[1]["Source row"].tolist() == [2, 3]
    assert references[1]["Amount"].tolist() == ["100.00", "200.00"]
    if residual:
        assert "Unassigned residual records" in [item.value for item in app.subheader]
        assert references[2]["Amount"].tolist() == ["0.01"]
        assert "remain part of the parent finding and its complete financial delta" in _texts(app)
    else:
        assert len(references) == 2
    counters = 7 if residual else 3
    for label in ("Planned", "Examined", "Reserved"):
        assert f"{label} combinations: {counters}" in _texts(app)
    assert "Search complete: Yes" in _texts(app)
    _assert_parent_evidence(app, finding)


@pytest.mark.parametrize("source", list(Source))
@pytest.mark.parametrize(
    "a,b,status,reason,complete,message",
    [
        (
            ("300",),
            ("300", "100", "200"),
            S.AMBIGUOUS,
            None,
            False,
            "enumeration stopped once ambiguity was proven",
        ),
        (("300",), ("100", "200", "0"), S.AMBIGUOUS, None, True, "TallyDiff did not choose one"),
        (
            ("300",),
            ("300", "1"),
            S.SINGLETON_ONLY,
            None,
            True,
            "does not promote one-to-one matching inside a duplicate group",
        ),
        (
            ("300",),
            ("100", "199.99"),
            S.NO_EXACT_SUBSET,
            None,
            True,
            "complete bounded search found no physical-row subset",
        ),
        (
            ("13",),
            ("1",) * 13,
            S.BOUND_EXCEEDED,
            R.CANDIDATE_ROW_LIMIT,
            False,
            "policy limit of 12 candidate rows",
        ),
        (
            ("100", "200"),
            ("150", "150"),
            S.NOT_ELIGIBLE,
            R.BOTH_SIDES_MULTIPLE,
            False,
            "does not perform many-to-many allocation or partition search",
        ),
        (
            ("100", "200"),
            (),
            S.NOT_ELIGIBLE,
            R.MISSING_OPPOSITE_SIDE,
            False,
            "lacks an opposite-side anchor",
        ),
    ],
)
def test_unresolved_selected_details_preserve_witnesses_parent_direction_and_raw_rows(
    source, a, b, status, reason, complete, message
):
    app = _group(a, b, source)
    finding = app.session_state["completed"][1].findings[0]
    analysis = finding.correspondence_analysis
    assert _metrics(app)["Analyzed duplicate groups"] == "1"
    assert _metrics(app)["Inferred correspondences"] == "0"
    assert f"{CORRESPONDENCE_STATUS_LABELS[status]}: 1" in _texts(app)
    _select(app)
    text = _texts(app)
    assert f"Status: {CORRESPONDENCE_STATUS_LABELS[status]}" in text
    assert f"Search complete: {'Yes' if complete else 'No'}" in text
    assert message in text
    if reason:
        assert f"Reason: {CORRESPONDENCE_REASON_LABELS[reason]}" in text
    for label, count in (
        ("Planned", analysis.planned_combinations),
        ("Examined", analysis.examined_combinations),
        ("Reserved", analysis.reserved_combinations),
    ):
        assert f"{label} combinations: {count:,}" in text
    references = _references(app)
    eligible_shape = len(a) == 1 and len(b) > 1
    if eligible_shape:
        assert references[0]["Source"].tolist() == [f"File {source.value}"]
    else:
        assert "Search anchor" not in text
    if status is S.AMBIGUOUS:
        assert "Candidate witness 1" in text and "Candidate witness 2" in text
        for index, witness in enumerate(analysis.ambiguity_witnesses, start=1):
            assert references[index][["Source", "Source row"]].values.tolist() == [
                [f"File {row.source.value}", row.source_row]
                for row in (*witness.rows_a, *witness.rows_b)
            ]
        if complete:
            assert "enumeration stopped" not in text
        else:
            assert "At least two exact solutions" in text
    if status is S.SINGLETON_ONLY:
        assert "Singleton candidate witness" in text
        assert "Inferred exact correspondence — review required" not in text
        if source is Source.B:
            assert analysis.ambiguity_witnesses[0].anchor.source is Source.A
            assert references[0]["Source"].tolist() == ["File B"]
    assert "Unassigned parent records" in text
    _assert_parent_evidence(app, finding)


def test_run_budget_bound_shows_unstarted_search_and_exact_counters():
    rows_a = [(f"K{index:03d}", "0", "Sales") for index in range(244)]
    rows_b = [(f"K{index:03d}", "0", "Sales") for index in range(244) for _ in range(12)]
    rows_a.append(("Z_BOUND", "300", "Sales"))
    rows_b.extend(("Z_BOUND", "100", "Sales") for _ in range(12))
    app = _run(_csv(rows_a), _csv(rows_b))
    assert _metrics(app)["Analyzed duplicate groups"] == "245"
    assert "Search bound exceeded: 1" in _texts(app)
    _review_widget(app, "Search matching keys").set_value("Z_BOUND").run()
    _select(app)
    text = _texts(app)
    assert "Reason: Run search budget exhausted" in text
    assert "complete search for this group was not started" in text
    assert "full planned cost could not be reserved under the deterministic per-run budget" in text
    assert "Planned combinations: 4,095" in text
    assert "Examined combinations: 0" in text and "Reserved combinations: 0" in text
    assert "Search complete: No" in text
    assert "Candidate witness" not in text
    _assert_parent_evidence(app, app.session_state["completed"][1].findings[-1])


@pytest.mark.parametrize(
    "amount_b,tolerance,category",
    [
        ("10.00", "0", FindingCategory.EXACT_MATCH),
        ("10.01", "0.01", FindingCategory.WITHIN_TOLERANCE),
        ("11.00", "0.01", FindingCategory.AMOUNT_MISMATCH),
    ],
)
def test_ordinary_bounded_groups_keep_normal_tolerance_and_review_behavior(
    amount_b, tolerance, category
):
    app = _group(("10.00",), (amount_b,), tolerance=tolerance)
    finding = app.session_state["completed"][1].findings[0]
    assert finding.category is category and finding.correspondence_analysis is None
    assert (
        _metrics(app)["Analyzed duplicate groups"]
        == _metrics(app)["Inferred correspondences"]
        == "0"
    )
    if category is FindingCategory.AMOUNT_MISMATCH:
        assert "Correspondence status" not in _exception_table(app).value
        _select(app)
        assert "Correspondence analysis" not in [item.value for item in app.subheader]
    else:
        assert not _exception_downloads(app)
        assert any("No exceptions to review" in item.value for item in app.success)


def test_header_only_bounded_result_displays_policy_zero_summary_and_no_data_message():
    app = _group((), ())
    result = app.session_state["completed"][1]
    assert result.one_to_many_policy is EXACT_UNIQUE_ONE_TO_MANY_POLICY
    assert (
        _metrics(app)["Analyzed duplicate groups"]
        == _metrics(app)["Inferred correspondences"]
        == "0"
    )
    assert "Reconciliation mode: Bounded one-to-many" in _texts(app)
    assert "Inferred correspondence policy: exact_unique_v1" in _texts(app)
    assert any("headers only" in item.value for item in app.info)
    assert not _exception_downloads(app)


def test_mixed_table_and_secondary_details_preserve_comparison_semantics():
    app = _run(
        _csv(
            [
                ("A_EXACT", "300", "different"),
                ("B_RESIDUAL", "300", "Sales"),
                ("C_ORDINARY", "10", "Sales"),
            ]
        ),
        _csv(
            [
                ("A_EXACT", "100", "Sales"),
                ("A_EXACT", "200", "Sales"),
                ("B_RESIDUAL", "100", "Sales"),
                ("B_RESIDUAL", "200", "Sales"),
                ("B_RESIDUAL", "0.01", "Sales"),
                ("C_ORDINARY", "10.01", "Finance"),
            ]
        ),
        tolerance="0.01",
        comparisons=True,
    )
    result = app.session_state["completed"][1]
    table = _exception_table(app).value
    assert table["Category"].tolist() == ["Exact match", "Amount mismatch", "Within tolerance"]
    assert table["Correspondence status"].tolist() == [
        "Unique exact subset",
        "Unique exact subset",
        "",
    ]
    assert list(table)[-2:] == ["Secondary differences", "Correspondence status"]
    assert (
        _metrics(app)["Analyzed duplicate groups"]
        == _metrics(app)["Inferred correspondences"]
        == "2"
    )
    _select(app)
    details = next(
        frame.value for frame in app.dataframe if "File A distinct values" in frame.value
    )
    assert details["Status"].tolist() == ["not_comparable"]
    assert json.loads(details["File A distinct values"][0]) == ["different"]
    assert "were not used to select or disambiguate this correspondence" in _texts(app)
    assert result.findings[0].inferred_solution.candidate_row_count == 2
    _select(app, 2)
    details = next(
        frame.value for frame in app.dataframe if "File A distinct values" in frame.value
    )
    assert details["Status"].tolist() == ["mismatch"]
    assert "Correspondence analysis" not in [item.value for item in app.subheader]


def test_normalized_correspondence_keeps_original_keys_in_references_and_raw_evidence():
    app = _uploaded_app(_data(("300.00",), "ACME"), _data(("100.00", "200.00"), "acme"))
    _simple_mapping(app)
    app.checkbox(key="norm_casefold_0").set_value(True)
    app.radio(key="reconciliation_mode").set_value(MODE).run()
    app.button(key="run").click().run()
    assert not app.exception
    assert _exception_table(app).value["Matching key"].tolist() == ["acme"]
    _select(app)
    assert _references(app)[0]["Original key"].tolist() == ['["ACME"]']
    assert _references(app)[1]["Original key"].tolist() == ['["acme"]', '["acme"]']
    assert [frame["Original value"][0] for frame in _raw(app)] == ["ACME", "acme", "acme"]


@pytest.mark.parametrize(
    "before,after",
    [
        (before, after)
        for before in ReconciliationMode
        for after in ReconciliationMode
        if before is not after
    ],
)
def test_switching_between_any_modes_clears_completed_result(before, after):
    app = _group(("300",), ("100", "200"))
    if before is not MODE:
        app.radio(key="reconciliation_mode").set_value(before).run()
        app.button(key="run").click().run()
    assert "completed" in app.session_state
    app.radio(key="reconciliation_mode").set_value(after).run()
    _assert_result_cleared(app)


@pytest.mark.parametrize(
    "change",
    ["key_a", "key_b", "amount_a", "normalization", "add_comparison", "comparison", "tolerance"],
)
def test_bounded_configuration_edits_clear_stale_result(change):
    app = _group(("300",), ("100", "200"), comparisons=True)
    if change == "key_a":
        app.selectbox(key="map_key_a_0").set_value("note").run()
    elif change == "key_b":
        app.selectbox(key="map_key_b_0").set_value("note").run()
    elif change == "amount_a":
        app.selectbox(key="map_amount_a").set_value("id").run()
    elif change == "normalization":
        app.checkbox(key="norm_casefold_0").set_value(True).run()
    elif change == "add_comparison":
        app.button(key="add_comparison").click().run()
    elif change == "comparison":
        app.selectbox(key="map_comparison_a_0").set_value("id").run()
    else:
        app.text_input(key="amount_tolerance").set_value("0.01").run()
    _assert_result_cleared(app)


@pytest.mark.parametrize("control", ["search", "category", "minimum", "secondary", "sort"])
def test_review_filters_and_selection_preserve_identity_result_and_complete_export(
    monkeypatch, review_downloads, control
):
    calls = []

    def tracked(*args, **kwargs):
        calls.append(kwargs)
        return reconcile(*args, **kwargs)

    monkeypatch.setattr(tallydiff, "reconcile", tracked)
    app = _run(
        _csv([("A_INFERRED", "300", "Sales"), ("B_ORDINARY", "10", "Sales")]),
        _csv(
            [
                ("A_INFERRED", "100", "Sales"),
                ("A_INFERRED", "200", "Sales"),
                ("B_ORDINARY", "12", "Finance"),
            ]
        ),
        comparisons=True,
    )
    identity, result = app.session_state["completed"]
    expected = export_exceptions_csv(result)
    profile = review_downloads["Download mapping profile"]
    _select(app)
    assert review_downloads["Download exception report"] == expected
    if control == "search":
        _review_widget(app, "Search matching keys").set_value("B_ORDINARY").run()
    elif control == "category":
        _review_widget(app, "Exception categories").set_value([FindingCategory.A_ONLY]).run()
    elif control == "minimum":
        _review_widget(app, "Minimum absolute delta").set_value("1").run()
    elif control == "secondary":
        next(
            widget for widget in app.checkbox if widget.label == "Has secondary field mismatch"
        ).set_value(True).run()
    else:
        _review_widget(app, "Exception sort order").set_value("absolute_delta_desc").run()
    assert not app.exception and len(calls) == 1
    assert app.session_state["completed"][0] == identity
    assert app.session_state["completed"][1] is result
    assert review_downloads["Download exception report"] == expected
    assert review_downloads["Download mapping profile"] == profile
    if control != "sort":
        table = _exception_table(app)
        assert table is None or "A_INFERRED" not in table.value["Matching key"].tolist()


@pytest.mark.parametrize("failure", ["policy", "missing_policy", "column"])
def test_invalid_bounded_profiles_leave_completed_configuration_atomic(failure):
    app = _group(("300",), ("100", "200"), comparisons=True)
    before = _editing_state(app)
    completed = app.session_state["completed"]
    document = json.loads(
        export_mapping_profile(
            ColumnMapping((("id", "id"),), "amount", "amount"),
            amount_tolerance=Decimal("100.000"),
            reconciliation_mode=MODE,
        )
    )
    if failure == "policy":
        document["one_to_many_policy"] = "unknown"
    elif failure == "missing_policy":
        del document["one_to_many_policy"]
    else:
        document["key_pairs"][0]["file_a"] = "missing"
    app.file_uploader(key="profile_upload").set_value(
        ("invalid.json", json.dumps(document).encode(), "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    assert not app.exception and app.error
    assert _editing_state(app) == before
    assert app.session_state["completed"][0] == completed[0]
    assert app.session_state["completed"][1] is completed[1]


def test_displayed_policy_limits_and_flags_come_from_completed_result(monkeypatch):
    class PolicyView:
        """A renderer test double; the engine's actual canonical result stays intact."""

        def __init__(self, result):
            self.result = result
            self.one_to_many_policy = replace(
                result.one_to_many_policy,
                policy_id="display_snapshot",
                max_candidate_rows=4,
                max_planned_combinations_per_run=17,
                exact_amounts_only=False,
                physical_row_uniqueness=False,
                singleton_rivals_count=False,
            )

        def __getattr__(self, name):
            return getattr(self.result, name)

    def rendered_result(*args, **kwargs):
        return PolicyView(reconcile(*args, **kwargs))

    monkeypatch.setattr(tallydiff, "reconcile", rendered_result)
    app = _group(("13",), ("1",) * 13)
    text = _texts(app)
    assert "Inferred correspondence policy: display_snapshot" in text
    assert (
        "Maximum candidate rows: 4" in text and "Maximum planned combinations per run: 17" in text
    )
    assert "Exact amounts only: No" in text and "Physical-row uniqueness: No" in text
    assert "Singleton alternatives count as rivals: No" in text
    _select(app)
    assert "policy limit of 4 candidate rows" in _texts(app)
    assert EXACT_UNIQUE_ONE_TO_MANY_POLICY.max_candidate_rows == 12
