"""App-owned discovery caches and explicit adoption remain separate from results."""

from dataclasses import replace
from decimal import Decimal

import pytest

import tallydiff.app as ui
from tallydiff import (
    ColumnMapping,
    ComparisonFieldMapping,
    IngestionError,
    ReconciliationMode,
    Source,
)
from tallydiff.discovery import (
    AmountCompatibility,
    AmountMappingSuggestion,
    AmountSuggestionResult,
    KeyCompatibility,
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
    assert state["discovery_key_candidate"] is None
    assert state["discovery_amount_candidate"] is None
    assert "_discovery_notice" not in state


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
    state.update(key_count=2, comparison_count=1)
    for index in range(2):
        for side in ("a", "b"):
            state[f"map_key_{side}_{index}"] = f"old key {index}"
        for rule in ui.NORMALIZATION_LABELS:
            state[f"norm_{rule}_{index}"] = True
    state.update(
        map_amount_a="old amount",
        map_amount_b="old gross",
        map_comparison_a_0="old comparison",
        map_comparison_b_0="other comparison",
    )
    ownership = _completed_state(state)
    ui._reset_mapping(reset_discovery=reset_discovery)
    assert state["key_count"] == 1 and state["comparison_count"] == 0
    assert {key: value for key, value in state.items() if key.startswith("map_")} == {
        "map_key_a_0": None,
        "map_key_b_0": None,
        "map_amount_a": None,
        "map_amount_b": None,
    }
    assert {key: value for key, value in state.items() if key.startswith("norm_")} == {
        f"norm_{rule}_0": False for rule in ui.NORMALIZATION_LABELS
    }
    assert not ownership.keys() & state.keys()
    assert all(state[key] is value for key, value in caches.items())
    if reset_discovery:
        assert state["discovery_key_candidate"] is None
        assert state["discovery_amount_candidate"] is None
        assert "_discovery_notice" not in state
    else:
        assert all(state[key] is value for key, value in selections.items())


def test_readding_key_component_starts_empty_without_clearing_retained_configuration(state):
    state.update(key_count=2, comparison_count=1)
    preserved = _retained_mapping_state(state)
    state.update(map_key_a_0="vendor", map_key_b_0="supplier", norm_casefold_0=True)
    state.update(map_key_a_1="invoice", map_key_b_1="reference", norm_casefold_1=True)
    _completed_state(state)
    ui._change_key_count(-1)
    assert state["key_count"] == 1
    assert "map_key_a_1" not in state and "map_key_b_1" not in state
    assert all(f"norm_{rule}_1" not in state for rule in ui.NORMALIZATION_LABELS)
    # A stale frontend value may still exist after the old slot is removed.
    state.update(map_key_a_1="invoice", map_key_b_1="reference", norm_casefold_1=True)
    ui._change_key_count(1)
    assert state["key_count"] == 2
    assert state["map_key_a_1"] is None and state["map_key_b_1"] is None
    assert all(state[f"norm_{rule}_1"] is False for rule in ui.NORMALIZATION_LABELS)
    assert state["map_key_a_0"] == "vendor" and state["map_key_b_0"] == "supplier"
    assert state["norm_casefold_0"] is True
    assert all(state[key] is value for key, value in preserved.items())
    assert "completed" not in state and "_review_state" not in state


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


def _threshold_facts(
    *,
    total_a=100,
    total_b=100,
    eligible_a=None,
    eligible_b=None,
    overlap_a=None,
    overlap_b=None,
    duplicate_a=0,
    duplicate_b=0,
    parseable_a=None,
    parseable_b=None,
):
    eligible_a = total_a if eligible_a is None else eligible_a
    eligible_b = total_b if eligible_b is None else eligible_b
    overlap_a = min(eligible_a, eligible_b) if overlap_a is None else overlap_a
    overlap_b = min(eligible_a, eligible_b) if overlap_b is None else overlap_b
    distinct_a = eligible_a - duplicate_a
    distinct_b = eligible_b - duplicate_b
    distinct_overlap = min(overlap_a, overlap_b, distinct_a, distinct_b)
    parseable_a = total_a if parseable_a is None else parseable_a
    parseable_b = total_b if parseable_b is None else parseable_b
    total_amount_a = Decimal(0) if total_a and parseable_a == total_a else None
    total_amount_b = Decimal(0) if total_b and parseable_b == total_b else None
    return (
        KeyCompatibility(
            key_pairs=(("id", "id"),),
            row_count_a=total_a,
            row_count_b=total_b,
            eligible_row_count_a=eligible_a,
            eligible_row_count_b=eligible_b,
            distinct_key_count_a=distinct_a,
            distinct_key_count_b=distinct_b,
            duplicate_excess_count_a=duplicate_a,
            duplicate_excess_count_b=duplicate_b,
            overlap_distinct_count=distinct_overlap,
            overlap_row_count_a=overlap_a,
            overlap_row_count_b=overlap_b,
            only_a_distinct_count=distinct_a - distinct_overlap,
            only_b_distinct_count=distinct_b - distinct_overlap,
        ),
        AmountCompatibility(
            column_a="amount",
            column_b="amount",
            row_count_a=total_a,
            row_count_b=total_b,
            parseable_count_a=parseable_a,
            parseable_count_b=parseable_b,
            blank_count_a=0,
            blank_count_b=0,
            total_a=total_amount_a,
            total_b=total_amount_b,
            control_difference=(
                Decimal(0) if total_amount_a is not None and total_amount_b is not None else None
            ),
        ),
    )


def _warnings_containing(warnings, text):
    return tuple(warning for warning in warnings if text.casefold() in warning.casefold())


@pytest.mark.parametrize(
    "total,overlap,warn",
    [
        (100, 9, True),
        (100, 10, False),
        (100, 11, False),
        (10**30, 10**29 - 1, True),
        (10**30, 10**29, False),
        (10**30, 10**29 + 1, False),
    ],
)
def test_configuration_low_overlap_threshold_is_strict_and_exact_at_large_integers(
    total, overlap, warn
):
    key, amount = _threshold_facts(
        total_a=total, total_b=total, overlap_a=overlap, overlap_b=overlap
    )
    warnings = ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE)
    low = _warnings_containing(warnings, "very low baseline overlap")
    assert bool(low) is warn
    assert len(warnings) == int(warn)
    if low:
        assert f"File A {overlap} / {total}" in low[0]
        assert f"File B {overlap} / {total}" in low[0]
        assert "legitimate" in low[0]
    assert warnings == ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE)


