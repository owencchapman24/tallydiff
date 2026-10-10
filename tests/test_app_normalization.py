"""End-to-end normalization controls, profiles, errors, and retained evidence."""

import csv
import json
from decimal import Decimal
from io import StringIO

import pytest
from test_app import (
    _assert_result_cleared,
    _exception_downloads,
    _exception_table,
    _review_widget,
    _select_first_finding,
    _simple_mapping,
    _uploaded_app,
)
from test_app import review_downloads as review_downloads

import tallydiff
import tallydiff.presentation as presentation
from tallydiff import (
    ColumnMapping,
    FindingCategory,
    KeyNormalizationConfig,
    KeyNormalizationRules,
    ReconciliationMode,
    export_mapping_profile,
)

FIELDS = ("casefold", "collapse_whitespace", "remove_punctuation", "strip_leading_zeros")


def _flags(app, index=0):
    return {field: app.checkbox(key=f"norm_{field}_{index}").value for field in FIELDS}


def _set_flags(app, index=0, **flags):
    for field, value in flags.items():
        app.checkbox(key=f"norm_{field}_{index}").set_value(value)
    app.run()


def _map_pairs(app, columns):
    while app.session_state["key_count"] < len(columns):
        app.button(key="add_key").click().run()
    for index, column in enumerate(columns):
        for side in ("a", "b"):
            app.selectbox(key=f"map_key_{side}_{index}").set_value(column)
    for side in ("a", "b"):
        app.selectbox(key=f"map_amount_{side}").set_value("amount")
    app.run()


def _upload_profile(app, data, name="profile.json"):
    app.file_uploader(key="profile_upload").set_value((name, data, "application/json")).run()


def _texts(app):
    return [item.value for item in (*app.text, *app.caption)]


def _assert_blocked(app):
    assert not app.exception
    assert "completed" not in app.session_state
    assert not app.metric and not _exception_downloads(app)
    assert not any("override" in button.label.lower() for button in app.button)
    assert [button.label for button in app.button] == [
        "Apply profile",
        "+ Add key field",
        "Remove last key field",
        "+ Add comparison field",
        "Remove last comparison field",
        "Run reconciliation",
    ]


def test_defaults_pass_canonical_none_to_engine_identity_and_profile(monkeypatch) -> None:
    observed = {"engine": [], "identity": [], "profile": []}

    def capture(target, original):
        def wrapped(*args, **kwargs):
            observed[target].append(kwargs["key_normalization"])
            return original(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(tallydiff, "reconcile", capture("engine", tallydiff.reconcile))
    monkeypatch.setattr(
        tallydiff, "export_mapping_profile", capture("profile", tallydiff.export_mapping_profile)
    )
    monkeypatch.setattr(
        presentation, "configuration_id", capture("identity", presentation.configuration_id)
    )
    app = _uploaded_app(b"id,amount\nINV,10\n", b"id,amount\nINV,10\n")
    assert _flags(app) == dict.fromkeys(FIELDS, False)
    assert {field: app.checkbox(key=f"norm_{field}_0").label for field in FIELDS} == (
        presentation.NORMALIZATION_LABELS
    )
    assert any("fixed order" in text and "-001" in text for text in _texts(app))
    _simple_mapping(app)
    assert app.download_button(key="download_profile")
    assert "completed" not in app.session_state
    app.button(key="run").click().run()
    assert not app.exception
    assert all(values and all(value is None for value in values) for values in observed.values())
    assert app.session_state["completed"][1].key_normalization is None
    assert "Key normalization: Exact — no transformations" in _texts(app)


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize(
    ("field", "key_a", "key_b", "normalized"),
    [
        ("casefold", "AcMe", "acme", "acme"),
        ("collapse_whitespace", "ACME   CORP", "ACME CORP", "ACME CORP"),
        ("remove_punctuation", "ACME-01", "ACME01", "ACME01"),
        ("strip_leading_zeros", "001042", "1042", "1042"),
    ],
)
def test_each_rule_matches_across_sources_in_both_modes(
    field, key_a, key_b, normalized, mode
) -> None:
    app = _uploaded_app(f"id,amount\n{key_a},10\n".encode(), f"id,amount\n{key_b},10\n".encode())
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode)
    _set_flags(app, **{field: True})
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.key_normalization == KeyNormalizationConfig(
        (KeyNormalizationRules(**{field: True}),)
    )
    assert result.mode is mode
    assert [(finding.key, finding.category) for finding in result.findings] == [
        ((normalized,), FindingCategory.EXACT_MATCH)
    ]
    assert {metric.label: metric.value for metric in app.metric}["Exact match"] == "1"
    assert f"Normalization: {presentation.NORMALIZATION_LABELS[field]}" in _texts(app)
    assert any("source evidence retains original" in text.lower() for text in _texts(app))


