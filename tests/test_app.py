import csv
import json
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tallydiff import (
    ColumnMapping,
    FindingCategory,
    ReconciliationIntegrityError,
    ReconciliationMode,
    export_mapping_profile,
    load_mapping_profile,
    reconcile,
)
from tallydiff.presentation import EXCEPTION_CATEGORIES

ROOT = Path(__file__).resolve().parents[1]


def _exception_downloads(app: AppTest) -> list:
    return [button for button in app.download_button if button.label == "Download exception report"]


def _uploaded_app(
    data_a: bytes, data_b: bytes, *, name_a="file_a.csv", name_b="file_b.csv"
) -> AppTest:
    app = AppTest.from_file(str(ROOT / "src/tallydiff/app.py"), default_timeout=15).run()
    assert not app.exception
    assert not _exception_downloads(app)
    app.file_uploader(key="upload_A").set_value(
        (
            name_a,
            data_a,
            "text/csv"
            if name_a.endswith(".csv")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )
    app.file_uploader(key="upload_B").set_value(
        (
            name_b,
            data_b,
            "text/csv"
            if name_b.endswith(".csv")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )
    return app.run()


def _simple_mapping(app: AppTest) -> None:
    for key, value in (
        ("map_key_a_0", "id"),
        ("map_key_b_0", "id"),
        ("map_amount_a", "amount"),
        ("map_amount_b", "amount"),
    ):
        app.selectbox(key=key).set_value(value)
    app.run()


def _select_first_finding(app: AppTest) -> None:
    app.session_state[app.dataframe[0].key] = {
        "selection": {"rows": [0], "columns": [], "cells": []}
    }
    app.run()


def test_sample_workflow_evidence_and_stale_result_invalidation() -> None:
    data_a = (ROOT / "sample_data/file_a.csv").read_bytes()
    data_b = (ROOT / "sample_data/file_b.csv").read_bytes()
    app = _uploaded_app(data_a, data_b)
    assert all(widget.value is None for widget in app.selectbox)
    assert app.button(key="run").disabled
    app.button(key="add_key").click().run()
    for key, value in (
        ("map_key_a_0", "Vendor ID"),
        ("map_key_b_0", "Supplier"),
        ("map_key_a_1", "Invoice Number"),
        ("map_key_b_1", "Invoice Ref"),
        ("map_amount_a", "Invoice Amount"),
        ("map_amount_b", "Gross Amount"),
    ):
        app.selectbox(key=key).set_value(value)
    app.run()
    assert not app.button(key="run").disabled
    assert not _exception_downloads(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert {metric.label: metric.value for metric in app.metric} == {
        "File A control total": "2550",
        "File B control total": "2305",
        "Net difference (A - B)": "+245",
        "Exact match": "1",
        "Within tolerance": "0",
        "Amount mismatch": "1",
        "File A only": "1",
        "File B only": "1",
        "Duplicate / ambiguous": "0",
    }
    assert len(_exception_downloads(app)) == 1
    assert _exception_downloads(app)[0].label == "Download exception report"
    table = app.dataframe[0].value
    assert table["Matching key"].tolist() == ["V001 / 1042", "V003 / 1044", "V004 / 1045"]
    assert table["Delta (A - B)"].tolist() == ["+45", "+500", "-300"]
    _select_first_finding(app)
    assert len(app.metric) == 9  # Evidence reruns retain the current result.
    assert len(_exception_downloads(app)) == 1
    assert app.dataframe[1].value["Original value"].tolist() == ["V001", "1042", "1250"]
    assert app.dataframe[2].value["Original value"].tolist() == ["V001", "1042", "1205"]
    assert [item.value for item in app.caption].count("Source record 2") == 2

    app.selectbox(key="map_key_a_0").set_value("Invoice Number").run()
    assert not app.metric
    assert not _exception_downloads(app)
    assert app.button(key="run").disabled  # Duplicate key selection cannot run.
    app.selectbox(key="map_key_a_0").set_value("Vendor ID").run()
    assert not app.metric  # Restoring the old mapping still requires a fresh run.
    assert not _exception_downloads(app)
    app.button(key="run").click().run()
    app.selectbox(key="map_amount_a").set_value("Invoice Number").run()
    assert not app.metric
    assert not _exception_downloads(app)
    app.selectbox(key="map_amount_a").set_value("Invoice Amount").run()
    app.button(key="run").click().run()
    app.file_uploader(key="upload_A").set_value(
        ("file_a.csv", data_a.replace(b"1250", b"999"), "text/csv")
    ).run()
    assert not app.metric
    assert not _exception_downloads(app)
    assert all(widget.value is None for widget in app.selectbox)
    assert not app.exception


def test_zero_delta_duplicates_still_show_warning_and_every_evidence_row() -> None:
    app = _uploaded_app(b"id,amount\nINV,100\nINV,200\n", b"id,amount\nINV,150\nINV,150\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert len(_exception_downloads(app)) == 1
    assert any("still require review" in warning.value for warning in app.warning)
    assert {metric.label: metric.value for metric in app.metric}["Duplicate / ambiguous"] == "1"
    _select_first_finding(app)
    assert len(app.dataframe) == 5
    assert [frame.value["Original value"].tolist() for frame in app.dataframe[1:]] == [
        ["INV", "100"],
        ["INV", "200"],
        ["INV", "150"],
        ["INV", "150"],
    ]


def test_ingestion_errors_from_both_files_block_results() -> None:
    app = _uploaded_app(b"id,amount\nA,NaN\n", b"id,amount\nB,abc\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert len(app.error) == 2
    assert "File A, source record 2" in app.error[0].value
    assert "File B, source record 2" in app.error[1].value
    assert not app.metric
    assert not _exception_downloads(app)


def test_integrity_failure_is_displayed_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_integrity(*args, **kwargs):
        raise ReconciliationIntegrityError("synthetic integrity failure")

    monkeypatch.setattr("tallydiff.reconcile", fail_integrity)
    app = _uploaded_app(b"id,amount\nA,1\n", b"id,amount\nA,1\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert "Reconciliation integrity check failed" in app.error[0].value
    assert not app.metric
    assert not _exception_downloads(app)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("data", [b"id,amount\n", b"id,amount\nMATCH,1.00\n"])
def test_no_exception_download_for_empty_or_exact_results(
    data: bytes, mode: ReconciliationMode
) -> None:
    app = _uploaded_app(data, data)
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    assert not _exception_downloads(app)

    app.button(key="run").click().run()

    assert not app.exception
    assert app.metric
    assert not _exception_downloads(app)


def test_tolerance_default_summary_separate_evidence_and_stale_download() -> None:
    app = _uploaded_app(
        b"id,amount\nEXACT,100.00\nINSIDE,100.00\nOUTSIDE,100.00\n",
        b"id,amount\nEXACT,100.00\nINSIDE,100.01\nOUTSIDE,100.02\n",
    )
    _simple_mapping(app)
    assert app.text_input(key="amount_tolerance").value == "0"
    app.button(key="run").click().run()
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Exact match"] == "1"
    assert metrics["Within tolerance"] == "0"
    assert metrics["Amount mismatch"] == "2"
    assert app.dataframe[0].value["Matching key"].tolist() == ["INSIDE", "OUTSIDE"]
    assert len(_exception_downloads(app)) == 1

    app.text_input(key="amount_tolerance").set_value("0.01").run()
    assert not app.metric and not _exception_downloads(app)
    app.text_input(key="amount_tolerance").set_value("0").run()
    assert not app.metric and not _exception_downloads(
        app
    )  # Restoring tolerance needs a fresh run.
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert (
        metrics["Exact match"] == metrics["Within tolerance"] == metrics["Amount mismatch"] == "1"
    )
    assert metrics["Net difference (A - B)"] == "-0.03"
    assert metrics["Net accepted variance (A - B)"] == "-0.01"
    assert any("Amount tolerance: 0.01" in item.value for item in app.text)
    assert app.dataframe[0].value["Matching key"].tolist() == ["OUTSIDE"]
    assert app.dataframe[1].value["Category"].tolist() == ["Within tolerance"]
    assert app.dataframe[1].value["Delta (A - B)"].tolist() == ["-0.01"]
    assert any("1 key groups require review" in item.value for item in app.warning)
    assert len(_exception_downloads(app)) == 1
    app.session_state[app.dataframe[1].key] = {
        "selection": {"rows": [0], "columns": [], "cells": []}
    }
    app.run()
    assert app.dataframe[2].value["Original value"].tolist() == ["INSIDE", "100.00"]
    assert app.dataframe[3].value["Original value"].tolist() == ["INSIDE", "100.01"]
    assert len(_exception_downloads(app)) == 1

    app.text_input(key="amount_tolerance").set_value("0.02").run()
    assert not app.metric and not _exception_downloads(app)
    assert not app.dataframe
    app.button(key="run").click().run()
    assert not app.exception and not _exception_downloads(app)
    assert "Reconciled within configured tolerance" in app.success[0].value
    assert {metric.label: metric.value for metric in app.metric}["Within tolerance"] == "2"


@pytest.mark.parametrize("text", ["-0.01", "NaN", "Infinity", "", "abc"])
def test_invalid_tolerance_blocks_run_and_clears_old_result(text: str) -> None:
    app = _uploaded_app(b"id,amount\nINV,100.00\n", b"id,amount\nINV,100.01\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert app.metric and _exception_downloads(app)
    app.text_input(key="amount_tolerance").set_value(text).run()
    assert not app.exception
    assert app.button(key="run").disabled
    assert "Amount tolerance:" in app.error[0].value
    assert not app.metric and not _exception_downloads(app) and not app.dataframe
    app.text_input(key="amount_tolerance").set_value("0").run()
    assert not app.button(key="run").disabled
    assert not app.metric and not _exception_downloads(app)


@pytest.mark.parametrize("offsetting", [False, True])
def test_only_accepted_variance_is_success_without_exact_equality_claim_or_download(
    offsetting: bool,
) -> None:
    app = _uploaded_app(
        b"id,amount\nA,100.00\nB,100.00\n",
        b"id,amount\nA,100.01\nB,99.99\n" if offsetting else b"id,amount\nA,100.01\nB,100.00\n",
    )
    _simple_mapping(app)
    app.text_input(key="amount_tolerance").set_value("$0.01").run()
    app.button(key="run").click().run()
    assert not app.exception and not app.warning and not _exception_downloads(app)
    assert "Reconciled within configured tolerance" in app.success[0].value
    assert "exactly" not in app.success[0].value
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Within tolerance"] == ("2" if offsetting else "1")
    assert metrics["Exact match"] == ("0" if offsetting else "1")
    assert metrics["Net accepted variance (A - B)"] == ("0.00" if offsetting else "-0.01")
    assert app.dataframe[0].value["Delta (A - B)"].tolist() == (
        ["-0.01", "+0.01"] if offsetting else ["-0.01"]
    )


@pytest.fixture
def downloaded_profile(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured = {}
    original = st.download_button

    def capture(label, *args, **kwargs):
        if label == "Download mapping profile":
            captured.update(data=kwargs["data"], filename=kwargs["file_name"])
        return original(label, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture)
    return captured


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_profile_download_apply_sample_round_trip_and_manual_edits(
    downloaded_profile: dict, caplog: pytest.LogCaptureFixture, mode: ReconciliationMode
) -> None:
    app = _uploaded_app(
        (ROOT / "sample_data/file_a.csv").read_bytes(),
        (ROOT / "sample_data/file_b.csv").read_bytes(),
    )
    assert app.button(key="apply_profile").disabled
    assert not app.download_button
    app.button(key="add_key").click().run()
    values = {
        "map_key_a_0": "Vendor ID",
        "map_key_b_0": "Supplier",
        "map_key_a_1": "Invoice Number",
        "map_key_b_1": "Invoice Ref",
        "map_amount_a": "Invoice Amount",
        "map_amount_b": "Gross Amount",
    }
    for key, value in values.items():
        app.selectbox(key=key).set_value(value)
    app.radio(key="reconciliation_mode").set_value(mode)
    app.text_input(key="amount_tolerance").set_value("0.0100").run()
    assert app.download_button(key="download_profile").label == "Download mapping profile"
    data = downloaded_profile["data"]
    assert downloaded_profile["filename"] == "tallydiff_profile.json"
    assert load_mapping_profile(data).amount_tolerance.as_tuple() == Decimal("0.0100").as_tuple()
    assert json.loads(data)["version"] == 2
    assert json.loads(data)["reconciliation_mode"] == mode.value
    app.button(key="run").click().run()
    original_identity = app.session_state["completed"][0]
    app.download_button(key="download_profile").click().run()
    assert app.session_state["completed"][0] == original_identity
    assert downloaded_profile["data"] == data
    assert not any(
        value in data for value in (b"V001", b"1250", b"2550", b"file_a.csv", b"findings")
    )

    app.button(key="remove_key").click().run()
    app.selectbox(key="map_amount_a").set_value("Invoice Number").run()
    other_mode = (
        ReconciliationMode.GROUPED_BY_KEY
        if mode is ReconciliationMode.UNIQUE
        else ReconciliationMode.UNIQUE
    )
    app.radio(key="reconciliation_mode").set_value(other_mode)
    app.text_input(key="amount_tolerance").set_value("2").run()
    app.file_uploader(key="profile_upload").set_value(
        ("next_month.json", data, "application/json")
    ).run()
    assert (
        app.selectbox(key="map_amount_a").value == "Invoice Number"
    )  # Selection alone does not apply.
    assert app.text_input(key="amount_tolerance").value == "2"
    assert app.radio(key="reconciliation_mode").value is other_mode
    app.button(key="apply_profile").click().run()
    assert not app.exception
    assert {key: app.selectbox(key=key).value for key in values} == values
    assert app.text_input(key="amount_tolerance").value == "0.0100"
    assert app.radio(key="reconciliation_mode").value is mode
    assert "created with a default value" not in caplog.text
    assert not app.metric and not _exception_downloads(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert app.session_state["completed"][0] == original_identity
    assert app.session_state["completed"][1].mode is mode
    assert {m.label: m.value for m in app.metric}["Net difference (A - B)"] == "+245"
    app.selectbox(key="map_key_a_0").set_value("Invoice Number").run()
    assert app.selectbox(key="map_key_a_0").value == "Invoice Number"
    assert app.button(key="run").disabled
    assert not app.metric and not _exception_downloads(app)
    app.selectbox(key="map_key_a_0").set_value("Vendor ID").run()
    app.text_input(key="amount_tolerance").set_value("0.02").run()
    assert not app.button(key="run").disabled
    assert load_mapping_profile(downloaded_profile["data"]).amount_tolerance == Decimal("0.02")


def test_applying_same_profile_clears_result_without_profile_filename_in_identity() -> None:
    app = _uploaded_app(b"id,amount\nINV,100.00\n", b"id,amount\nINV,100.02\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    identity = app.session_state["completed"][0]
    data = export_mapping_profile(ColumnMapping((("id", "id"),), "amount", "amount"))
    for filename in ("first.json", "renamed.json"):
        app.file_uploader(key="profile_upload").set_value(
            (filename, data, "application/json")
        ).run()
        assert app.session_state["completed"][0] == identity
        assert app.metric and _exception_downloads(app)
        app.button(key="apply_profile").click().run()
        assert not app.exception
        assert not app.metric and not _exception_downloads(app)
        app.button(key="run").click().run()
        assert app.session_state["completed"][0] == identity


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("failure", ["json", "mode", "columns"])
def test_failed_profile_application_is_atomic_and_preserves_current_configuration(
    failure: str,
    mode: ReconciliationMode,
) -> None:
    app = _uploaded_app(b"id,amount\nINV,100.00\n", b"id,amount\nINV,100.02\n")
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    completed = app.session_state["completed"]
    profile = export_mapping_profile(
        ColumnMapping((("id", "id"), ("amount", "missing")), "amount", "amount"),
        amount_tolerance=Decimal("0.01"),
        reconciliation_mode=(
            ReconciliationMode.GROUPED_BY_KEY
            if mode is ReconciliationMode.UNIQUE
            else ReconciliationMode.UNIQUE
        ),
    )
    if failure == "json":
        profile = b"{"
    elif failure == "mode":
        document = json.loads(profile)
        document["reconciliation_mode"] = "invalid"
        profile = json.dumps(document).encode()
    app.file_uploader(key="profile_upload").set_value(
        ("bad.json", profile, "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    assert not app.exception
    assert app.error
    assert {
        "json": "valid UTF-8 JSON",
        "mode": "Unsupported reconciliation_mode",
        "columns": 'File B is missing required column "missing"',
    }[failure] in app.error[0].value
    assert app.radio(key="reconciliation_mode").value is mode
    assert app.selectbox(key="map_key_a_0").value == "id"
    assert app.selectbox(key="map_key_b_0").value == "id"
    assert app.selectbox(key="map_amount_a").value == "amount"
    assert app.selectbox(key="map_amount_b").value == "amount"
    assert app.session_state["key_count"] == 1
    assert app.text_input(key="amount_tolerance").value == "0"
    assert app.session_state["completed"] == completed
    assert app.metric and _exception_downloads(app)


def test_profile_replaces_longer_mapping_and_accepts_extra_columns() -> None:
    app = _uploaded_app(b"id,extra,amount\nINV,A,1\n", b"id,extra,amount\nINV,A,1\n")
    app.button(key="add_key").click().run()
    _simple_mapping(app)
    app.selectbox(key="map_key_a_1").set_value("extra")
    app.selectbox(key="map_key_b_1").set_value("extra").run()
    profile = export_mapping_profile(ColumnMapping((("id", "id"),), "amount", "amount"))
    app.file_uploader(key="profile_upload").set_value(
        ("one_key.json", profile, "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    assert not app.exception
    assert app.session_state["key_count"] == 1
    assert "map_key_a_1" not in app.session_state
    assert "map_key_b_1" not in app.session_state
    app.button(key="run").click().run()
    assert not app.exception
    assert "match exactly" in app.success[0].value


def test_profile_download_tracks_current_configuration_validity() -> None:
    app = _uploaded_app(b"id,amount\nINV,1\n", b"id,amount\nINV,1\n")
    assert not app.download_button
    _simple_mapping(app)
    assert app.download_button(key="download_profile")
    app.text_input(key="amount_tolerance").set_value("invalid").run()
    assert not app.download_button
    app.text_input(key="amount_tolerance").set_value("0").run()
    assert app.download_button(key="download_profile")
    app.button(key="add_key").click().run()
    assert not app.download_button  # An incomplete key pair cannot be saved.
    app.selectbox(key="map_key_a_1").set_value("id")
    app.selectbox(key="map_key_b_1").set_value("id").run()
    assert not app.download_button  # Repeated selections cannot be saved.


def _worksheet(app: AppTest, side: str):
    return next(widget for widget in app.selectbox if widget.label == f"File {side} worksheet")


def _acceptance_mapping(app: AppTest) -> None:
    app.button(key="add_key").click().run()
    for key, value in (
        ("map_key_a_0", "Vendor ID"),
        ("map_key_b_0", "Supplier"),
        ("map_key_a_1", "Invoice Number"),
        ("map_key_b_1", "Invoice Ref"),
        ("map_amount_a", "Invoice Amount"),
        ("map_amount_b", "Gross Amount"),
    ):
        app.selectbox(key=key).set_value(value)
    app.run()


@pytest.mark.parametrize("formats", [("xlsx", "xlsx"), ("csv", "xlsx"), ("xlsx", "csv")])
def test_excel_and_mixed_ui_245_evidence_and_profile(acceptance_workbooks, formats) -> None:
    data = [
        acceptance_workbooks[index]
        if kind == "xlsx"
        else (ROOT / "sample_data" / f"file_{side}.csv").read_bytes()
        for index, (side, kind) in enumerate(zip(("a", "b"), formats, strict=True))
    ]
    app = _uploaded_app(*data, name_a=f"a.{formats[0]}", name_b=f"b.{formats[1]}")
    for side, kind in zip(("A", "B"), formats, strict=True):
        assert any(f"Detected file type: {kind.upper()}" == item.value for item in app.caption)
        if kind == "xlsx":
            assert _worksheet(app, side).value == f"Ledger {side}"
    _acceptance_mapping(app)
    profile = export_mapping_profile(
        ColumnMapping(
            (("Vendor ID", "Supplier"), ("Invoice Number", "Invoice Ref")),
            "Invoice Amount",
            "Gross Amount",
        )
    )
    # The profile remains usable across filenames, sheets, and formats.
    app.file_uploader(key="profile_upload").set_value(
        ("csv_mapping.json", profile, "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    app.button(key="run").click().run()
    assert not app.exception and not app.error
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["File A control total"] == "2550"
    assert metrics["File B control total"] == "2305"
    assert metrics["Net difference (A - B)"] == "+245"
    assert metrics["Exact match"] == metrics["Amount mismatch"] == "1"
    assert metrics["File A only"] == metrics["File B only"] == "1"
    assert metrics["Duplicate / ambiguous"] == "0"
    assert app.dataframe[0].value["Delta (A - B)"].tolist() == ["+45", "+500", "-300"]
    assert len(_exception_downloads(app)) == 1
    _select_first_finding(app)
    assert app.dataframe[1].value["Original value"].tolist() == ["V001", "1042", "1250"]
    assert app.dataframe[2].value["Original value"].tolist() == ["V001", "1042", "1205"]
    for side, kind in zip(("A", "B"), formats, strict=True):
        if kind == "xlsx":
            assert any(f"worksheet 'Ledger {side}'" in item.value for item in app.text)

    completed = app.session_state["completed"]
    incompatible = export_mapping_profile(
        ColumnMapping(
            (("Vendor ID", "missing"),),
            "Invoice Amount",
            "Gross Amount",
        ),
        amount_tolerance=Decimal("0.01"),
    )
    app.file_uploader(key="profile_upload").set_value(
        ("bad.json", incompatible, "application/json")
    ).run()
    app.button(key="apply_profile").click().run()
    assert not app.exception
    assert 'File B is missing required column "missing"' in app.error[0].value
    assert app.session_state["completed"] == completed
    assert app.session_state["key_count"] == 2
    assert app.text_input(key="amount_tolerance").value == "0"
    assert len(_exception_downloads(app)) == 1


def test_multi_sheet_requires_explicit_selection_and_resets_schema(xlsx_bytes) -> None:
    data = xlsx_bytes(
        {
            " First é ": [["id", "amount"], ["INV", 2]],
            "Other": [["reference", "gross"], ["INV", 3]],
        },
        configure=lambda book: setattr(book, "active", 1),
    )
    app = _uploaded_app(data, b"id,amount\nINV,1\n", name_a="multi.xlsx")
    sheet = _worksheet(app, "A")
    assert sheet.options == [" First é ", "Other"]
    assert sheet.value is None  # The active worksheet is not selected implicitly.
    assert not app.button and not app.download_button
    sheet.set_value(" First é ").run()
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert len(_exception_downloads(app)) == 1
    assert app.session_state["completed"][1].control_difference == 1
    _worksheet(app, "A").set_value("Other").run()
    assert not app.exception
    assert app.selectbox(key="map_key_a_0").options == ["reference", "gross"]
    assert app.selectbox(key="map_key_a_0").value is None
    assert app.selectbox(key="map_key_b_0").value is None
    assert app.button(key="run").disabled
    assert not app.metric and not app.download_button
    _worksheet(app, "A").set_value(" First é ").run()
    assert not app.metric and not _exception_downloads(app)


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("side", ["A", "B"])
def test_same_schema_sheet_changes_invalidate_result_identity(xlsx_bytes, side, mode) -> None:
    data = xlsx_bytes(
        {
            "One": [["id", "amount"], ["INV", 2]],
            "Two": [["id", "amount"], ["INV", 3]],
        }
    )
    kwargs = {"name_a" if side == "A" else "name_b": "multi.xlsx"}
    inputs = (data, b"id,amount\nINV,1\n") if side == "A" else (b"id,amount\nINV,1\n", data)
    app = _uploaded_app(*inputs, **kwargs)
    _worksheet(app, side).set_value("One").run()
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    old_identity = app.session_state["completed"][0]
    _worksheet(app, side).set_value("Two").run()
    assert not app.metric and not _exception_downloads(app)
    assert app.radio(key="reconciliation_mode").value is mode
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert app.session_state["completed"][0] != old_identity
    assert app.session_state["completed"][1].control_difference == (2 if side == "A" else -2)
    _worksheet(app, side).set_value("One").run()
    assert not app.metric and not _exception_downloads(app)


def test_replacing_workbook_requires_fresh_multi_sheet_selection(xlsx_bytes) -> None:
    data = xlsx_bytes(
        {"One": [["id", "amount"], ["INV", 1]], "Two": [["id", "amount"], ["INV", 2]]}
    )
    app = _uploaded_app(data, b"id,amount\nINV,1\n", name_a="first.xlsx")
    _worksheet(app, "A").set_value("One").run()
    _simple_mapping(app)
    app.button(key="run").click().run()
    replacement = xlsx_bytes(
        {"One": [["id", "amount"], ["INV", 3]], "Two": [["id", "amount"], ["INV", 4]]}
    )
    app.file_uploader(key="upload_A").set_value(
        (
            "second.xlsx",
            replacement,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    ).run()
    assert _worksheet(app, "A").value is None
    assert not app.metric and not _exception_downloads(app)
    assert not app.exception


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_xlsx_formula_error_is_readable_and_blocks_download(xlsx_bytes, mode) -> None:
    data = xlsx_bytes({"Data": [["id", "amount"], ["INV", "=100+1"]]})
    app = _uploaded_app(b"id,amount\nINV,101\n", data, name_b="formula.xlsx")
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    assert not app.exception
    assert len(app.error) == 1
    message = app.error[0].value
    assert "File B, worksheet 'Data', worksheet row 2, column 'amount'" in message
    assert "formula" in message and "export/paste as values" in message
    assert not app.metric and not _exception_downloads(app)


def test_corrupt_xlsx_ui_has_concise_error() -> None:
    app = _uploaded_app(b"not a workbook", b"id,amount\nINV,1\n", name_a="broken.xlsx")
    assert not app.exception
    assert "File A: could not read XLSX workbook" in app.error[0].value
    assert not app.metric and not app.download_button


def _assert_result_cleared(app: AppTest) -> None:
    assert not app.exception
    assert "completed" not in app.session_state
    assert not app.metric and not app.dataframe and not _exception_downloads(app)


def test_mode_control_defaults_to_unique_records_and_discloses_result_mode() -> None:
    app = _uploaded_app(b"id,amount\nINV,1\n", b"id,amount\nINV,1\n")
    control = app.radio(key="reconciliation_mode")

    assert control.label == "Reconciliation mode"
    assert control.options == ["Unique records", "Group by matching key"]
    assert control.value is ReconciliationMode.UNIQUE
    assert any(
        "Duplicate matching keys remain ambiguous and require review." == caption.value
        for caption in app.caption
    )
    _simple_mapping(app)
    app.button(key="run").click().run()

    assert not app.exception
    assert app.session_state["completed"][1].mode is ReconciliationMode.UNIQUE
    assert any(item.value == "Reconciliation mode: Unique records" for item in app.text)
    assert not any("Grouped results" in caption.value for caption in app.caption)


@pytest.mark.parametrize(
    "formats", [("csv", "csv"), ("xlsx", "xlsx"), ("csv", "xlsx"), ("xlsx", "csv")]
)
@pytest.mark.parametrize(
    ("last_b", "tolerance", "category", "label", "shown_b", "delta"),
    [
        ("150", "0", FindingCategory.EXACT_MATCH, "Exact match", "300", "0"),
        (
            "150.005",
            "0.01",
            FindingCategory.WITHIN_TOLERANCE,
            "Within tolerance",
            "300.005",
            "-0.005",
        ),
        ("151", "0.01", FindingCategory.AMOUNT_MISMATCH, "Amount mismatch", "301", "-1"),
    ],
)
def test_mode_selects_engine_semantics_and_preserves_all_grouped_evidence(
    xlsx_bytes, formats, last_b, tolerance, category, label, shown_b, delta
) -> None:
    rows = [
        [["id", "amount"], ["INV", "100"], ["INV", "200"]],
        [["id", "amount"], ["INV", "150"], ["INV", last_b]],
    ]
    inputs = [
        xlsx_bytes({"Data": values})
        if kind == "xlsx"
        else ("\n".join(",".join(row) for row in values) + "\n").encode()
        for kind, values in zip(formats, rows, strict=True)
    ]
    app = _uploaded_app(*inputs, name_a=f"a.{formats[0]}", name_b=f"b.{formats[1]}")
    _simple_mapping(app)
    app.text_input(key="amount_tolerance").set_value(tolerance).run()
    app.button(key="run").click().run()
    unique_identity, unique = app.session_state["completed"]
    assert unique.mode is ReconciliationMode.UNIQUE
    assert unique.findings[0].category is FindingCategory.DUPLICATE_AMBIGUOUS
    assert {metric.label: metric.value for metric in app.metric}["Duplicate / ambiguous"] == "1"
    assert _exception_downloads(app)

    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    _assert_result_cleared(app)
    assert app.selectbox(key="map_key_a_0").value == "id"
    assert app.selectbox(key="map_key_b_0").value == "id"
    assert app.selectbox(key="map_amount_a").value == "amount"
    assert app.selectbox(key="map_amount_b").value == "amount"
    assert app.text_input(key="amount_tolerance").value == tolerance
    app.button(key="run").click().run()

    assert not app.exception
    grouped_identity, result = app.session_state["completed"]
    assert grouped_identity != unique_identity
    assert result.mode is ReconciliationMode.GROUPED_BY_KEY
    assert result.findings[0].category is category
    assert result.control_difference == result.finding_delta_sum == Decimal(delta)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics[label] == "1"
    assert metrics["Duplicate / ambiguous"] == "0"
    assert any(item.value == "Reconciliation mode: Group by matching key" for item in app.text)
    assert any(
        "Rows sharing the same matching key are totaled on each side" in item.value
        and "does not claim that individual rows correspond" in item.value
        for item in app.caption
    )
    assert any(
        "Grouped results compare totals for each matching key" in item.value
        and "do not claim that individual rows correspond" in item.value
        for item in app.caption
    )
    table = app.dataframe[0].value
    assert table["Category"].tolist() == [label]
    assert table["File A amount"].tolist() == ["300"]
    assert table["File B amount"].tolist() == [shown_b]
    assert table["Delta (A - B)"].tolist() == [delta]
    assert table["File A records"].tolist() == table["File B records"].tolist() == ["2, 3"]
    assert bool(_exception_downloads(app)) is (category is FindingCategory.AMOUNT_MISMATCH)
    if category is FindingCategory.EXACT_MATCH:
        assert any(item.value == "Grouped exact key totals" for item in app.subheader)
        assert "key totals agree exactly" in app.success[0].value

    _select_first_finding(app)
    assert not app.exception
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert app.session_state["completed"][0] == grouped_identity
    assert len(app.dataframe) == 5
    assert [frame.value["Original value"].tolist() for frame in app.dataframe[1:]] == [
        ["INV", "100"],
        ["INV", "200"],
        ["INV", "150"],
        ["INV", last_b],
    ]
    assert [caption.value for caption in app.caption].count("Source record 2") == 2
    assert [caption.value for caption in app.caption].count("Source record 3") == 2


def test_mode_and_tolerance_changes_clear_results_and_return_to_deterministic_identity() -> None:
    app = _uploaded_app(b"id,amount\nINV,100\nINV,200\n", b"id,amount\nINV,150\nINV,150.005\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    original_identity = app.session_state["completed"][0]

    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    _assert_result_cleared(app)
    app.text_input(key="amount_tolerance").set_value("0.01").run()
    app.button(key="run").click().run()
    grouped_identity, grouped_result = app.session_state["completed"]
    assert grouped_identity != original_identity
    assert grouped_result.mode is ReconciliationMode.GROUPED_BY_KEY
    assert grouped_result.findings[0].category is FindingCategory.WITHIN_TOLERANCE

    for tolerance in ("0.02", "0.01"):
        app.text_input(key="amount_tolerance").set_value(tolerance).run()
        _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] == grouped_identity

    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.UNIQUE).run()
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    identity, result = app.session_state["completed"]
    assert identity not in (original_identity, grouped_identity)
    assert result.mode is ReconciliationMode.UNIQUE
    assert result.findings[0].category is FindingCategory.DUPLICATE_AMBIGUOUS

    app.text_input(key="amount_tolerance").set_value("0").run()
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] == original_identity
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    _assert_result_cleared(app)


def test_applying_v1_profile_restores_unique_only_after_explicit_apply(
    downloaded_profile: dict,
) -> None:
    app = _uploaded_app(b"id,amount\nINV,100\nINV,200\n", b"id,amount\nINV,150\nINV,150\n")
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY)
    app.text_input(key="amount_tolerance").set_value("1").run()
    app.button(key="run").click().run()
    completed = app.session_state["completed"]
    profile = json.dumps(
        {
            "format": "tallydiff-mapping-profile",
            "version": 1,
            "key_pairs": [{"file_a": "id", "file_b": "id"}],
            "amount_columns": {"file_a": "amount", "file_b": "amount"},
            "amount_tolerance": "0.0100",
        }
    ).encode()
    app.file_uploader(key="profile_upload").set_value(
        ("legacy.json", profile, "application/json")
    ).run()

    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert app.text_input(key="amount_tolerance").value == "1"
    assert app.session_state["completed"] == completed
    app.button(key="apply_profile").click().run()
    _assert_result_cleared(app)
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.UNIQUE
    assert app.text_input(key="amount_tolerance").value == "0.0100"
    exported = json.loads(downloaded_profile["data"])
    assert exported["version"] == 2 and exported["reconciliation_mode"] == "unique"

    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.mode is ReconciliationMode.UNIQUE
    assert result.findings[0].category is FindingCategory.DUPLICATE_AMBIGUOUS


def test_grouped_one_to_one_exact_has_no_grouped_exact_evidence_table() -> None:
    app = _uploaded_app(b"id,amount\nINV,10\n", b"id,amount\nINV,10\n")
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    app.button(key="run").click().run()

    assert not app.exception
    assert {metric.label: metric.value for metric in app.metric}["Exact match"] == "1"
    assert not any(item.value == "Grouped exact key totals" for item in app.subheader)
    assert not app.dataframe
    assert any(
        "Grouped results compare totals for each matching key" in item.value
        and "do not claim that individual rows correspond" in item.value
        for item in app.caption
    )


@pytest.mark.parametrize(
    ("amounts_a", "amounts_b"),
    [
        (["300"], ["100", "200"]),
        (["100", "200"], ["300"]),
        (["100", "200"], ["150", "150"]),
    ],
    ids=["one-many", "many-one", "many-many"],
)
def test_grouped_exact_table_excludes_one_to_one_but_counts_all_and_preserves_evidence(
    amounts_a: list[str], amounts_b: list[str]
) -> None:
    inputs = [
        ("id,amount\nSINGLE,9\n" + "".join(f"GROUPED,{amount}\n" for amount in amounts)).encode()
        for amounts in (amounts_a, amounts_b)
    ]
    app = _uploaded_app(*inputs)
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY).run()
    app.button(key="run").click().run()

    assert not app.exception
    assert {metric.label: metric.value for metric in app.metric}["Exact match"] == "2"
    assert any(item.value == "Grouped exact key totals" for item in app.subheader)
    assert len(app.dataframe) == 1
    table = app.dataframe[0].value
    assert table["Matching key"].tolist() == ["GROUPED"]
    assert table["File A amount"].tolist() == table["File B amount"].tolist() == ["300"]
    assert table["File A records"].tolist() == [
        ", ".join(str(row) for row in range(3, 3 + len(amounts_a)))
    ]
    assert table["File B records"].tolist() == [
        ", ".join(str(row) for row in range(3, 3 + len(amounts_b)))
    ]

    _select_first_finding(app)

    assert not app.exception
    assert len(app.dataframe) == 1 + len(amounts_a) + len(amounts_b)
    assert [frame.value["Original value"].tolist() for frame in app.dataframe[1:]] == [
        ["GROUPED", amount] for amount in (*amounts_a, *amounts_b)
    ]
    assert [item.value for item in app.caption if item.value.startswith("Source record ")] == [
        f"Source record {row}"
        for amounts in (amounts_a, amounts_b)
        for row in range(3, 3 + len(amounts))
    ]
    assert {metric.label: metric.value for metric in app.metric}["Exact match"] == "2"


@pytest.fixture
def review_downloads(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured = {}
    original = st.download_button

    def capture(label, *args, **kwargs):
        if label in ("Download exception report", "Download mapping profile"):
            captured[label] = kwargs["data"]
        return original(label, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture)
    return captured


def _review_app(mode=ReconciliationMode.UNIQUE, tolerance="0") -> AppTest:
    app = _uploaded_app(
        b"id,amount\nA_ONLY,5\nBIG,10\nCREDIT,0\nDUP,5\nDUP,-5\nSMALL,1\n"
        b"EXACT,1\nGROUPED_EXACT,1\nGROUPED_EXACT,2\n",
        b"id,amount\nBIG,0\nCREDIT,2\nDUP,0\nSMALL,1.0001\nEXACT,1\nGROUPED_EXACT,3\nB_ONLY,3\n",
    )
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode)
    app.text_input(key="amount_tolerance").set_value(tolerance).run()
    assert not any(widget.label == "Search matching keys" for widget in app.text_input)
    app.button(key="run").click().run()
    assert not app.exception
    return app


def _review_widget(app: AppTest, label: str):
    return next(
        widget
        for widget in (*app.text_input, *app.multiselect, *app.selectbox)
        if widget.label == label
    )


def _exception_table(app: AppTest):
    return next(
        (frame for frame in app.dataframe if frame.key and frame.key.startswith("exceptions_")),
        None,
    )


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_exception_review_defaults_show_all_with_fixed_exception_categories(mode) -> None:
    app = _review_app(mode)
    assert _review_widget(app, "Search matching keys").value == ""
    assert _review_widget(app, "Minimum absolute delta").value == "0"
    assert _review_widget(app, "Exception sort order").value == "matching_key"
    categories = _review_widget(app, "Exception categories")
    assert categories.value == list(EXCEPTION_CATEGORIES)
    assert categories.options == [
        "Amount mismatch",
        "File A only",
        "File B only",
        "Duplicate / ambiguous",
    ]
    expected = ["A_ONLY", "BIG", "B_ONLY", "CREDIT", "SMALL"]
    if mode is ReconciliationMode.UNIQUE:
        expected = ["A_ONLY", "BIG", "B_ONLY", "CREDIT", "DUP", "GROUPED_EXACT", "SMALL"]
    assert _exception_table(app).value["Matching key"].tolist() == expected
    assert any(
        item.value == f"Showing {len(expected)} of {len(expected)} exception groups."
        for item in app.caption
    )
    assert any(
        "Review filters affect the displayed table only. The exception export remains complete."
        == item.value
        for item in app.caption
    )


@pytest.mark.parametrize(
    ("data_b", "tolerance"),
    [(b"id,amount\n", "0"), (b"id,amount\nINV,1\n", "0"), (b"id,amount\nINV,1.01\n", "0.01")],
)
def test_exception_review_controls_are_absent_without_exceptions(data_b, tolerance) -> None:
    data_a = b"id,amount\n" if data_b == b"id,amount\n" else b"id,amount\nINV,1\n"
    app = _uploaded_app(data_a, data_b)
    _simple_mapping(app)
    app.text_input(key="amount_tolerance").set_value(tolerance).run()
    app.button(key="run").click().run()

    assert not app.exception
    labels = {widget.label for widget in (*app.text_input, *app.selectbox, *app.multiselect)}
    assert not labels.intersection(
        {
            "Search matching keys",
            "Exception categories",
            "Minimum absolute delta",
            "Exception sort order",
        }
    )


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize(
    ("label", "value", "visible_keys"),
    [
        ("Search matching keys", "big", ["BIG"]),
        (
            "Exception categories",
            [FindingCategory.A_ONLY, FindingCategory.B_ONLY],
            ["A_ONLY", "B_ONLY"],
        ),
        ("Minimum absolute delta", "3", ["A_ONLY", "BIG", "B_ONLY"]),
        ("Exception sort order", "absolute_delta_desc", None),
    ],
)
def test_review_controls_preserve_result_metrics_profile_and_complete_export(
    monkeypatch, review_downloads, mode, label, value, visible_keys
) -> None:
    calls = []

    def tracked_reconcile(*args, **kwargs):
        calls.append(kwargs)
        return reconcile(*args, **kwargs)

    monkeypatch.setattr("tallydiff.reconcile", tracked_reconcile)
    app = _review_app(mode)
    completed = app.session_state["completed"]
    metrics = {metric.label: metric.value for metric in app.metric}
    original_export = review_downloads["Download exception report"]
    original_profile = review_downloads["Download mapping profile"]
    full_keys = [finding.key[0] for finding in completed[1].exceptions]
    _review_widget(app, label).set_value(value).run()

    assert not app.exception
    assert len(calls) == 1
    assert app.session_state["completed"][0] == completed[0]
    assert app.session_state["completed"][1] is completed[1]
    assert {metric.label: metric.value for metric in app.metric} == metrics
    assert review_downloads["Download mapping profile"] == original_profile
    assert review_downloads["Download exception report"] == original_export
    assert [
        row["Matching key"] for row in csv.DictReader(StringIO(original_export.decode()))
    ] == full_keys
    if visible_keys is None:
        visible_keys = ["BIG", "A_ONLY", "B_ONLY", "CREDIT", "SMALL"]
        if mode is ReconciliationMode.UNIQUE:
            visible_keys += ["DUP", "GROUPED_EXACT"]
    assert _exception_table(app).value["Matching key"].tolist() == visible_keys
    assert any(
        item.value == f"Showing {len(visible_keys)} of {len(full_keys)} exception groups."
        for item in app.caption
    )
    assert app.text_input(key="amount_tolerance").value == "0"


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("Search matching keys", "missing"),
        ("Exception categories", []),
        ("Minimum absolute delta", "11"),
    ],
)
def test_review_zero_matches_is_clear_and_keeps_complete_export(
    review_downloads, label, value
) -> None:
    app = _review_app()
    completed = app.session_state["completed"]
    original_export = review_downloads["Download exception report"]
    _review_widget(app, label).set_value(value).run()
    app.run()

    assert not app.exception
    assert _exception_table(app) is None
    assert any(item.value == "Showing 0 of 7 exception groups." for item in app.caption)
    assert any(
        "No exception groups match the current review filters." == item.value for item in app.info
    )
    assert _review_widget(app, label).value == value
    assert app.session_state["completed"][1] is completed[1]
    assert _exception_downloads(app)
    assert review_downloads["Download exception report"] == original_export


@pytest.mark.parametrize("value", ["-0.01", "NaN", "Infinity", "invalid"])
def test_invalid_review_minimum_keeps_completed_result_and_export(review_downloads, value) -> None:
    app = _review_app()
    completed = app.session_state["completed"]
    original_export = review_downloads["Download exception report"]
    _review_widget(app, "Minimum absolute delta").set_value(value).run()

    assert not app.exception
    assert any("Minimum absolute delta:" in item.value for item in app.error)
    assert _exception_table(app) is None
    assert app.session_state["completed"][1] is completed[1]
    assert review_downloads["Download exception report"] == original_export
    assert not app.button(key="run").disabled
    assert app.text_input(key="amount_tolerance").value == "0"
    _review_widget(app, "Minimum absolute delta").set_value("0").run()
    assert not app.exception and not app.error
    assert len(_exception_table(app).value) == 7


@pytest.mark.parametrize("value", ["", " \t"])
def test_blank_review_minimum_is_equivalent_to_zero(value) -> None:
    app = _review_app()
    _review_widget(app, "Minimum absolute delta").set_value(value).run()

    assert not app.exception and not app.error
    assert len(_exception_table(app).value) == 7


@pytest.mark.parametrize("mode", list(ReconciliationMode))
def test_filtered_finding_keeps_every_source_row_in_both_modes(mode) -> None:
    app = _uploaded_app(
        b"id,amount\nMULTI,100\nMULTI,200\nHIDDEN,7\n",
        b"id,amount\nMULTI,100\nHIDDEN,6\n",
    )
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    _review_widget(app, "Search matching keys").set_value("multi").run()
    assert _exception_table(app).value["Matching key"].tolist() == ["MULTI"]

    _select_first_finding(app)

    assert not app.exception
    assert [frame.value["Original value"].tolist() for frame in app.dataframe[1:]] == [
        ["MULTI", "100"],
        ["MULTI", "200"],
        ["MULTI", "100"],
    ]
    assert [item.value for item in app.caption if item.value.startswith("Source record ")] == [
        "Source record 2",
        "Source record 3",
        "Source record 2",
    ]
    assert any(item.value == "Showing 1 of 2 exception groups." for item in app.caption)


def test_changing_exception_view_resets_selection_before_filtering_or_reordering() -> None:
    app = _review_app()
    table = _exception_table(app)
    app.session_state[table.key] = {"selection": {"rows": [6], "columns": [], "cells": []}}
    app.run()
    assert any(item.value == "Selected key: SMALL" for item in app.text)

    _review_widget(app, "Minimum absolute delta").set_value("3").run()
    assert not app.exception
    assert _exception_table(app).key != table.key
    assert not any(item.value.startswith("Selected key:") for item in app.text)
    _select_first_finding(app)
    assert any(item.value == "Selected key: A_ONLY" for item in app.text)

    _review_widget(app, "Exception sort order").set_value("absolute_delta_desc").run()
    assert not app.exception
    assert not any(item.value.startswith("Selected key:") for item in app.text)
    _select_first_finding(app)
    assert any(item.value == "Selected key: BIG" for item in app.text)
    assert [frame.value["Original value"].tolist() for frame in app.dataframe[1:]] == [
        ["BIG", "10"],
        ["BIG", "0"],
    ]


def test_exception_filters_leave_tolerated_and_grouped_exact_evidence_separate() -> None:
    app = _review_app(ReconciliationMode.GROUPED_BY_KEY, tolerance="0.001")
    metrics = {metric.label: metric.value for metric in app.metric}
    tolerated = next(frame for frame in app.dataframe if frame.key.startswith("tolerated_"))
    exact = next(frame for frame in app.dataframe if frame.key.startswith("exact_"))
    tolerated_rows, exact_rows = tolerated.value.copy(), exact.value.copy()

    for label, value in (("Search matching keys", "missing"), ("Minimum absolute delta", "-1")):
        _review_widget(app, label).set_value(value).run()
        assert not app.exception
        assert _exception_table(app) is None
        assert {metric.label: metric.value for metric in app.metric} == metrics
        current_tolerated = next(frame for frame in app.dataframe if frame.key == tolerated.key)
        current_exact = next(frame for frame in app.dataframe if frame.key == exact.key)
        assert current_tolerated.value.equals(tolerated_rows)
        assert current_exact.value.equals(exact_rows)
    app.session_state[exact.key] = {"selection": {"rows": [0], "columns": [], "cells": []}}
    app.run()
    assert not app.exception
    assert any(item.value == "Selected key: DUP" for item in app.text)
    assert len(app.dataframe) == 5
