"""Secondary comparison wiring, review, and retained evidence in Streamlit."""

import csv
import json
from decimal import Decimal
from io import StringIO

import pytest
from test_app import (
    _exception_downloads,
    _exception_table,
    _review_widget,
    _select_first_finding,
    _simple_mapping,
    _uploaded_app,
)
from test_app import review_downloads as review_downloads

import tallydiff
from tallydiff import (
    ComparisonFieldMapping,
    FieldComparisonStatus,
    FindingCategory,
    ReconciliationMode,
)
from tallydiff.presentation import CATEGORY_LABELS, EXCEPTION_CATEGORIES


def _csv(rows):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _comparisons(app, *pairs):
    for _ in pairs:
        app.button(key="add_comparison").click().run()
    for index, (left, right) in enumerate(pairs):
        app.selectbox(key=f"map_comparison_a_{index}").set_value(left)
        app.selectbox(key=f"map_comparison_b_{index}").set_value(right)
    app.run()
    assert not app.exception
    assert not app.button(key="run").disabled


def _texts(app):
    return "\n".join(item.value for item in (*app.text, *app.caption, *app.markdown)).lower()


def _metrics(app):
    return {metric.label: metric.value for metric in app.metric}


def _secondary_checkbox(app):
    return next(widget for widget in app.checkbox if widget.label == "Has secondary field mismatch")


def _details(app):
    return next(frame.value for frame in app.dataframe if "File A distinct values" in frame.value)


def _evidence(app):
    return [frame.value for frame in app.dataframe if "Original value" in frame.value]


def _download_rows(downloads):
    return list(csv.DictReader(StringIO(downloads["Download exception report"].decode("utf-8"))))