@pytest.mark.parametrize("totals", [(1000, 100), (100, 1000)])
@pytest.mark.parametrize("overlap", [10, 90])
def test_configuration_low_overlap_requires_both_directions_below_ten_percent(totals, overlap):
    key, amount = _threshold_facts(
        total_a=totals[0], total_b=totals[1], overlap_a=overlap, overlap_b=overlap
    )
    assert ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE) == ()


@pytest.mark.parametrize("eligible_a,eligible_b", [(0, 3), (3, 0), (0, 0)])
def test_configuration_zero_eligible_rows_do_not_also_warn_about_overlap(eligible_a, eligible_b):
    key, amount = _threshold_facts(
        total_a=3, total_b=3, eligible_a=eligible_a, eligible_b=eligible_b
    )
    warnings = ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE)
    assert not _warnings_containing(warnings, "baseline overlap")
    no_eligible = _warnings_containing(warnings, "no eligible baseline key rows")
    poor = _warnings_containing(warnings, "poor baseline key eligibility")
    expected_sides = [
        side for side, eligible in (("A", eligible_a), ("B", eligible_b)) if not eligible
    ]
    assert len(no_eligible) == len(poor) == len(expected_sides)
    for side, warning in zip(expected_sides, no_eligible, strict=True):
        assert f"File {side}" in warning and "0 / 3" in warning


def test_configuration_zero_overlap_with_eligible_rows_is_advisory_and_excludes_low_warning():
    key, amount = _threshold_facts(total_a=3, total_b=3, overlap_a=0, overlap_b=0)
    warnings = ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE)
    assert len(warnings) == 1
    assert "no exact baseline overlap" in warnings[0]
    assert "nonoverlapping source populations" in warnings[0]
    assert "not proof" in warnings[0]
    assert not _warnings_containing(warnings, "very low")


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize(
    "total,eligible,warn",
    [
        (10, 8, True),
        (10, 9, False),
        (10, 10, False),
        (10**30, 9 * 10**29 - 1, True),
        (10**30, 9 * 10**29, False),
        (10**30, 9 * 10**29 + 1, False),
    ],
)
def test_configuration_poor_eligibility_threshold_is_strict_per_side_and_exact(
    side, total, eligible, warn
):
    key, amount = _threshold_facts(total_a=total, total_b=total, **{f"eligible_{side}": eligible})
    warnings = ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE)
    poor = _warnings_containing(warnings, "poor baseline key eligibility")
    assert bool(poor) is warn
    assert len(warnings) == int(warn)
    if poor:
        assert f"File {side.upper()}" in poor[0]
        assert f"{eligible} / {total}" in poor[0]