def test_composite_rules_preserve_component_order_and_exact_component() -> None:
    app = _uploaded_app(
        b"vendor,invoice,tag,amount\nACME-01,001,X,10\n",
        b"vendor,invoice,tag,amount\nacme01,1,X,10\n",
    )
    _map_pairs(app, ("vendor", "invoice", "tag"))
    _set_flags(app, 0, casefold=True, remove_punctuation=True)
    _set_flags(app, 1, strip_leading_zeros=True)
    assert _flags(app, 2) == dict.fromkeys(FIELDS, False)
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.key_normalization == KeyNormalizationConfig(
        (
            KeyNormalizationRules(casefold=True, remove_punctuation=True),
            KeyNormalizationRules(strip_leading_zeros=True),
            KeyNormalizationRules(),
        )
    )
    assert result.findings[0].key == ("acme01", "1", "X")
    texts = _texts(app)
    disclosures = [text for text in texts if text.startswith("Normalization:")]
    assert disclosures == [
        "Normalization: Ignore letter case, Ignore punctuation",
        "Normalization: Ignore leading zeros",
        "Normalization: Exact — no transformations",
    ]
    assert [text for text in texts if text.startswith("Key ") and " ↔ " in text] == [
        "Key 1: vendor  ↔  vendor",
        "Key 2: invoice  ↔  invoice",
        "Key 3: tag  ↔  tag",
    ]


@pytest.mark.parametrize("field", FIELDS)
def test_any_checkbox_change_clears_result_and_reverting_requires_new_run(field) -> None:
    app = _uploaded_app(b"id,amount\n001,10\n", b"id,amount\n001,9\n")
    _simple_mapping(app)
    app.button(key="run").click().run()
    original_identity = app.session_state["completed"][0]
    assert _exception_downloads(app)
    _set_flags(app, **{field: True})
    _assert_result_cleared(app)
    _set_flags(app, **{field: False})
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] == original_identity
    assert app.session_state["completed"][1].key_normalization is None


def test_removed_component_rules_never_resurrect_and_added_rules_default_false() -> None:
    app = _uploaded_app(b"id,extra,amount\nA,B,1\n", b"id,extra,amount\nA,B,1\n")
    _simple_mapping(app)
    _set_flags(app, casefold=True)
    app.button(key="add_key").click().run()
    assert _flags(app, 0)["casefold"]
    assert _flags(app, 1) == dict.fromkeys(FIELDS, False)
    _set_flags(app, 1, **dict.fromkeys(FIELDS, True))
    app.button(key="remove_key").click().run()
    for field in FIELDS:
        assert f"norm_{field}_1" not in app.session_state
    assert _flags(app, 0)["casefold"]
    app.button(key="add_key").click().run()
    assert _flags(app, 1) == dict.fromkeys(FIELDS, False)