def _review_app():
    app = _uploaded_app(
        b"id,amount,department\nEXACT,10,Sales\nTOLERATED,10,Sales\n"
        b"PRIMARY,9,Sales\nBOTH,9,Sales\nA_ONLY,0,Sales\n",
        b"id,amount,department\nEXACT,10,Support\nTOLERATED,10.01,Support\n"
        b"PRIMARY,1,Sales\nBOTH,1,Support\nB_ONLY,0,Sales\n",
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    assert not app.exception
    return app


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_zero_comparisons_retains_existing_result_and_review_workflow(mode) -> None:
    app = _uploaded_app(b"id,amount\nINV,10\n", b"id,amount\nINV,9\n")
    _simple_mapping(app)
    assert app.session_state["comparison_count"] == 0
    assert not any(widget.key.startswith("map_comparison_") for widget in app.selectbox)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()

    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.comparison_fields == ()
    assert result.findings[0].field_comparisons == ()
    assert _metrics(app)["Amount mismatch"] == "1"
    assert "Secondary field differences" not in _metrics(app)
    assert not any(widget.label == "Has secondary field mismatch" for widget in app.checkbox)
    assert "Secondary differences" not in _exception_table(app).value
    assert _review_widget(app, "Exception categories").value == list(EXCEPTION_CATEGORIES)
    assert "secondary comparisons: exact original evidence" not in _texts(app)


@pytest.mark.parametrize(
    ("amount_b", "tolerance", "category", "delta"),
    [
        ("10", "0", FindingCategory.EXACT_MATCH, "0"),
        ("10.01", "0.01", FindingCategory.WITHIN_TOLERANCE, "-0.01"),
    ],
)
def test_unique_secondary_mismatch_requires_review_without_changing_amounts(
    review_downloads, amount_b, tolerance, category, delta
) -> None:
    app = _uploaded_app(
        b"id,amount,Department\nINV,10,Sales\n",
        f"id,amount,Cost Center\nINV,{amount_b},Support\n".encode(),
    )
    _simple_mapping(app)
    _comparisons(app, ("Department", "Cost Center"))
    app.text_input(key="amount_tolerance").set_value(tolerance).run()
    app.button(key="run").click().run()

    assert not app.exception
    result = app.session_state["completed"][1]
    assert len(result.exceptions) == 1
    assert result.exceptions[0].category is category
    assert result.exceptions[0].delta == Decimal(delta)
    assert result.tolerated_findings == ()
    assert _metrics(app)[CATEGORY_LABELS[category]] == "1"
    assert _metrics(app)["Secondary field differences"] == "1"
    assert "Net accepted variance (A - B)" not in _metrics(app)
    table = _exception_table(app).value
    assert table["Category"].tolist() == [CATEGORY_LABELS[category]]
    assert table["Delta (A - B)"].tolist() == [delta]
    assert table["Secondary differences"].tolist() == ["Department ↔ Cost Center"]
    assert not any(frame.key and frame.key.startswith("tolerated_") for frame in app.dataframe)
    assert any("require review" in warning.value for warning in app.warning)
    if category is FindingCategory.EXACT_MATCH:
        assert any("Control totals agree" in warning.value for warning in app.warning)
    assert len(_exception_downloads(app)) == 1
    exported = _download_rows(review_downloads)
    assert exported[0]["Category"] == CATEGORY_LABELS[category]
    assert exported[0]["Delta (A - B)"] == delta
    assert exported[0]["Comparison 1 status"] == "mismatch"
    assert json.loads(exported[0]["Comparison 1 File A values — Department"]) == ["Sales"]
    disclosure = _texts(app)
    assert (
        "comparison 1:" in disclosure and "department" in disclosure and "cost center" in disclosure
    )
    assert "exact original evidence" in disclosure
    assert "financial delta" in disclosure
    assert "one-row-per-side" in disclosure
    assert "amount/presence" in disclosure
    _select_first_finding(app)
    details = _details(app)
    assert details["File A field"].tolist() == ["Department"]
    assert details["File B field"].tolist() == ["Cost Center"]
    assert details["Status"].tolist() == ["mismatch"]
    assert [json.loads(value) for value in details["File A distinct values"]] == [["Sales"]]
    assert [frame["Original value"].tolist() for frame in _evidence(app)] == [
        ["INV", "10", "Sales"],
        ["INV", amount_b, "Support"],
    ]


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(("amount_b", "tolerance"), [("10", "0"), ("10.01", "0.01")])
def test_matching_secondary_fields_are_accepted_where_comparable(mode, amount_b, tolerance) -> None:
    app = _uploaded_app(
        b"id,amount,department\nINV,10,Sales\n",
        f"id,amount,department\nINV,{amount_b},Sales\n".encode(),
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.radio(key="reconciliation_mode").set_value(mode)
    app.text_input(key="amount_tolerance").set_value(tolerance).run()
    app.button(key="run").click().run()

    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.exceptions == ()
    assert result.findings[0].field_comparisons[0].status is FieldComparisonStatus.MATCH
    assert _metrics(app)["Secondary field differences"] == "0"
    assert not _exception_downloads(app)
    assert not any(widget.label == "Has secondary field mismatch" for widget in app.checkbox)
    assert any(
        "secondary" in message.value.lower() and "comparable" in message.value.lower()
        for message in app.success
    )
    if tolerance != "0":
        tolerated = next(frame for frame in app.dataframe if frame.key.startswith("tolerated_"))
        assert tolerated.value["Matching key"].tolist() == ["INV"]
        assert "secondary mismatch" in _texts(app)


def test_grouped_distinct_sets_and_independent_fields_do_not_duplicate_exact_exceptions() -> None:
    app = _uploaded_app(
        b"id,amount,department,currency\nREPEAT,10,Sales,USD\n"
        b"BAD,4,Sales,USD\nBAD,6,Support,EUR\n"
        b"ASSOCIATION,4,Sales,USD\nASSOCIATION,6,Support,EUR\n",
        b"id,amount,department,currency\nREPEAT,4,Sales,USD\nREPEAT,6,Sales,USD\n"
        b"BAD,10,Sales,USD\nASSOCIATION,4,Sales,EUR\nASSOCIATION,6,Support,USD\n",
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"), ("currency", "currency"))
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    app.button(key="run").click().run()

    assert not app.exception
    result = app.session_state["completed"][1]
    assert [finding.key for finding in result.exceptions] == [("BAD",)]
    assert _metrics(app)["Exact match"] == "3"
    assert _metrics(app)["Secondary field differences"] == "1"
    assert _exception_table(app).value["Matching key"].tolist() == ["BAD"]
    exact = next(frame for frame in app.dataframe if frame.key.startswith("exact_"))
    assert exact.value["Matching key"].tolist() == ["ASSOCIATION", "REPEAT"]
    assert "BAD" not in exact.value["Matching key"].tolist()
    disclosure = _texts(app)
    assert "distinct values" in disclosure or "distinct value sets" in disclosure
    for limitation in (
        "row correspondence",
        "occurrence counts",
        "cross-field combinations",
        "amount allocation",
    ):
        assert limitation in disclosure
    assert "do not" in disclosure
    _select_first_finding(app)
    details = _details(app)
    assert details["Status"].tolist() == ["mismatch", "mismatch"]
    assert [json.loads(value) for value in details["File A distinct values"]] == [
        ["Sales", "Support"],
        ["EUR", "USD"],
    ]
    assert len(_evidence(app)) == 3


def test_ordered_comparisons_keep_matching_and_mismatching_fields_associated(
    review_downloads,
) -> None:
    app = _uploaded_app(
        b"id,amount,Department,Currency\nINV,10,Sales,USD\n",
        b"id,amount,Cost Center,ISO Code\nINV,10,Support,USD\n",
    )
    _simple_mapping(app)
    _comparisons(app, ("Currency", "ISO Code"), ("Department", "Cost Center"))
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.comparison_fields == (
        ComparisonFieldMapping("Currency", "ISO Code"),
        ComparisonFieldMapping("Department", "Cost Center"),
    )
    assert _exception_table(app).value["Secondary differences"].tolist() == [
        "Department ↔ Cost Center"
    ]
    disclosure = _texts(app)
    assert disclosure.index("comparison 1: currency") < disclosure.index("comparison 2: department")
    exported = _download_rows(review_downloads)[0]
    assert exported["Comparison 1 status"] == "match"
    assert exported["Comparison 2 status"] == "mismatch"
    assert exported["Secondary differences"] == "Comparison 2: Department ↔ Cost Center"
    assert json.loads(exported["Comparison 1 File B values — ISO Code"]) == ["USD"]
    _select_first_finding(app)
    details = _details(app)
    assert details["Comparison"].tolist() == [1, 2]
    assert details["File A field"].tolist() == ["Currency", "Department"]
    assert details["File B field"].tolist() == ["ISO Code", "Cost Center"]
    assert details["Status"].tolist() == ["match", "mismatch"]
    assert [json.loads(value) for value in details["File B distinct values"]] == [
        ["USD"],
        ["Support"],
    ]
    assert len(_evidence(app)) == 2
    detail_index = next(
        index
        for index, frame in enumerate(app.dataframe)
        if "File A distinct values" in frame.value
    )
    evidence_index = next(
        index for index, frame in enumerate(app.dataframe) if "Original value" in frame.value
    )
    assert detail_index < evidence_index


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_normalized_matching_key_preserves_original_secondary_comparison(mode) -> None:
    app = _uploaded_app(b"id,amount\nACME-01,10\n", b"id,amount\nACME01,10\n")
    _simple_mapping(app)
    _comparisons(app, ("id", "id"))
    app.checkbox(key="norm_remove_punctuation_0").set_value(True)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()

    assert not app.exception
    finding = app.session_state["completed"][1].exceptions[0]
    assert finding.key == ("ACME01",)
    assert finding.category is FindingCategory.EXACT_MATCH
    assert finding.field_comparisons[0].values_a == ("ACME-01",)
    assert finding.field_comparisons[0].values_b == ("ACME01",)
    assert _exception_table(app).value["Secondary differences"].tolist() == ["id ↔ id"]
    assert "matching keys may be normalized" in _texts(app)
    assert "exact original evidence" in _texts(app)
    _select_first_finding(app)
    details = _details(app)
    assert json.loads(details.iloc[0]["File A distinct values"]) == ["ACME-01"]
    assert _evidence(app)[0]["Original value"].tolist() == ["ACME-01", "10"]


@pytest.mark.parametrize(
    ("format_a", "format_b"), [("csv", "csv"), ("csv", "xlsx"), ("xlsx", "csv"), ("xlsx", "xlsx")]
)
def test_directional_columns_reach_each_ingester_and_engine(
    monkeypatch, xlsx_bytes, format_a, format_b
) -> None:
    observed = {"ingest": [], "engine": []}

    def capture_ingester(kind, original):
        def wrapped(*args, **kwargs):
            observed["ingest"].append((kind, kwargs["source"], kwargs["comparison_columns"]))
            return original(*args, **kwargs)

        return wrapped

    def capture_engine(*args, **kwargs):
        observed["engine"].append(kwargs["comparison_fields"])
        return tallydiff_engine(*args, **kwargs)

    tallydiff_engine = tallydiff.reconcile
    monkeypatch.setattr(tallydiff, "ingest_csv", capture_ingester("csv", tallydiff.ingest_csv))
    monkeypatch.setattr(tallydiff, "ingest_xlsx", capture_ingester("xlsx", tallydiff.ingest_xlsx))
    monkeypatch.setattr(tallydiff, "reconcile", capture_engine)
    rows_a = [["id", "amount", "Department"], ["INV", 10, "Café"]]
    rows_b = [["id", "amount", "Cost Center"], ["INV", 10, "Cafe\u0301"]]
    data_a = _csv(rows_a) if format_a == "csv" else xlsx_bytes({"Ledger": rows_a})
    data_b = _csv(rows_b) if format_b == "csv" else xlsx_bytes({"Ledger": rows_b})
    app = _uploaded_app(data_a, data_b, name_a=f"file_a.{format_a}", name_b=f"file_b.{format_b}")
    _simple_mapping(app)
    _comparisons(app, ("Department", "Cost Center"))
    app.button(key="run").click().run()

    assert not app.exception
    assert observed["ingest"] == [
        (format_a, tallydiff.Source.A, ("Department",)),
        (format_b, tallydiff.Source.B, ("Cost Center",)),
    ]
    assert observed["engine"] == [(ComparisonFieldMapping("Department", "Cost Center"),)]
    assert _metrics(app)["Secondary field differences"] == "1"
    finding = app.session_state["completed"][1].exceptions[0]
    assert finding.field_comparisons[0].values_a == ("Café",)
    assert finding.field_comparisons[0].values_b == ("Cafe\u0301",)


@pytest.mark.parametrize("source", ["A", "B"])
@pytest.mark.parametrize("value", ["=1+1", "#N/A"])
def test_selected_xlsx_secondary_formula_or_error_blocks_completed_result(
    xlsx_bytes, source, value
) -> None:
    workbook = xlsx_bytes({"Ledger": [["id", "amount", "department"], ["INV", 10, value]]})
    csv_data = b"id,amount,department\nINV,10,Sales\n"
    app = _uploaded_app(
        workbook if source == "A" else csv_data,
        workbook if source == "B" else csv_data,
        name_a="book.xlsx" if source == "A" else "file_a.csv",
        name_b="book.xlsx" if source == "B" else "file_b.csv",
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.button(key="run").click().run()

    assert not app.exception
    assert "completed" not in app.session_state
    assert not app.metric and not _exception_downloads(app)
    assert len(app.error) == 1
    context = app.error[0].value
    assert f"File {source}" in context and "Ledger" in context
    assert "2" in context and "department" in context and value in context
    assert "formula" in context.lower() if value.startswith("=") else "error" in context.lower()


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_header_only_inputs_keep_comparison_configuration_without_crashing(
    review_downloads, xlsx_bytes, mode, format
) -> None:
    rows = [["id", "amount", "department"]]
    data = _csv(rows) if format == "csv" else xlsx_bytes({"Ledger": rows})
    app = _uploaded_app(data, data, name_a=f"file_a.{format}", name_b=f"file_b.{format}")
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.radio(key="reconciliation_mode").set_value(mode).run()
    assert json.loads(review_downloads["Download mapping profile"])["comparison_fields"] == [
        {"file_a": "department", "file_b": "department"}
    ]
    app.button(key="run").click().run()

    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.comparison_fields == (ComparisonFieldMapping("department", "department"),)
    assert result.findings == ()
    assert any("no data records" in message.value for message in app.info)
    assert not _exception_downloads(app) and not app.dataframe
    assert "comparison 1: department" in _texts(app)


def test_exception_categories_and_secondary_filter_default_show_all_exceptions() -> None:
    app = _review_app()
    categories = _review_widget(app, "Exception categories")
    assert categories.value == [
        *EXCEPTION_CATEGORIES,
        FindingCategory.EXACT_MATCH,
        FindingCategory.WITHIN_TOLERANCE,
    ]
    assert categories.options == [CATEGORY_LABELS[category] for category in categories.value]
    assert not _secondary_checkbox(app).value
    assert _exception_table(app).value["Matching key"].tolist() == [
        "A_ONLY",
        "BOTH",
        "B_ONLY",
        "EXACT",
        "PRIMARY",
        "TOLERATED",
    ]
    assert _metrics(app)["Secondary field differences"] == "3"
    assert _metrics(app)["Exact match"] == "1"
    assert _metrics(app)["Within tolerance"] == "1"


@pytest.mark.parametrize(
    ("control", "value", "expected"),
    [
        ("secondary", True, ["BOTH", "EXACT", "TOLERATED"]),
        ("Search matching keys", "exact", ["EXACT"]),
        ("Exception categories", [FindingCategory.EXACT_MATCH], ["EXACT"]),
        ("Minimum absolute delta", "0.001", ["BOTH", "PRIMARY", "TOLERATED"]),
        (
            "Exception sort order",
            "absolute_delta_desc",
            ["BOTH", "PRIMARY", "TOLERATED", "A_ONLY", "B_ONLY", "EXACT"],
        ),
    ],
)
def test_review_controls_do_not_mutate_complete_result_profile_or_exception_export(
    review_downloads, control, value, expected
) -> None:
    app = _review_app()
    completed = app.session_state["completed"]
    metrics = _metrics(app)
    original_export = review_downloads["Download exception report"]
    original_profile = review_downloads["Download mapping profile"]
    widget = _secondary_checkbox(app) if control == "secondary" else _review_widget(app, control)
    widget.set_value(value).run()

    assert not app.exception
    assert app.session_state["completed"][0] == completed[0]
    assert app.session_state["completed"][1] is completed[1]
    assert _metrics(app) == metrics
    assert review_downloads["Download exception report"] == original_export
    assert review_downloads["Download mapping profile"] == original_profile
    assert _exception_table(app).value["Matching key"].tolist() == expected
    assert [row["Matching key"] for row in _download_rows(review_downloads)] == [
        "A_ONLY",
        "BOTH",
        "B_ONLY",
        "EXACT",
        "PRIMARY",
        "TOLERATED",
    ]


def test_secondary_filter_changes_table_identity_and_resets_selected_evidence(
    review_downloads,
) -> None:
    app = _review_app()
    table = _exception_table(app)
    app.session_state[table.key] = {"selection": {"rows": [4], "columns": [], "cells": []}}
    app.run()
    assert any(item.value == "Selected key: PRIMARY" for item in app.text)
    assert len(_evidence(app)) == 2
    exported = review_downloads["Download exception report"]

    _secondary_checkbox(app).set_value(True).run()

    assert not app.exception
    assert _exception_table(app).key != table.key
    assert not any(item.value.startswith("Selected key:") for item in app.text)
    assert not _evidence(app)
    assert review_downloads["Download exception report"] == exported
    _select_first_finding(app)
    assert any(item.value == "Selected key: BOTH" for item in app.text)
    _review_widget(app, "Minimum absolute delta").set_value("0.02").run()
    assert _exception_table(app).value["Matching key"].tolist() == ["BOTH"]
    _review_widget(app, "Exception categories").set_value([FindingCategory.EXACT_MATCH]).run()
    assert _exception_table(app) is None
    assert any("No exception groups match" in item.value for item in app.info)
    assert review_downloads["Download exception report"] == exported


@pytest.mark.parametrize(
    ("amount_b", "tolerance", "secondary", "additional"),
    [
        ("10", "0", "Support", [FindingCategory.EXACT_MATCH]),
        ("10.01", "0.01", "Support", [FindingCategory.WITHIN_TOLERANCE]),
        ("9", "0", "Sales", []),
    ],
)
def test_review_category_extensions_exist_only_for_supplied_secondary_exceptions(
    amount_b, tolerance, secondary, additional
) -> None:
    app = _uploaded_app(
        b"id,amount,department\nINV,10,Sales\n",
        f"id,amount,department\nINV,{amount_b},{secondary}\n".encode(),
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.text_input(key="amount_tolerance").set_value(tolerance).run()
    app.button(key="run").click().run()
    assert not app.exception
    categories = _review_widget(app, "Exception categories")
    assert categories.value == [*EXCEPTION_CATEGORIES, *additional]
    assert categories.options == [CATEGORY_LABELS[category] for category in categories.value]


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_selected_detail_preserves_unicode_blank_and_absent_side_as_distinct_json(mode) -> None:
    app = _uploaded_app(
        _csv([["id", "amount", "department"], ["A_ONLY", 0, ""], ["BOTH", 10, "Café\n東京"]]),
        _csv([["id", "amount", "department"], ["BOTH", 10, "Cafe\u0301\n東京"]]),
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    _select_first_finding(app)
    assert not app.exception
    details = _details(app)
    assert details.iloc[0]["File A distinct values"] == '[""]'
    assert details.iloc[0]["File B distinct values"] == "[]"
    assert details.iloc[0]["Status"] == "not_comparable"
    assert len(_evidence(app)) == 1
    assert _evidence(app)[0]["Original value"].tolist() == ["A_ONLY", "0", ""]
    assert _metrics(app)["Secondary field differences"] == "1"
    _secondary_checkbox(app).set_value(True).run()
    _select_first_finding(app)
    details = _details(app)
    assert details.iloc[0]["Status"] == "mismatch"
    assert json.loads(details.iloc[0]["File A distinct values"]) == ["Café\n東京"]
    assert json.loads(details.iloc[0]["File B distinct values"]) == ["Cafe\u0301\n東京"]
    assert "東京" in details.iloc[0]["File A distinct values"]
    assert [frame["Original value"].iloc[-1] for frame in _evidence(app)] == [
        "Café\n東京",
        "Cafe\u0301\n東京",
    ]


def test_unique_duplicates_keep_not_comparable_details_and_every_source_row(
    review_downloads,
) -> None:
    app = _uploaded_app(
        b"id,amount,department\nINV,4,Sales\nINV,6,Support\n",
        b"id,amount,department\nINV,10,Sales\n",
    )
    _simple_mapping(app)
    _comparisons(app, ("department", "department"))
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    finding = result.exceptions[0]
    assert finding.category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert finding.field_comparisons[0].status is FieldComparisonStatus.NOT_COMPARABLE
    assert _metrics(app)["Secondary field differences"] == "0"
    assert _exception_table(app).value["Secondary differences"].tolist() == [""]
    exported = _download_rows(review_downloads)[0]
    assert exported["Comparison 1 status"] == "not_comparable"
    assert json.loads(exported["Comparison 1 File A values — department"]) == ["Sales", "Support"]
    _select_first_finding(app)
    details = _details(app)
    assert details["Status"].tolist() == ["not_comparable"]
    assert len(_evidence(app)) == 3
    assert [frame["Original value"].tolist() for frame in _evidence(app)] == [
        ["INV", "4", "Sales"],
        ["INV", "6", "Support"],
        ["INV", "10", "Sales"],
    ]
    _secondary_checkbox(app).set_value(True).run()
    assert _exception_table(app) is None
    assert any("No exception groups match" in item.value for item in app.info)
    assert _download_rows(review_downloads)[0] == exported