@pytest.mark.parametrize("mode", list(ReconciliationMode))
@pytest.mark.parametrize("side", ["a", "b"])
def test_configuration_duplicate_excess_warning_is_unique_specific_and_avoids_mode_advice(
    mode, side
):
    key, amount = _threshold_facts(
        total_a=10,
        total_b=10,
        duplicate_a=9 if side == "a" else 0,
        duplicate_b=9 if side == "b" else 0,
        overlap_a=10 if side == "a" else 1,
        overlap_b=10 if side == "b" else 1,
    )
    warnings = ui._configuration_warnings(key, amount, mode)
    duplicate = _warnings_containing(warnings, "duplicate keys may produce ambiguous findings")
    assert bool(duplicate) is (mode is ReconciliationMode.UNIQUE)
    assert len(warnings) == int(mode is ReconciliationMode.UNIQUE)
    if duplicate:
        assert "duplicate excess" in duplicate[0]
        assert f"File {side.upper()} 9" in duplicate[0]
        assert "does not establish" in duplicate[0]


@pytest.mark.parametrize("totals", [(0, 0), (0, 3), (3, 0)])
def test_configuration_empty_tables_generate_no_misleading_overlap_or_eligibility_warning(totals):
    key, amount = _threshold_facts(total_a=totals[0], total_b=totals[1])
    assert ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE) == ()


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("parseable", [0, 1])
def test_configuration_amount_warning_identifies_each_affected_side_and_exact_counts(
    side, parseable
):
    key, amount = _threshold_facts(total_a=2, total_b=2, **{f"parseable_{side}": parseable})
    warnings = ui._configuration_warnings(key, amount, ReconciliationMode.UNIQUE)
    assert len(warnings) == 1
    assert "values incompatible with amount parsing" in warnings[0]
    assert f"File {side.upper()}" in warnings[0]
    assert f"{parseable} / 2" in warnings[0]
    assert "ingestion validation" in warnings[0] and "Run" in warnings[0]


@pytest.mark.parametrize(
    "difference", [Decimal(0), Decimal("0.000001"), Decimal("999999999999999")]
)
def test_configuration_warnings_ignore_control_total_equality_and_proximity(difference):
    key, amount = _threshold_facts()
    changed = replace(amount, total_a=difference, total_b=Decimal(0), control_difference=difference)
    assert ui._configuration_warnings(key, changed, ReconciliationMode.UNIQUE) == ()


def _configuration_inputs():
    data = b"first,second,third,amount,alternate,secondary\nV1,I1,L1,10,100,x\nV1,I1,L2,20,200,y\n"
    return _input(Source.A, data), _input(Source.B, data)


def _selected_mapping():
    return ColumnMapping(
        (("first", "first"), ("second", "second"), ("third", "third")), "amount", "amount"
    )


def _record_configuration_comparisons(monkeypatch):
    calls = []
    original_key = ui.compare_key_mapping
    original_amount = ui.compare_amount_columns

    def key(a, b, *, key_pairs):
        calls.append(("key", key_pairs))
        return original_key(a, b, key_pairs=key_pairs)

    def amount(a, b, *, column_a, column_b):
        calls.append(("amount", column_a, column_b))
        return original_amount(a, b, column_a=column_a, column_b=column_b)

    monkeypatch.setattr(ui, "compare_key_mapping", key)
    monkeypatch.setattr(ui, "compare_amount_columns", amount)
    return calls


def test_configuration_cache_uses_exact_selected_identity_and_reuses_baseline_facts(
    state, monkeypatch
):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    calls = _record_configuration_comparisons(monkeypatch)
    evidence = ui._cached_configuration_evidence(file_a, file_b, mapping)
    assert evidence is state["_configuration_evidence"]
    assert evidence.identity == (
        (Source.A, file_a.data, "csv", None),
        (Source.B, file_b.data, "csv", None),
        mapping.key_pairs,
        "amount",
        "amount",
    )
    assert evidence.key == compare_key_mapping(
        file_a.discovery, file_b.discovery, key_pairs=mapping.key_pairs
    )
    assert evidence.amount == compare_amount_columns(
        file_a.discovery, file_b.discovery, column_a="amount", column_b="amount"
    )
    assert len(evidence.key.key_pairs) == 3
    ownership = _completed_state(state)
    suggestion_bundle = object()
    state["_discovery_pair"] = suggestion_bundle
    for mode in ReconciliationMode:
        state.update(reconciliation_mode=mode, amount_tolerance="-invalid", norm_casefold_0=True)
        assert ui._cached_configuration_evidence(file_a, file_b, mapping) is evidence
        assert (
            ui._cached_configuration_evidence(
                replace(file_a, name="renamed.csv"), replace(file_b, name="other.csv"), mapping
            )
            is evidence
        )
    with_comparison = replace(
        mapping, comparison_fields=(ComparisonFieldMapping("secondary", "secondary"),)
    )
    assert ui._cached_configuration_evidence(file_a, file_b, with_comparison) is evidence
    assert calls == [("key", mapping.key_pairs), ("amount", "amount", "amount")]
    assert all(state[key] is value for key, value in ownership.items())
    assert state["_discovery_pair"] is suggestion_bundle