@pytest.mark.parametrize("source", ["A", "B"])
def test_changed_file_resets_all_normalization_and_mapping_state(source) -> None:
    data = b"id,extra,amount\nA,B,1\n"
    app = _uploaded_app(data, data)
    _map_pairs(app, ("id", "extra"))
    _set_flags(app, 0, casefold=True)
    _set_flags(app, 1, remove_punctuation=True)
    app.button(key="run").click().run()
    app.file_uploader(key=f"upload_{source}").set_value(
        (f"file_{source.lower()}.csv", data.replace(b",1\n", b",2\n"), "text/csv")
    ).run()
    _assert_result_cleared(app)
    assert app.session_state["key_count"] == 1
    assert _flags(app) == dict.fromkeys(FIELDS, False)
    assert all(widget.value is None for widget in app.selectbox)
    for field in FIELDS:
        assert f"norm_{field}_1" not in app.session_state


@pytest.mark.parametrize("source", ["A", "B"])
def test_worksheet_change_resets_normalization(xlsx_bytes, source) -> None:
    workbook = xlsx_bytes(
        {"One": [["id", "amount"], ["A", 1]], "Two": [["id", "amount"], ["A", 2]]}
    )
    csv_data = b"id,amount\nA,1\n"
    app = _uploaded_app(
        workbook if source == "A" else csv_data,
        workbook if source == "B" else csv_data,
        name_a="book.xlsx" if source == "A" else "file_a.csv",
        name_b="book.xlsx" if source == "B" else "file_b.csv",
    )
    worksheet = next(box for box in app.selectbox if box.label == f"File {source} worksheet")
    worksheet.set_value("One").run()
    _simple_mapping(app)
    _set_flags(app, remove_punctuation=True)
    app.button(key="run").click().run()
    assert app.metric
    next(box for box in app.selectbox if box.label == f"File {source} worksheet").set_value(
        "Two"
    ).run()
    _assert_result_cleared(app)
    assert _flags(app) == dict.fromkeys(FIELDS, False)
    assert app.selectbox(key="map_key_a_0").value is None
    assert app.selectbox(key="map_key_b_0").value is None


def test_normalized_profile_download_apply_and_filename_identity(review_downloads, caplog) -> None:
    app = _uploaded_app(b"id,extra,amount\nACME-01,001,10\n", b"id,extra,amount\nacme01,1,10.01\n")
    _simple_mapping(app)
    _set_flags(app, casefold=True)
    manual_profile = json.loads(review_downloads["Download mapping profile"])
    assert manual_profile["version"] == 5
    assert manual_profile["one_to_many_policy"] is None
    assert manual_profile["comparison_fields"] == []
    assert manual_profile["key_normalization"] == [
        dict(zip(FIELDS, (True, False, False, False), strict=True))
    ]
    assert "completed" not in app.session_state
    app.button(key="run").click().run()
    previous = app.session_state["completed"]
    config = KeyNormalizationConfig(
        (
            KeyNormalizationRules(casefold=True, remove_punctuation=True),
            KeyNormalizationRules(collapse_whitespace=True, strip_leading_zeros=True),
        )
    )
    data = export_mapping_profile(
        ColumnMapping((("id", "id"), ("extra", "extra")), "amount", "amount"),
        amount_tolerance=Decimal("0.0100"),
        reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY,
        key_normalization=config,
    )
    _upload_profile(app, data, "first.json")
    assert app.session_state["completed"][1] is previous[1]
    assert _flags(app)["casefold"] and not _flags(app)["remove_punctuation"]
    assert app.session_state["key_count"] == 1
    app.button(key="apply_profile").click().run()
    _assert_result_cleared(app)
    assert app.session_state["key_count"] == 2
    for index, column in enumerate(("id", "extra")):
        assert app.selectbox(key=f"map_key_a_{index}").value == column
        assert app.selectbox(key=f"map_key_b_{index}").value == column
    assert app.selectbox(key="map_amount_a").value == "amount"
    assert app.selectbox(key="map_amount_b").value == "amount"
    assert app.text_input(key="amount_tolerance").value == "0.0100"
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert _flags(app, 0) == dict(zip(FIELDS, (True, False, True, False), strict=True))
    assert _flags(app, 1) == dict(zip(FIELDS, (False, True, False, True), strict=True))
    downloaded = json.loads(review_downloads["Download mapping profile"])
    assert downloaded == json.loads(data)
    assert app.download_button(key="download_profile")
    assert "created with a default value" not in caplog.text
    app.button(key="run").click().run()
    assert not app.exception
    identity, result = app.session_state["completed"]
    assert result.key_normalization == config
    assert result.findings[0].category is FindingCategory.WITHIN_TOLERANCE
    _upload_profile(app, data, "different_filename.json")
    assert app.session_state["completed"][0] == identity
    assert app.session_state["completed"][1] is result
    app.button(key="apply_profile").click().run()
    _assert_result_cleared(app)
    app.button(key="run").click().run()
    assert app.session_state["completed"][0] == identity


