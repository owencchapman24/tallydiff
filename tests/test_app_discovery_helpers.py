"""App-owned discovery caches and explicit adoption remain separate from results."""

from dataclasses import replace
from decimal import Decimal

import pytest

import tallydiff.app as ui
from tallydiff import IngestionError, ReconciliationMode, Source
from tallydiff.discovery import (
    AmountMappingSuggestion,
    AmountSuggestionResult,
    KeyMappingSuggestion,
    KeySuggestionResult,
    compare_amount_columns,
    compare_headers,
    compare_key_mapping,
    discover_csv,
)


class _State(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


@pytest.fixture
def state(monkeypatch):
    values = _State()
    monkeypatch.setattr(ui.st, "session_state", values)

    def fail(*args, **kwargs):
        raise AssertionError("discovery helpers must not run reconciliation")

    monkeypatch.setattr(ui, "reconcile", fail)
    return values


@pytest.fixture
def snapshots():
    return (
        discover_csv(
            "vendor,invoice,amount,secondary\nV1,I1,10,x\nV1,I2,20,y\nV2,I1,30,z\n",
            source=Source.A,
        ),
        discover_csv(
            "supplier,reference,gross,other\nV1,I1,11,x\nV1,I2,20,y\nV2,I1,30,z\n",
            source=Source.B,
        ),
    )


def _key_suggestion(snapshots, pairs=(("vendor", "supplier"), ("invoice", "reference"))):
    return KeyMappingSuggestion(
        pairs,
        compare_key_mapping(*snapshots, key_pairs=pairs),
        tuple(compare_headers(*pair) for pair in pairs),
    )


def _amount_suggestion(snapshots):
    return AmountMappingSuggestion(
        "amount",
        "gross",
        compare_amount_columns(*snapshots, column_a="amount", column_b="gross"),
        compare_headers("amount", "gross"),
    )


def _input(source, data, *, name="input.csv", input_format="csv", worksheet=None):
    cached = ui._cached_discovery(source, data, input_format, worksheet)
    return ui._Input(
        name,
        data,
        tuple(column.name for column in cached.snapshot.profile.columns),
        input_format,
        cached.snapshot,
        worksheet=worksheet,
        text=cached.text,
    )


def _completed_state(state):
    completed = ("existing configuration", object())
    ownership = {
        "completed": completed,
        "_review_state": object(),
        "_review_binding": object(),
        "_review_prepared_downloads": object(),
        "review_session_upload": object(),
        "exceptions_prior_selection": object(),
    }
    state.update(ownership)
    return ownership


def _retained_mapping_state(state):
    preserved = {
        "map_amount_a": "previous amount",
        "map_amount_b": "previous gross",
        "amount_tolerance": "0.01",
        "reconciliation_mode": ReconciliationMode.BOUNDED_ONE_TO_MANY,
        "comparison_count": 1,
        "map_comparison_a_0": "secondary",
        "map_comparison_b_0": "other",
        "profile_upload": object(),
        "_discovery_source_A": object(),
        "_discovery_source_B": object(),
        "_discovery_pair": object(),
        "unrelated": object(),
    }
    state.update(preserved)
    return preserved


def test_discovery_identity_covers_role_exact_bytes_format_and_worksheet():
    data = b"id,amount\nK1,1\n"
    identity = ui._discovery_identity(Source.A, data, "csv", None)
    assert type(identity) is tuple
    assert identity == (Source.A, data, "csv", None)
    assert identity == ui._discovery_identity(Source.A, bytes(bytearray(data)), "csv", None)
    alternatives = (
        (Source.B, data, "csv", None),
        (Source.A, data + b"\n", "csv", None),
        (Source.A, data, "xlsx", None),
        (Source.A, data, "csv", "Data"),
    )
    assert all(identity != ui._discovery_identity(*parts) for parts in alternatives)


def test_csv_discovery_cache_reuses_current_side_and_replaces_changed_bytes(state, monkeypatch):
    original = ui.discover_csv
    calls = []

    def record(text, *, source):
        calls.append((source, text))
        return original(text, source=source)

    monkeypatch.setattr(ui, "discover_csv", record)
    data_a = b"id,amount\nA,1\n"
    data_b = b"id,amount\nB,2\n"
    first_a = ui._cached_discovery(Source.A, data_a, "csv", None)
    first_b = ui._cached_discovery(Source.B, data_b, "csv", None)
    assert first_a.text == data_a.decode("utf-8")
    assert first_a.identity == (Source.A, data_a, "csv", None)
    assert first_a.snapshot.profile.source is Source.A
    for _ in range(3):
        state["map_key_a_0"] = "id"
        state["amount_tolerance"] = "0.01"
        assert ui._cached_discovery(Source.A, data_a, "csv", None) is first_a
        assert ui._cached_discovery(Source.B, data_b, "csv", None) is first_b
    assert calls == [(Source.A, data_a.decode()), (Source.B, data_b.decode())]
    for index in range(5):
        changed = f"id,amount\nA,{index + 10}\n".encode()
        current = ui._cached_discovery(Source.A, changed, "csv", None)
        assert current.identity == (Source.A, changed, "csv", None)
        assert state["_discovery_source_A"] is current
        assert state["_discovery_source_B"] is first_b
    assert {key for key in state if key.startswith("_discovery_source_")} == {
        "_discovery_source_A",
        "_discovery_source_B",
    }
    assert len(calls) == 7


def test_failed_replacement_does_not_retain_old_snapshot_for_current_source(state):
    valid = b"id,amount\nA,1\n"
    cached = ui._cached_discovery(Source.A, valid, "csv", None)
    with pytest.raises(IngestionError):
        ui._cached_discovery(Source.A, b"id,amount\nA,1,extra\n", "csv", None)
    assert state.get("_discovery_source_A") is not cached


def test_xlsx_discovery_cache_separates_worksheets_and_retains_no_decoded_csv(
    state, monkeypatch, xlsx_bytes
):
    data = xlsx_bytes({"First": [["id", "amount"], ["A", 1]], "Second": [["id"], ["B"]]})
    original = ui.discover_xlsx
    calls = []

    def record(data, *, source, worksheet):
        calls.append((source, worksheet))
        return original(data, source=source, worksheet=worksheet)

    monkeypatch.setattr(ui, "discover_xlsx", record)
    first = ui._cached_discovery(Source.A, data, "xlsx", "First")
    assert first.text is None
    assert first.snapshot.profile.worksheet == "First"
    assert ui._cached_discovery(Source.A, data, "xlsx", "First") is first
    second = ui._cached_discovery(Source.A, data, "xlsx", "Second")
    assert second is state["_discovery_source_A"]
    assert second.identity == (Source.A, data, "xlsx", "Second")
    assert tuple(column.name for column in second.snapshot.profile.columns) == ("id",)
    assert calls == [(Source.A, "First"), (Source.A, "Second")]


def test_pair_cache_reuses_results_across_mapping_edits_and_filename_changes(state, monkeypatch):
    data = b"id,amount\nA,1\nB,2\n"
    file_a = _input(Source.A, data, name="first.csv")
    file_b = _input(Source.B, data, name="second.csv")
    original_keys = ui.suggest_key_mappings
    original_amounts = ui.suggest_amount_mappings
    calls = []

    def keys(a, b, *, max_suggestions):
        calls.append(("keys", max_suggestions))
        return original_keys(a, b, max_suggestions=max_suggestions)

    def amounts(a, b, *, max_suggestions):
        calls.append(("amounts", max_suggestions))
        return original_amounts(a, b, max_suggestions=max_suggestions)

    monkeypatch.setattr(ui, "suggest_key_mappings", keys)
    monkeypatch.setattr(ui, "suggest_amount_mappings", amounts)
    first = ui._cached_suggestions(file_a, file_b)
    assert first.identity == ((Source.A, data, "csv", None), (Source.B, data, "csv", None))
    ownership = _completed_state(state)
    state["map_amount_a"] = "amount"
    state["norm_casefold_0"] = True
    for name in ("renamed.csv", "another-name.csv"):
        assert ui._cached_suggestions(replace(file_a, name=name), file_b) is first
    assert calls == [("keys", 8), ("amounts", 8)]
    assert all(state[key] is value for key, value in ownership.items())


def test_pair_cache_replacement_clears_stale_candidate_selection_and_notice(state, monkeypatch):
    data = b"id,amount\nA,1\n"
    file_a = _input(Source.A, data)
    file_b = _input(Source.B, data)
    first = ui._cached_suggestions(file_a, file_b)
    state.update(
        discovery_key_candidate=0,
        discovery_amount_candidate=1,
        _discovery_notice="old pair applied",
    )
    changed_b = _input(Source.B, b"id,amount\nA,9\n")
    second = ui._cached_suggestions(file_a, changed_b)
    assert second is not first
    assert state["_discovery_pair"] is second
    assert second.identity[1] == (Source.B, changed_b.data, "csv", None)
    assert not {
        "discovery_key_candidate",
        "discovery_amount_candidate",
        "_discovery_notice",
    }.intersection(state)


def test_key_adoption_cleans_all_old_slots_and_normalization_only(state, snapshots):
    preserved = _retained_mapping_state(state)
    _completed_state(state)
    state["key_count"] = 3
    for index in (0, 1, 2, 9):
        for side in ("a", "b"):
            state[f"map_key_{side}_{index}"] = f"old {side} {index}"
        for name in ui.NORMALIZATION_LABELS:
            state[f"norm_{name}_{index}"] = True
    ui._apply_key_suggestion(_key_suggestion(snapshots))
    assert state["key_count"] == 2
    assert {key: value for key, value in state.items() if key.startswith("map_key_")} == {
        "map_key_a_0": "vendor",
        "map_key_b_0": "supplier",
        "map_key_a_1": "invoice",
        "map_key_b_1": "reference",
    }
    assert {key: value for key, value in state.items() if key.startswith("norm_")} == {
        f"norm_{name}_{index}": False for index in range(2) for name in ui.NORMALIZATION_LABELS
    }
    assert all(state[key] is value for key, value in preserved.items())
    assert not {
        "completed",
        "_review_state",
        "_review_binding",
        "_review_prepared_downloads",
    }.intersection(state)


def test_identical_key_adoption_clears_inactive_slots_without_invalidating_current_result(
    state, snapshots
):
    suggestion = _key_suggestion(snapshots)
    state["key_count"] = 2
    for index, (left, right) in enumerate(suggestion.key_pairs):
        state[f"map_key_a_{index}"] = left
        state[f"map_key_b_{index}"] = right
        for name in ui.NORMALIZATION_LABELS:
            state[f"norm_{name}_{index}"] = False
    state["map_key_a_9"] = "inactive"
    state["norm_casefold_9"] = True
    ownership = _completed_state(state)
    ui._apply_key_suggestion(suggestion)
    assert "map_key_a_9" not in state and "norm_casefold_9" not in state
    assert all(state[key] is value for key, value in ownership.items())


def test_key_adoption_with_same_pairs_resets_active_normalization_and_invalidates_result(
    state, snapshots
):
    suggestion = _key_suggestion(snapshots)
    state["key_count"] = len(suggestion.key_pairs)
    for index, (left, right) in enumerate(suggestion.key_pairs):
        state[f"map_key_a_{index}"] = left
        state[f"map_key_b_{index}"] = right
        state[f"norm_casefold_{index}"] = True
    _completed_state(state)
    ui._apply_key_suggestion(suggestion)
    assert "completed" not in state and "_review_state" not in state
    assert all(not value for key, value in state.items() if key.startswith("norm_"))


def test_amount_adoption_preserves_keys_normalization_and_secondary_conflicts(state, snapshots):
    preserved = _retained_mapping_state(state)
    for name in ("map_amount_a", "map_amount_b"):
        preserved.pop(name)
    state.update(
        key_count=2,
        map_key_a_0="vendor",
        map_key_b_0="supplier",
        map_key_a_1="invoice",
        map_key_b_1="reference",
        norm_casefold_0=True,
        norm_remove_punctuation_1=True,
        map_comparison_a_0="amount",
        map_comparison_b_0="gross",
    )
    preserved.update(
        {
            key: value
            for key, value in state.items()
            if key.startswith(("map_key_", "norm_", "map_comparison_")) or key == "key_count"
        }
    )
    _completed_state(state)
    ui._apply_amount_suggestion(_amount_suggestion(snapshots))
    assert state["map_amount_a"] == "amount"
    assert state["map_amount_b"] == "gross"
    assert all(state[key] is value for key, value in preserved.items())
    assert "completed" not in state and "_review_state" not in state


def test_identical_amount_adoption_keeps_completed_result_and_review_ownership(state, snapshots):
    state.update(map_amount_a="amount", map_amount_b="gross")
    ownership = _completed_state(state)
    ui._apply_amount_suggestion(_amount_suggestion(snapshots))
    assert all(state[key] is value for key, value in ownership.items())


@pytest.mark.parametrize("reset_discovery", [True, False])
def test_mapping_reset_clears_discovery_selection_only_when_requested(state, reset_discovery):
    caches = {
        key: object() for key in ("_discovery_source_A", "_discovery_source_B", "_discovery_pair")
    }
    selections = {
        "discovery_key_candidate": 0,
        "discovery_amount_candidate": 1,
        "_discovery_notice": "applied",
    }
    state.update(caches | selections)
    state.update(key_count=2, comparison_count=1, map_key_a_0="old", norm_casefold_0=True)
    _completed_state(state)
    ui._reset_mapping(reset_discovery=reset_discovery)
    assert state["key_count"] == 1 and state["comparison_count"] == 0
    assert not any(key.startswith(("map_", "norm_")) for key in state)
    assert "completed" not in state
    assert all(state[key] is value for key, value in caches.items())
    if reset_discovery:
        assert not selections.keys() & state.keys()
    else:
        assert all(state[key] is value for key, value in selections.items())


def test_key_candidate_labels_keep_exact_names_and_component_order(snapshots):
    suggestion = _key_suggestion(snapshots)
    assert ui._key_candidate_label(suggestion) == "vendor ↔ supplier / invoice ↔ reference"
    assert ui._key_candidate_label(
        _key_suggestion(snapshots, tuple(reversed(suggestion.key_pairs)))
    ) == ("invoice ↔ reference / vendor ↔ supplier")


def test_profile_rows_preserve_source_order_exact_counts_and_unavailable_totals():
    snapshot = discover_csv(
        "Account,Amount,Partial,Blank,Numeric ID\n"
        "A,1.10,1, ,001\nA,2.20,word,\t,002\nB,3.30,2,,001\n",
        source=Source.A,
    )
    rows = ui._profile_rows(snapshot)
    assert [row["Column"] for row in rows] == [
        "Account",
        "Amount",
        "Partial",
        "Blank",
        "Numeric ID",
    ]
    assert rows[0] == {
        "Column": "Account",
        "Blank rows": 0,
        "Distinct nonblank evidence": 2,
        "Key-eligible rows": 3,
        "Distinct baseline keys": 2,
        "Duplicate excess": 1,
        "Amount-parseable rows": 0,
        "Candidate amount total": "—",
    }
    assert rows[1]["Candidate amount total"] == "6.60"
    assert rows[2]["Amount-parseable rows"] == 2
    assert rows[2]["Candidate amount total"] == "—"
    assert rows[3]["Blank rows"] == 3
    assert rows[3]["Distinct baseline keys"] == 0
    assert rows[3]["Candidate amount total"] == "—"
    assert rows[4]["Amount-parseable rows"] == 3
    assert rows[4]["Candidate amount total"] == "4"
    assert not any("_key_rows" in row for row in rows)


def test_key_candidate_rows_display_supplied_order_and_physical_coverage_counts(snapshots):
    single = _key_suggestion(snapshots, (("vendor", "supplier"),))
    composite = _key_suggestion(snapshots)
    result = KeySuggestionResult((single, composite), 1, 16, 1, False)
    rows = ui._key_candidate_rows(result)
    assert rows == [
        {
            "Candidate": 1,
            "Key mapping": "vendor ↔ supplier",
            "Cardinality": 1,
            "Eligible A / total A": "3 / 3",
            "Eligible B / total B": "3 / 3",
            "Overlap rows A / eligible A": "3 / 3",
            "Overlap rows B / eligible B": "3 / 3",
            "Distinct overlapping keys": 2,
            "Duplicate excess A": 1,
            "Duplicate excess B": 1,
            "Header evidence": "Nonempty exact component matches: 0; shared tokens: —",
        },
        {
            "Candidate": 2,
            "Key mapping": "vendor ↔ supplier / invoice ↔ reference",
            "Cardinality": 2,
            "Eligible A / total A": "3 / 3",
            "Eligible B / total B": "3 / 3",
            "Overlap rows A / eligible A": "3 / 3",
            "Overlap rows B / eligible B": "3 / 3",
            "Distinct overlapping keys": 3,
            "Duplicate excess A": 0,
            "Duplicate excess B": 0,
            "Header evidence": "Nonempty exact component matches: 0; shared tokens: — / —",
        },
    ]


def test_header_display_excludes_empty_normalized_equality_from_positive_matches():
    assert ui._header_evidence((compare_headers("$", "€"),)) == (
        "Nonempty exact component matches: 0; shared tokens: —"
    )
    assert (
        ui._header_evidence(
            (
                compare_headers("Invoice Amount", "invoice_amount"),
                compare_headers("ID", "Reference"),
            )
        )
        == "Nonempty exact component matches: 1; shared tokens: invoice, amount / —"
    )


def test_amount_rows_show_exact_decimal_totals_and_signed_control_difference():
    snapshots = (
        discover_csv(
            "Invoice Amount\n12345678901234567890.123456789\n0.000000001\n", source=Source.A
        ),
        discover_csv("invoice_amount\n12345678901234567890.123456789\n0\n", source=Source.B),
    )
    facts = compare_amount_columns(*snapshots, column_a="Invoice Amount", column_b="invoice_amount")
    assert facts.control_difference == Decimal("0.000000001")
    suggestion = AmountMappingSuggestion(
        "Invoice Amount",
        "invoice_amount",
        facts,
        compare_headers("Invoice Amount", "invoice_amount"),
    )
    result = AmountSuggestionResult((suggestion,), 1, 1)
    assert ui._amount_candidate_rows(result) == [
        {
            "Candidate": 1,
            "File A column": "Invoice Amount",
            "File B column": "invoice_amount",
            "Parseable A / total A": "2 / 2",
            "Parseable B / total B": "2 / 2",
            "Candidate total A": "12345678901234567890.123456790",
            "Candidate total B": "12345678901234567890.123456789",
            "A - B control difference": "+0.000000001",
            "Header evidence": (
                "Nonempty exact component matches: 1; shared tokens: invoice, amount"
            ),
        }
    ]
    assert ui._amount_candidate_rows(AmountSuggestionResult((), 0, 1)) == []