def test_configuration_cache_replaces_ordered_keys_and_either_selected_amount(state, monkeypatch):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    calls = _record_configuration_comparisons(monkeypatch)
    previous = ui._cached_configuration_evidence(file_a, file_b, mapping)
    variants = (
        replace(mapping, key_pairs=tuple(reversed(mapping.key_pairs))),
        replace(mapping, amount_a="alternate"),
        replace(mapping, amount_b="alternate"),
        replace(mapping, key_pairs=(("second", "second"),)),
    )
    for selected in variants:
        current = ui._cached_configuration_evidence(file_a, file_b, selected)
        assert current is not previous
        assert state["_configuration_evidence"] is current
        assert current.identity[-3:] == (selected.key_pairs, selected.amount_a, selected.amount_b)
        assert current.key == compare_key_mapping(
            file_a.discovery, file_b.discovery, key_pairs=selected.key_pairs
        )
        previous = current
    assert len(calls) == 2 * (len(variants) + 1)
    assert {key for key in state if key.startswith("_configuration_")} == {
        "_configuration_evidence"
    }


def test_configuration_cache_recomputes_for_bytes_format_or_worksheet_changes(
    state, monkeypatch, xlsx_bytes
):
    file_a = _input(Source.A, b"id,amount\nA,1\n")
    file_b = _input(Source.B, b"id,amount\nA,1\n")
    mapping = ColumnMapping((("id", "id"),), "amount", "amount")
    calls = _record_configuration_comparisons(monkeypatch)
    previous = ui._cached_configuration_evidence(file_a, file_b, mapping)
    data = xlsx_bytes({"One": [["id", "amount"], ["A", 1]], "Two": [["id", "amount"], ["A", 1]]})
    sources = (
        _input(Source.B, b"id,amount\nA,2\n"),
        _input(Source.B, data, input_format="xlsx", worksheet="One"),
        _input(Source.B, data, input_format="xlsx", worksheet="Two"),
    )
    for changed_b in sources:
        current = ui._cached_configuration_evidence(file_a, changed_b, mapping)
        assert current is not previous
        assert current.identity[1] == (
            Source.B,
            changed_b.data,
            changed_b.input_format,
            changed_b.worksheet,
        )
        assert ui._cached_configuration_evidence(file_a, changed_b, mapping) is current
        previous = current
    changed_a = _input(Source.A, b"id,amount\nA,9\n")
    current = ui._cached_configuration_evidence(changed_a, sources[-1], mapping)
    assert current is not previous
    assert current.identity[0] == (Source.A, changed_a.data, "csv", None)
    assert len(calls) == 10


def test_invalid_selected_configuration_evicts_cache_without_comparison_or_result_changes(
    state, monkeypatch
):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    calls = _record_configuration_comparisons(monkeypatch)
    ownership = _completed_state(state)
    invalid = (
        replace(mapping, key_pairs=((None, "first"),)),
        replace(mapping, key_pairs=(("first", "first"), ("first", "second"))),
        replace(mapping, amount_a=None),
        replace(mapping, amount_b="missing"),
        replace(mapping, comparison_fields=(ComparisonFieldMapping("amount", "secondary"),)),
        replace(mapping, comparison_fields=(ComparisonFieldMapping("secondary", None),)),
    )
    for selected in invalid:
        assert ui._cached_configuration_evidence(file_a, file_b, mapping) is not None
        previous_count = len(calls)
        assert ui._cached_configuration_evidence(file_a, file_b, selected) is None
        assert "_configuration_evidence" not in state
        assert len(calls) == previous_count
        assert all(state[key] is value for key, value in ownership.items())