@pytest.mark.parametrize(
    ("version", "mode"),
    [
        (1, ReconciliationMode.UNIQUE),
        (2, ReconciliationMode.UNIQUE),
        (2, ReconciliationMode.GROUPED_BY_KEY),
        (3, ReconciliationMode.GROUPED_BY_KEY),
    ],
)
def test_legacy_and_exact_v3_profiles_explicitly_clear_every_rule(version, mode) -> None:
    data = b"id,extra,amount\nACME,001,10\n"
    app = _uploaded_app(data, data)
    _map_pairs(app, ("id", "extra"))
    for index in range(2):
        _set_flags(app, index, **dict.fromkeys(FIELDS, True))
    app.button(key="run").click().run()
    assert app.metric
    profile = json.loads(
        export_mapping_profile(
            ColumnMapping((("id", "id"), ("extra", "extra")), "amount", "amount"),
            reconciliation_mode=mode,
        )
    )
    profile["version"] = version
    profile.pop("one_to_many_policy")
    profile.pop("comparison_fields")
    if version < 3:
        profile.pop("key_normalization")
    if version == 1:
        profile.pop("reconciliation_mode")
    _upload_profile(app, json.dumps(profile).encode())
    app.button(key="apply_profile").click().run()
    _assert_result_cleared(app)
    assert app.radio(key="reconciliation_mode").value is mode
    for index in range(2):
        assert _flags(app, index) == dict.fromkeys(FIELDS, False)
        for field in FIELDS:
            assert app.session_state[f"norm_{field}_{index}"] is False
    app.button(key="run").click().run()
    assert app.session_state["completed"][1].key_normalization is None


