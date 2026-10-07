from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tallydiff import ReconciliationIntegrityError

ROOT = Path(__file__).resolve().parents[1]


def _uploaded_app(data_a: bytes, data_b: bytes) -> AppTest:
    app = AppTest.from_file(str(ROOT / "src/tallydiff/app.py"), default_timeout=15).run()
    assert not app.exception
    app.file_uploader(key="upload_A").set_value(("file_a.csv", data_a, "text/csv"))
    app.file_uploader(key="upload_B").set_value(("file_b.csv", data_b, "text/csv"))
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


def _select_first_exception(app: AppTest) -> None:
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
    app.button(key="run").click().run()
    assert not app.exception
    assert {metric.label: metric.value for metric in app.metric} == {
        "File A control total": "2550",
        "File B control total": "2305",
        "Net difference (A - B)": "+245",
        "Exact match": "1",
        "Amount mismatch": "1",
        "File A only": "1",
        "File B only": "1",
        "Duplicate / ambiguous": "0",
    }
    table = app.dataframe[0].value
    assert table["Matching key"].tolist() == ["V001 / 1042", "V003 / 1044", "V004 / 1045"]
    assert table["Delta (A - B)"].tolist() == ["+45", "+500", "-300"]
    _select_first_exception(app)
    assert len(app.metric) == 8  # Evidence reruns retain the current result.
    assert app.dataframe[1].value["Original value"].tolist() == ["V001", "1042", "1250"]
    assert app.dataframe[2].value["Original value"].tolist() == ["V001", "1042", "1205"]
    assert [item.value for item in app.caption].count("Source record 2") == 2

    app.selectbox(key="map_key_a_0").set_value("Invoice Number").run()
    assert not app.metric
    assert app.button(key="run").disabled  # Duplicate key selection cannot run.
    app.selectbox(key="map_key_a_0").set_value("Vendor ID").run()
    assert not app.metric  # Restoring the old mapping still requires a fresh run.
    app.button(key="run").click().run()
    app.selectbox(key="map_amount_a").set_value("Invoice Number").run()
    assert not app.metric
    app.selectbox(key="map_amount_a").set_value("Invoice Amount").run()
    app.button(key="run").click().run()
    app.file_uploader(key="upload_A").set_value(
        ("file_a.csv", data_a.replace(b"1250", b"999"), "text/csv")
    ).run()
    assert not app.metric
    assert all(widget.value is None for widget in app.selectbox)
    assert not app.exception


def test_zero_delta_duplicates_still_show_warning_and_every_evidence_row() -> None:
    app = _uploaded_app(b"id,amount\nINV,100\nINV,200\n", b"id,amount\nINV,150\nINV,150\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert any("still require review" in warning.value for warning in app.warning)
    assert {metric.label: metric.value for metric in app.metric}["Duplicate / ambiguous"] == "1"
    _select_first_exception(app)
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


def test_integrity_failure_is_displayed_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_integrity(*args):
        raise ReconciliationIntegrityError("synthetic integrity failure")

    monkeypatch.setattr("tallydiff.reconcile", fail_integrity)
    app = _uploaded_app(b"id,amount\nA,1\n", b"id,amount\nA,1\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    assert not app.exception
    assert "Reconciliation integrity check failed" in app.error[0].value
    assert not app.metric