@pytest.mark.parametrize("reset_discovery", [True, False])
def test_configuration_cache_survives_result_clear_and_profile_reset_but_not_source_reset(
    state, reset_discovery
):
    file_a, file_b = _configuration_inputs()
    selected = ui._cached_configuration_evidence(file_a, file_b, _selected_mapping())
    _completed_state(state)
    ui._clear_result()
    assert state["_configuration_evidence"] is selected
    ui._reset_mapping(reset_discovery=reset_discovery)
    if reset_discovery:
        assert "_configuration_evidence" not in state
    else:
        assert state["_configuration_evidence"] is selected
        mapping = _selected_mapping()
        state["key_count"] = len(mapping.key_pairs)
        for index, (left, right) in enumerate(mapping.key_pairs):
            state[f"map_key_a_{index}"] = left
            state[f"map_key_b_{index}"] = right
        state.update(
            map_amount_a=mapping.amount_a,
            map_amount_b=mapping.amount_b,
            reconciliation_mode=ReconciliationMode.GROUPED_BY_KEY,
            amount_tolerance="0.01",
            norm_casefold_0=True,
        )
        assert ui._cached_configuration_evidence(file_a, file_b, mapping) is selected


def _record_possible_key_searches(monkeypatch):
    original = ui.find_possible_key_differences
    calls = []

    def record(file_a, file_b, *, key_pairs):
        calls.append((file_a, file_b, key_pairs))
        return original(file_a, file_b, key_pairs=key_pairs)

    monkeypatch.setattr(ui, "find_possible_key_differences", record)
    return calls


def test_possible_key_identity_uses_exact_sources_and_ordered_keys_only(state):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    expected = (
        (Source.A, file_a.data, "csv", None),
        (Source.B, file_b.data, "csv", None),
        mapping.key_pairs,
    )
    assert ui._possible_key_difference_identity(file_a, file_b, mapping) == expected
    renamed_a = replace(file_a, name="renamed.csv")
    renamed_b = replace(file_b, name="another name.csv")
    variants = (
        replace(mapping, amount_a="alternate"),
        replace(mapping, amount_b="alternate"),
        replace(mapping, comparison_fields=(ComparisonFieldMapping("secondary", "secondary"),)),
    )
    for selected in variants:
        assert ui._possible_key_difference_identity(renamed_a, renamed_b, selected) == expected
    reversed_keys = replace(mapping, key_pairs=tuple(reversed(mapping.key_pairs)))
    assert ui._possible_key_difference_identity(file_a, file_b, reversed_keys) == (
        *expected[:2],
        reversed_keys.key_pairs,
    )


def test_possible_key_getter_never_searches_before_inspection(state, monkeypatch):
    file_a, file_b = _configuration_inputs()
    calls = _record_possible_key_searches(monkeypatch)
    ownership = _completed_state(state)
    for _ in range(3):
        assert ui._current_possible_key_differences(file_a, file_b, _selected_mapping()) is None
    assert calls == []
    assert "_possible_key_differences" not in state
    assert all(state[key] is value for key, value in ownership.items())


def test_possible_key_inspection_caches_one_request_without_changing_other_ownership(
    state, monkeypatch
):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    calls = _record_possible_key_searches(monkeypatch)
    ui._cached_configuration_evidence(file_a, file_b, mapping)
    ownership = _completed_state(state)
    state.update(
        key_count=3,
        map_key_a_0="first",
        map_key_b_0="first",
        map_amount_a="amount",
        map_amount_b="amount",
        norm_casefold_0=True,
        reconciliation_mode=ReconciliationMode.UNIQUE,
        amount_tolerance="0.01",
        _discovery_pair=object(),
        discovery_key_candidate=1,
        discovery_amount_candidate=2,
        _discovery_notice="existing notice",
        unrelated=object(),
    )
    before = dict(state)
    selected = ui._inspect_possible_key_differences(file_a, file_b, mapping)
    holder = state["_possible_key_differences"]
    assert holder is selected
    assert holder.identity == ui._possible_key_difference_identity(file_a, file_b, mapping)
    assert selected.result.evaluated and selected.result.total_candidate_count == 0
    assert ui._current_possible_key_differences(file_a, file_b, mapping) is selected
    assert ui._inspect_possible_key_differences(file_a, file_b, mapping) is selected
    assert calls == [(file_a.discovery, file_b.discovery, mapping.key_pairs)]
    assert all(state[key] is value for key, value in before.items())
    assert all(state[key] is value for key, value in ownership.items())
    assert {key for key in state if key.startswith("_possible_key_")} == {
        "_possible_key_differences"
    }