@pytest.mark.parametrize("failure", ["malformed", "column", "rule"])
def test_failed_profile_application_keeps_configuration_and_completed_result(failure) -> None:
    app = _uploaded_app(b"id,amount\nACME,10\n", b"id,amount\nacme,9\n")
    _simple_mapping(app)
    _set_flags(app, casefold=True, strip_leading_zeros=True)
    app.radio(key="reconciliation_mode").set_value(ReconciliationMode.GROUPED_BY_KEY)
    app.text_input(key="amount_tolerance").set_value("0.0100").run()
    app.button(key="run").click().run()
    identity, result = app.session_state["completed"]
    before_flags = _flags(app)
    invalid = json.loads(export_mapping_profile(ColumnMapping((("id", "id"),), "amount", "amount")))
    if failure == "column":
        invalid["key_pairs"][0]["file_a"] = "missing"
    elif failure == "rule":
        invalid["key_normalization"][0]["casefold"] = "true"
    data = b"{" if failure == "malformed" else json.dumps(invalid).encode()
    _upload_profile(app, data)
    app.button(key="apply_profile").click().run()
    assert not app.exception and app.error
    assert app.session_state["completed"][0] == identity
    assert app.session_state["completed"][1] is result
    assert _flags(app) == before_flags
    assert app.session_state["key_count"] == 1
    assert app.selectbox(key="map_key_a_0").value == "id"
    assert app.selectbox(key="map_key_b_0").value == "id"
    assert app.selectbox(key="map_amount_a").value == "amount"
    assert app.selectbox(key="map_amount_b").value == "amount"
    assert app.text_input(key="amount_tolerance").value == "0.0100"
    assert app.radio(key="reconciliation_mode").value is ReconciliationMode.GROUPED_BY_KEY
    assert app.metric and _exception_downloads(app)


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
def test_normalized_table_evidence_review_and_complete_export(
    review_downloads, monkeypatch, mode
) -> None:
    calls = []
    original = tallydiff.reconcile

    def counted(*args, **kwargs):
        calls.append(kwargs["key_normalization"])
        return original(*args, **kwargs)

    monkeypatch.setattr(tallydiff, "reconcile", counted)
    app = _uploaded_app(
        b"id,amount\nACME-01,10\nB-02,20\nC-03,0\n",
        b"id,amount\nACME01,9\nB02,15\n",
    )
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode)
    _set_flags(app, remove_punctuation=True)
    app.button(key="run").click().run()
    assert not app.exception
    identity, result = app.session_state["completed"]
    assert _exception_table(app).value["Matching key"].tolist() == ["ACME01", "B02", "C03"]
    _select_first_finding(app)
    evidence = [
        frame.value["Original value"].tolist()
        for frame in app.dataframe
        if list(frame.value.columns) == ["Field", "Original value"]
    ]
    assert evidence == [["ACME-01", "10"], ["ACME01", "9"]]
    assert result.findings[0].rows_a[0].key == ("ACME-01",)
    assert result.findings[0].rows_b[0].key == ("ACME01",)
    full_export = review_downloads["Download exception report"]
    full_profile = review_downloads["Download mapping profile"]
    assert [row["Matching key"] for row in csv.DictReader(StringIO(full_export.decode()))] == [
        "ACME01",
        "B02",
        "C03",
    ]
    _review_widget(app, "Search matching keys").set_value("B02").run()
    assert _exception_table(app).value["Matching key"].tolist() == ["B02"]
    _review_widget(app, "Search matching keys").set_value("")
    _review_widget(app, "Minimum absolute delta").set_value("2").run()
    assert _exception_table(app).value["Matching key"].tolist() == ["B02"]
    _review_widget(app, "Exception sort order").set_value("absolute_delta_desc").run()
    _review_widget(app, "Exception categories").set_value([FindingCategory.A_ONLY]).run()
    assert _exception_table(app) is None
    assert app.session_state["completed"][0] == identity
    assert app.session_state["completed"][1] is result
    assert len(calls) == 1
    assert review_downloads["Download exception report"] == full_export
    assert review_downloads["Download mapping profile"] == full_profile


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("source", ["A", "B"])
def test_blank_normalized_key_blocks_with_original_source_context(mode, source) -> None:
    bad, good = b"id,amount\n---,10\n", b"id,amount\nX,9\n"
    app = _uploaded_app(bad if source == "A" else good, bad if source == "B" else good)
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    assert app.metric and _exception_downloads(app)
    _set_flags(app, remove_punctuation=True)
    app.button(key="run").click().run()
    _assert_blocked(app)
    assert not app.dataframe
    message = app.error[0].value
    assert "Key normalization blocked" in message
    assert f"File {source} source row 2" in message
    assert "key component 1" in message
    assert "original value: '---'" in message