def test_possible_key_cache_reuses_valid_amount_mode_tolerance_and_normalization_changes(
    state, monkeypatch
):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    calls = _record_possible_key_searches(monkeypatch)
    selected = ui._inspect_possible_key_differences(file_a, file_b, mapping)
    for mode in ReconciliationMode:
        state.update(reconciliation_mode=mode, amount_tolerance="12.34", norm_casefold_0=True)
        ui._clear_result()
        assert ui._current_possible_key_differences(file_a, file_b, mapping) is selected
    for changed in (
        replace(mapping, amount_a="alternate"),
        replace(mapping, amount_b="alternate"),
        replace(mapping, comparison_fields=(ComparisonFieldMapping("secondary", "secondary"),)),
    ):
        assert ui._current_possible_key_differences(file_a, file_b, changed) is selected
        assert ui._inspect_possible_key_differences(file_a, file_b, changed) is selected
    assert (
        ui._inspect_possible_key_differences(
            replace(file_a, name="changed name.csv"), file_b, mapping
        )
        is selected
    )
    assert len(calls) == 1


def test_possible_key_getter_clears_changed_keys_without_automatic_search(state, monkeypatch):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    calls = _record_possible_key_searches(monkeypatch)
    selections = (
        replace(mapping, key_pairs=tuple(reversed(mapping.key_pairs))),
        replace(mapping, key_pairs=(("first", "first"), ("third", "third"))),
        replace(mapping, key_pairs=(("second", "first"), ("first", "second"))),
    )
    for changed in selections:
        selected = ui._inspect_possible_key_differences(file_a, file_b, mapping)
        count = len(calls)
        assert ui._current_possible_key_differences(file_a, file_b, changed) is None
        assert "_possible_key_differences" not in state
        assert len(calls) == count
        replacement = ui._inspect_possible_key_differences(file_a, file_b, changed)
        assert replacement is not selected
        assert replacement.result.key_pairs == changed.key_pairs
        assert len(calls) == count + 1


def test_possible_key_source_changes_clear_current_result_without_search(
    state, monkeypatch, xlsx_bytes
):
    data = b"first,second,amount\nV,I1,10\n"
    file_a = _input(Source.A, data)
    file_b = _input(Source.B, data)
    mapping = ColumnMapping((("first", "first"), ("second", "second")), "amount", "amount")
    workbook = xlsx_bytes(
        {
            "One": [["first", "second", "amount"], ["V", "I1", 10]],
            "Two": [["first", "second", "amount"], ["V", "I1", 10]],
        }
    )
    sources = (
        (file_a, _input(Source.B, b"first,second,amount\nV,I1,11\n")),
        (_input(Source.A, b"first,second,amount\nV,I1,12\n"), file_b),
        (file_a, _input(Source.B, workbook, input_format="xlsx", worksheet="One")),
        (file_a, _input(Source.B, workbook, input_format="xlsx", worksheet="Two")),
    )
    calls = _record_possible_key_searches(monkeypatch)
    for changed_a, changed_b in sources:
        ui._inspect_possible_key_differences(file_a, file_b, mapping)
        count = len(calls)
        assert ui._current_possible_key_differences(changed_a, changed_b, mapping) is None
        assert "_possible_key_differences" not in state
        assert len(calls) == count
        selected = ui._inspect_possible_key_differences(changed_a, changed_b, mapping)
        assert selected is state["_possible_key_differences"]
        assert len(calls) == count + 1
    one = _input(Source.B, workbook, input_format="xlsx", worksheet="One")
    two = _input(Source.B, workbook, input_format="xlsx", worksheet="Two")
    ui._inspect_possible_key_differences(file_a, one, mapping)
    count = len(calls)
    assert ui._current_possible_key_differences(file_a, two, mapping) is None
    assert len(calls) == count


@pytest.mark.parametrize(
    "change",
    (
        {"key_pairs": ((None, "first"), ("second", "second"))},
        {"key_pairs": (("first", "first"), ("first", "second"))},
        {"key_pairs": (("missing", "first"), ("second", "second"))},
        {"amount_a": None},
        {"amount_b": "missing"},
        {"comparison_fields": (ComparisonFieldMapping("amount", "secondary"),)},
        {"comparison_fields": (ComparisonFieldMapping("secondary", None),)},
    ),
)
def test_possible_key_invalid_full_configuration_clears_without_search_or_result_changes(
    state, monkeypatch, change
):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    invalid = replace(mapping, **change)
    calls = _record_possible_key_searches(monkeypatch)
    ownership = _completed_state(state)
    for getter in (ui._current_possible_key_differences, ui._inspect_possible_key_differences):
        ui._inspect_possible_key_differences(file_a, file_b, mapping)
        count = len(calls)
        assert ui._possible_key_difference_identity(file_a, file_b, invalid) is None
        assert getter(file_a, file_b, invalid) is None
        assert "_possible_key_differences" not in state
        assert len(calls) == count
        assert all(state[key] is value for key, value in ownership.items())


def test_possible_key_single_component_never_searches_or_retains_old_composite_result(
    state, monkeypatch
):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    single = replace(mapping, key_pairs=(("first", "first"),))
    calls = _record_possible_key_searches(monkeypatch)
    ui._inspect_possible_key_differences(file_a, file_b, mapping)
    assert ui._possible_key_difference_identity(file_a, file_b, single) is None
    assert ui._current_possible_key_differences(file_a, file_b, single) is None
    assert ui._inspect_possible_key_differences(file_a, file_b, single) is None
    assert "_possible_key_differences" not in state
    assert len(calls) == 1


@pytest.mark.parametrize("reset_discovery", [True, False])
def test_possible_key_cache_source_reset_clears_profile_reset_preserves(state, reset_discovery):
    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    selected = ui._inspect_possible_key_differences(file_a, file_b, mapping)
    ui._reset_mapping(reset_discovery=reset_discovery)
    if reset_discovery:
        assert "_possible_key_differences" not in state
    else:
        assert ui._current_possible_key_differences(file_a, file_b, mapping) is selected


def test_possible_key_getter_reuses_identity_holder_after_app_class_recreation(state, monkeypatch):
    from types import SimpleNamespace

    file_a, file_b = _configuration_inputs()
    mapping = _selected_mapping()
    calls = _record_possible_key_searches(monkeypatch)
    selected = ui._inspect_possible_key_differences(file_a, file_b, mapping)
    identity = state["_possible_key_differences"].identity
    restored = SimpleNamespace(identity=identity, result=selected.result)
    state["_possible_key_differences"] = restored
    assert ui._current_possible_key_differences(file_a, file_b, mapping) is restored
    assert ui._inspect_possible_key_differences(file_a, file_b, mapping) is restored
    assert len(calls) == 1


def test_possible_key_rows_preserve_complete_baseline_keys_and_exact_shared_components():
    import json

    from tallydiff.discovery import PossibleKeyDifference, PossibleKeyDifferenceResult

    common = 'Vendor "quoted", [one] | café 雪'
    long_value = "0" * 1000 + "7"
    key_a = (common, "000123", long_value)
    key_b = (common, "000124", long_value)
    pairs = (("Vendor", "Supplier"), ("Invoice", "Reference"), ("Account", "Account code"))
    candidate = PossibleKeyDifference(key_a, key_b, 1, "Invoice", "Reference")
    result = PossibleKeyDifferenceResult(pairs, (candidate,), 1, False, True, None, False, 6)
    rows = ui._possible_key_difference_rows(result)
    assert len(rows) == 1
    row = rows[0]
    assert tuple(row) == (
        "File A baseline key",
        "File B baseline key",
        "Differing key component",
        "Exact shared-component evidence",
    )
    assert json.loads(row["File A baseline key"]) == list(key_a)
    assert json.loads(row["File B baseline key"]) == list(key_b)
    assert "café 雪" in row["File A baseline key"]
    assert row["Differing key component"] == "2: Invoice ↔ Reference"
    assert json.loads(row["Exact shared-component evidence"]) == [
        {"component": 1, "column_a": "Vendor", "column_b": "Supplier", "value": common},
        {"component": 3, "column_a": "Account", "column_b": "Account code", "value": long_value},
    ]
    assert ui._possible_key_difference_rows(result) == rows
    assert result.candidates == (candidate,)


def test_possible_key_rows_preserve_detector_order_without_suggesting_rank():
    import json

    from tallydiff.discovery import PossibleKeyDifference, PossibleKeyDifferenceResult

    pairs = (("First", "One"), ("Second", "Two"))
    candidates = (
        PossibleKeyDifference(("B", "same"), ("C", "same"), 0, "First", "One"),
        PossibleKeyDifference(("same", "01"), ("same", "1"), 1, "Second", "Two"),
    )
    result = PossibleKeyDifferenceResult(pairs, candidates, 21, True, True, None, True, 100)
    rows = ui._possible_key_difference_rows(result)
    assert [json.loads(row["File A baseline key"]) for row in rows] == [
        ["B", "same"],
        ["same", "01"],
    ]
    assert [row["Differing key component"] for row in rows] == ["1: First ↔ One", "2: Second ↔ Two"]
    assert all(
        not any(word in name.lower() for word in ("rank", "score", "confidence"))
        for name in rows[0]
    )
    empty = replace(result, candidates=(), total_candidate_count=0, display_truncated=False)
    assert ui._possible_key_difference_rows(empty) == []