@pytest.mark.parametrize("mode", [ReconciliationMode.UNIQUE, ReconciliationMode.GROUPED_BY_KEY])
@pytest.mark.parametrize("sources", [("A",), ("B",), ("A", "B")])
def test_collision_blocks_both_modes_and_shows_structured_original_evidence(mode, sources) -> None:
    colliding = b"id,amount\nACME-01,10\nACME01,20\nACME-01,30\n"
    safe = b"id,amount\nACME01,20\n"
    app = _uploaded_app(
        colliding if "A" in sources else safe,
        colliding if "B" in sources else safe,
    )
    _simple_mapping(app)
    app.radio(key="reconciliation_mode").set_value(mode).run()
    app.button(key="run").click().run()
    assert app.metric
    _set_flags(app, remove_punctuation=True)
    app.button(key="run").click().run()
    _assert_blocked(app)
    assert any("refuses to merge them automatically" in text for text in _texts(app))
    assert [item.value for item in app.subheader if "normalization collisions" in item.value] == [
        f"File {source} normalization collisions" for source in sources
    ]
    assert _texts(app).count('Normalized matching key: ["ACME01"]') == len(sources)
    tables = [
        frame.value
        for frame in app.dataframe
        if list(frame.value.columns) == ["Original key", "Source records"]
    ]
    assert len(tables) == len(sources)
    for table in tables:
        assert table["Original key"].tolist() == ['["ACME-01"]', '["ACME01"]']
        assert table["Source records"].tolist() == ["2, 4", "3"]
    evidence = [
        frame.value["Original value"].tolist()
        for frame in app.dataframe
        if list(frame.value.columns) == ["Field", "Original value"]
    ]
    assert evidence == [["ACME-01", "10"], ["ACME-01", "30"], ["ACME01", "20"]] * len(sources)


def test_normalization_preserves_mixed_csv_xlsx_workflow(xlsx_bytes) -> None:
    workbook = xlsx_bytes({"Data": [["id", "amount"], ["ACME01", 10]]})
    app = _uploaded_app(b"id,amount\nACME-01,10\n", workbook, name_b="file_b.xlsx")
    next(box for box in app.selectbox if box.label == "File B worksheet").set_value("Data").run()
    _simple_mapping(app)
    _set_flags(app, remove_punctuation=True)
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.findings[0].key == ("ACME01",)
    assert result.findings[0].category is FindingCategory.EXACT_MATCH
    assert result.findings[0].rows_a[0].raw_fields["id"] == "ACME-01"
    assert result.findings[0].rows_b[0].raw_fields["id"] == "ACME01"


def test_fixed_order_punctuation_then_zero_stripping_matches_exposed_digits() -> None:
    app = _uploaded_app(b"id,amount\n-001,10\n", b"id,amount\n1,10\n")
    _simple_mapping(app)
    _set_flags(app, remove_punctuation=True, strip_leading_zeros=True)
    app.button(key="run").click().run()
    assert not app.exception
    result = app.session_state["completed"][1]
    assert result.findings[0].key == ("1",)
    assert result.findings[0].category is FindingCategory.EXACT_MATCH
    assert result.findings[0].rows_a[0].key == ("-001",)


def test_composite_collisions_keep_full_keys_and_engine_order() -> None:
    app = _uploaded_app(
        b"id,region,amount\nZ-02,North-1,5\nA-01,South,10\n"
        b"Z02,North1,15\nA01,South,20\nA-01,South,30\n",
        b"id,region,amount\nA01,South,60\nZ02,North1,20\n",
    )
    _map_pairs(app, ("id", "region"))
    _set_flags(app, 0, remove_punctuation=True)
    _set_flags(app, 1, remove_punctuation=True)
    app.button(key="run").click().run()
    _assert_blocked(app)
    assert [text for text in _texts(app) if text.startswith("Normalized matching key:")] == [
        'Normalized matching key: ["A01", "South"]',
        'Normalized matching key: ["Z02", "North1"]',
    ]
    tables = [
        frame.value
        for frame in app.dataframe
        if list(frame.value.columns) == ["Original key", "Source records"]
    ]
    assert len(tables) == 2
    assert tables[0].to_dict("records") == [
        {"Original key": '["A-01", "South"]', "Source records": "3, 6"},
        {"Original key": '["A01", "South"]', "Source records": "5"},
    ]
    assert tables[1].to_dict("records") == [
        {"Original key": '["Z-02", "North-1"]', "Source records": "2"},
        {"Original key": '["Z02", "North1"]', "Source records": "4"},
    ]