def test_all_discovery_caches_keep_only_current_entries_through_repeated_source_and_mapping_cycles(
    state, monkeypatch
):
    from collections import Counter

    calls = Counter()
    for name in (
        "discover_csv",
        "suggest_key_mappings",
        "suggest_amount_mappings",
        "compare_key_mapping",
        "compare_amount_columns",
        "find_possible_key_differences",
    ):
        original = getattr(ui, name)

        def record(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(ui, name, record)
    cache_keys = {
        "_discovery_source_A",
        "_discovery_source_B",
        "_discovery_pair",
        "_configuration_evidence",
        "_possible_key_differences",
    }
    mapping = _selected_mapping()
    for cycle in range(4):
        data_a = (
            "first,second,third,amount,alternate,secondary\n"
            f"V1,I1,L1,{10 + cycle},100,x\n"
            f"V1,I2,L2,{20 + cycle},200,y\n"
        ).encode()
        data_b = (
            "first,second,third,amount,alternate,secondary\n"
            f"V1,I1,L1,{11 + cycle},100,x\n"
            f"V1,I3,L2,{21 + cycle},200,y\n"
        ).encode()
        file_a = _input(Source.A, data_a)
        file_b = _input(Source.B, data_b)
        suggestions = ui._cached_suggestions(file_a, file_b)
        original = ui._cached_configuration_evidence(file_a, file_b, mapping)
        requested = ui._inspect_possible_key_differences(file_a, file_b, mapping)
        ownership = _completed_state(state)
        before = calls.copy()
        for mode in ReconciliationMode:
            state.update(reconciliation_mode=mode, amount_tolerance="0.01", norm_casefold_0=True)
            renamed_a = _input(Source.A, data_a, name=f"renamed-{cycle}.csv")
            renamed_b = _input(Source.B, data_b, name=f"other-{cycle}.csv")
            assert ui._cached_suggestions(renamed_a, renamed_b) is suggestions
            assert ui._cached_configuration_evidence(renamed_a, renamed_b, mapping) is original
            assert ui._inspect_possible_key_differences(renamed_a, renamed_b, mapping) is requested
        assert calls == before
        amount_changed = replace(mapping, amount_a="alternate", amount_b="alternate")
        assert ui._cached_configuration_evidence(file_a, file_b, amount_changed) is not original
        assert ui._current_possible_key_differences(file_a, file_b, amount_changed) is requested
        reordered = replace(amount_changed, key_pairs=tuple(reversed(mapping.key_pairs)))
        ui._cached_configuration_evidence(file_a, file_b, reordered)
        searches = calls["find_possible_key_differences"]
        assert ui._current_possible_key_differences(file_a, file_b, reordered) is None
        assert calls["find_possible_key_differences"] == searches
        changed_request = ui._inspect_possible_key_differences(file_a, file_b, reordered)
        assert changed_request is not requested
        restored = ui._cached_configuration_evidence(file_a, file_b, mapping)
        assert restored is not original
        assert ui._current_possible_key_differences(file_a, file_b, mapping) is None
        assert ui._inspect_possible_key_differences(file_a, file_b, mapping) is not requested
        assert ui._cached_suggestions(file_a, file_b) is suggestions
        assert all(state[key] is value for key, value in ownership.items())
        assert {
            key
            for key in state
            if key.startswith(("_discovery_", "_configuration_", "_possible_key_"))
        } == cache_keys
        assert state["_discovery_source_A"].identity[1] == data_a
        assert state["_discovery_source_B"].identity[1] == data_b
        assert state["_configuration_evidence"].identity[:2] == suggestions.identity
        assert state["_possible_key_differences"].identity[:2] == suggestions.identity
        assert calls == {
            "discover_csv": 2 * (cycle + 1),
            "suggest_key_mappings": cycle + 1,
            "suggest_amount_mappings": cycle + 1,
            "compare_key_mapping": 4 * (cycle + 1),
            "compare_amount_columns": 4 * (cycle + 1),
            "find_possible_key_differences": 3 * (cycle + 1),
        }
