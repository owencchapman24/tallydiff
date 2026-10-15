"""Conservative exact baseline differences are advisory, bounded, and unambiguous."""

import csv
from dataclasses import FrozenInstanceError, fields, replace
from io import StringIO

import pytest

import tallydiff
import tallydiff.discovery as discovery
from tallydiff.models import Source


def _snapshot(rows, source, width=2):
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow([f"{source.value}{index}" for index in range(width)])
    writer.writerows(rows)
    return discovery.discover_csv(stream.getvalue(), source=source)


def _find(rows_a, rows_b, width=2):
    a = _snapshot(rows_a, Source.A, width)
    b = _snapshot(rows_b, Source.B, width)
    pairs = tuple((f"A{index}", f"B{index}") for index in range(width))
    return discovery.find_possible_key_differences(a, b, key_pairs=pairs)


def test_exact_one_component_difference_preserves_original_baseline_values_and_columns():
    result = _find([("Vendor-17", 'INV-/"002\nΩ')], [("Vendor-17", "INV-003")])
    assert result.evaluated and result.skip_reason is None
    assert result.total_candidate_count == 1
    assert result.signature_entry_count == 4
    assert not result.display_truncated and not result.ambiguity_suppressed
    assert result.candidates == (
        discovery.PossibleKeyDifference(
            ("Vendor-17", 'INV-/"002\nΩ'), ("Vendor-17", "INV-003"), 1, "A1", "B1"
        ),
    )


@pytest.mark.parametrize(
    "rows_a,rows_b",
    [
        ([("V", "I")], [("V", "I")]),
        ([("V", "I")], [("W", "J")]),
        ([("Vendor-17", "INV-002")], [("vendor17", "INV002")]),
        ([("x", "y")], [("y", "z")]),
        ([("V", "")], [("V", "I")]),
        ([("", "I")], [("V", "I")]),
        ([(" ", "I")], [("V", "I")]),
        ([], []),
        ([], [("V", "I")]),
    ],
)
def test_exact_matches_multiple_differences_missing_values_and_similarity_do_not_emit(
    rows_a, rows_b
):
    result = _find(rows_a, rows_b)
    assert result.evaluated and result.skip_reason is None
    assert result.candidates == () and result.total_candidate_count == 0
    assert not result.display_truncated


def test_complete_matches_are_removed_before_signature_ambiguity_analysis():
    result = _find([("V", "same"), ("V", "left")], [("V", "same"), ("V", "right")])
    assert result.total_candidate_count == 1
    assert result.candidates[0].key_a == ("V", "left")
    assert result.candidates[0].key_b == ("V", "right")
    assert result.signature_entry_count == 4
    assert not result.ambiguity_suppressed


@pytest.mark.parametrize("repeat_side", [Source.A, Source.B])
def test_repeated_keys_are_excluded_without_mutating_source_observations(repeat_side):
    rows_a = [("V", "left")] * (2 if repeat_side is Source.A else 1)
    rows_b = [("V", "right")] * (2 if repeat_side is Source.B else 1)
    a, b = _snapshot(rows_a, Source.A), _snapshot(rows_b, Source.B)
    before = (a, b, hash(a), hash(b), a._key_rows, b._key_rows)
    result = discovery.find_possible_key_differences(a, b, key_pairs=(("A0", "B0"), ("A1", "B1")))
    assert result.candidates == ()
    assert result.signature_entry_count == 4  # Distinct repeated keys remain evidence.
    assert before == (a, b, hash(a), hash(b), a._key_rows, b._key_rows)
    repeated = a if repeat_side is Source.A else b
    assert repeated.profile.row_count == len(repeated._key_rows) == 2


@pytest.mark.parametrize(
    "rows_a,rows_b",
    [
        ([("V", "left")], [("V", "right1"), ("V", "right2")]),
        ([("V", "left1"), ("V", "left2")], [("V", "right")]),
        ([("P", "1")], [("P", "2"), ("Q", "1")]),
        ([("P", "1"), ("Q", "1")], [("R", "1"), ("P", "2")]),
        ([("X", "1"), ("Y", "2")], [("X", "2"), ("Z", "1"), ("W", "1")]),
        ([("P", "1"), ("P", "3"), ("P", "3")], [("P", "2")]),
        ([("P", "1"), ("P", "1"), ("Q", "2")], [("P", "2")]),
    ],
)
def test_cross_signature_ambiguity_and_repeated_alternatives_suppress_apparent_unique_pairs(
    rows_a, rows_b
):
    result = _find(rows_a, rows_b)
    assert result.evaluated and result.ambiguity_suppressed
    assert result.candidates == () and result.total_candidate_count == 0


def test_same_side_signature_without_cross_file_evidence_does_not_suppress_other_candidates():
    result = _find([("P", "1"), ("P", "3")], [("Q", "1"), ("R", "3")])
    assert result.total_candidate_count == 2
    assert not result.ambiguity_suppressed
    assert [candidate.component_index for candidate in result.candidates] == [0, 0]


@pytest.mark.parametrize("width", [3, 4, 7])
def test_any_selected_composite_cardinality_uses_exact_order_and_one_omitted_position(width):
    left = tuple(f"value{index}" for index in range(width))
    right = tuple("different" if index == width - 2 else value for index, value in enumerate(left))
    result = _find([left], [right], width)
    assert result.total_candidate_count == 1
    assert result.candidates[0].component_index == width - 2
    assert result.candidates[0].key_a == left and result.candidates[0].key_b == right
    assert result.signature_entry_count == width * 2


def test_result_order_is_component_then_original_keys_and_independent_of_row_order():
    a = [("ZZ", "fixed"), ("vendor-b", "left"), ("vendor-a", "left")]
    b = [("AA", "fixed"), ("vendor-b", "right"), ("vendor-a", "right")]
    result = _find(a, b)
    assert result == _find(list(reversed(a)), [b[1], b[2], b[0]])
    assert [candidate.component_index for candidate in result.candidates] == [0, 1, 1]
    assert [candidate.key_a for candidate in result.candidates] == [
        ("ZZ", "fixed"),
        ("vendor-a", "left"),
        ("vendor-b", "left"),
    ]


def test_twenty_display_limit_preserves_exact_total_and_deterministic_first_candidates():
    a = [(f"V{index:03}", "left") for index in reversed(range(25))]
    b = [(f"V{index:03}", "right") for index in range(25)]
    result = _find(a, b)
    assert result.total_candidate_count == 25
    assert result.display_truncated and len(result.candidates) == 20
    assert [candidate.key_a[0] for candidate in result.candidates] == [
        f"V{index:03}" for index in range(20)
    ]
    assert not _find(a[:20], b[5:]).display_truncated


def _repeated_snapshot(source, row_count):
    snapshot = _snapshot([("V", "I")], source)
    columns = tuple(
        replace(
            column,
            row_count=row_count,
            nonblank_count=row_count,
            key_eligible_count=row_count,
            duplicate_excess_count=row_count - 1,
        )
        for column in snapshot.profile.columns
    )
    return discovery.DiscoverySnapshot(
        replace(snapshot.profile, row_count=row_count, columns=columns),
        snapshot._key_rows * row_count,
    )


@pytest.mark.parametrize("oversized_side", [Source.A, Source.B])
def test_source_row_limit_is_checked_before_key_counts(oversized_side, monkeypatch):
    a = _repeated_snapshot(Source.A, 25_001 if oversized_side is Source.A else 1)
    b = _repeated_snapshot(Source.B, 25_001 if oversized_side is Source.B else 1)

    def fail(*args, **kwargs):
        raise AssertionError("row limit must precede additional key indexes")

    monkeypatch.setattr(discovery, "_key_counts", fail)
    result = discovery.find_possible_key_differences(a, b, key_pairs=(("A0", "B0"), ("A1", "B1")))
    assert not result.evaluated and result.skip_reason == "source_row_limit"
    assert result.signature_entry_count is None
    assert not result.candidates and not result.display_truncated


def test_exact_source_row_limit_is_inclusive():
    a, b = _repeated_snapshot(Source.A, 25_000), _repeated_snapshot(Source.B, 25_000)
    result = discovery.find_possible_key_differences(a, b, key_pairs=(("A0", "B0"), ("A1", "B1")))
    assert result.evaluated and result.signature_entry_count == 0


@pytest.mark.parametrize("limit,evaluated", [(5, False), (6, True)])
def test_signature_budget_is_calculated_before_indexes_and_includes_boundary(
    limit, evaluated, monkeypatch
):
    original = discovery._key_difference_signature_index
    calls = []

    def record(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(discovery, "MAX_KEY_DIFFERENCE_SIGNATURE_ENTRIES", limit)
    monkeypatch.setattr(discovery, "_key_difference_signature_index", record)
    result = _find([("V", "I", "left")], [("V", "I", "right")], 3)
    assert result.evaluated is evaluated
    assert result.signature_entry_count == 6
    assert len(calls) == (2 if evaluated else 0)
    assert result.skip_reason == (None if evaluated else "signature_entry_limit")


def test_real_signature_budget_boundary_and_ordinary_twenty_thousand_workload(monkeypatch):
    assert discovery.MAX_KEY_DIFFERENCE_ROWS == 25_000
    assert discovery.MAX_KEY_DIFFERENCE_SIGNATURE_ENTRIES == 100_000
    assert discovery.MAX_KEY_DIFFERENCE_CANDIDATES == 20
    calls = []

    def empty_index(keys, prefixes, suffixes):
        calls.append(len(keys))
        return {}

    monkeypatch.setattr(discovery, "_key_difference_signature_index", empty_index)
    for rows, width, expected, evaluated in (
        (20_000, 2, 80_000, True),
        (25_000, 2, 100_000, True),
        (16_667, 3, 100_002, False),
    ):
        a = _snapshot(
            [tuple(f"a-{i}-{j}" for j in range(width)) for i in range(rows)], Source.A, width
        )
        b = _snapshot(
            [tuple(f"b-{i}-{j}" for j in range(width)) for i in range(rows)], Source.B, width
        )
        before = len(calls)
        result = discovery.find_possible_key_differences(
            a, b, key_pairs=tuple((f"A{j}", f"B{j}") for j in range(width))
        )
        assert result.signature_entry_count == expected and result.evaluated is evaluated
        assert len(calls) - before == (2 if evaluated else 0)


def test_compact_signature_ids_are_exact_across_files_and_linear_in_wide_keys(monkeypatch):
    width = 1001
    left = tuple(f"component {index}" for index in range(width))
    right = tuple("different" if index == 500 else value for index, value in enumerate(left))
    prefixes, suffixes = {}, {}
    index_a = discovery._key_difference_signature_index((left,), prefixes, suffixes)
    index_b = discovery._key_difference_signature_index((right,), prefixes, suffixes)
    common = set(index_a) & set(index_b)
    assert len(common) == 1 and next(iter(common))[0] == 500
    assert len(prefixes) <= width * 2 and len(suffixes) <= width * 2
    assert all(
        len(signature) == 3 and all(type(value) is int for value in signature)
        for signature in index_a
    )

    def fail(*args, **kwargs):
        raise AssertionError("valid selected names must use constant-time ordinal lookups")

    monkeypatch.setattr(discovery, "_column_index", fail)
    assert _find([left], [right], width).total_candidate_count == 1


def test_single_component_is_explicitly_not_evaluated_without_counting(monkeypatch):
    a, b = _snapshot([("left",)], Source.A, 1), _snapshot([("right",)], Source.B, 1)

    def fail(*args, **kwargs):
        raise AssertionError("single-component selection must not count or index keys")

    monkeypatch.setattr(discovery, "_key_counts", fail)
    result = discovery.find_possible_key_differences(a, b, key_pairs=(("A0", "B0"),))
    assert not result.evaluated and result.skip_reason == "requires_composite_key"
    assert result.signature_entry_count is None and result.total_candidate_count == 0


def test_xlsx_and_csv_observations_use_existing_canonical_keys_and_ineligible_cells(xlsx_bytes):
    a = _snapshot([("V", "1"), ("bad", "1")], Source.A)
    xlsx = xlsx_bytes(
        {"Data": [["B0", "B1"], ["V", 2], ["bad", True], ["bad", "=1+1"], ["bad", "#N/A"]]}
    )
    b = discovery.discover_xlsx(xlsx, source=Source.B, worksheet="Data")
    result = discovery.find_possible_key_differences(a, b, key_pairs=(("A0", "B0"), ("A1", "B1")))
    assert result == _find([("V", "1"), ("bad", "1")], [("V", "2")])
    assert result.candidates[0].key_b == ("V", "2")


@pytest.mark.parametrize(
    "selection",
    [
        None,
        "A0",
        [],
        [("A0",)],
        [("missing", "B0")],
        [("A0", "B0"), ("A0", "B1")],
        [("A0", "B0"), ("A1", "B0")],
        [(None, "B0")],
        [("A0", None)],
    ],
)
def test_selection_validation_preserves_existing_compare_errors(selection):
    a, b = _snapshot([("V", "I")], Source.A), _snapshot([("V", "J")], Source.B)
    with pytest.raises((TypeError, ValueError)) as expected:
        discovery.compare_key_mapping(a, b, key_pairs=selection)
    with pytest.raises(type(expected.value), match=f"^{str(expected.value)}$"):
        discovery.find_possible_key_differences(a, b, key_pairs=selection)


@pytest.mark.parametrize(
    "directions", [(Source.B, Source.A), (Source.A, Source.A), (Source.B, Source.B)]
)
def test_wrong_snapshot_direction_is_rejected(directions):
    with pytest.raises(ValueError, match="snapshot_a must be Source.A"):
        discovery.find_possible_key_differences(
            _snapshot([("V", "I")], directions[0]),
            _snapshot([("V", "J")], directions[1]),
            key_pairs=(("A0", "B0"), ("A1", "B1")),
        )


def test_results_are_frozen_slotted_distinct_from_findings_and_not_root_exports():
    result = _find([("V", "I")], [("V", "J")])
    assert tuple(field.name for field in fields(result)) == (
        "key_pairs",
        "candidates",
        "total_candidate_count",
        "display_truncated",
        "evaluated",
        "skip_reason",
        "ambiguity_suppressed",
        "signature_entry_count",
    )
    for value, attribute in ((result, "evaluated"), (result.candidates[0], "column_a")):
        assert not hasattr(value, "__dict__") and isinstance(hash(value), int)
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, None)
    assert not isinstance(result.candidates[0], tallydiff.ReconciliationFinding)
    assert {
        "PossibleKeyDifference",
        "PossibleKeyDifferenceResult",
        "find_possible_key_differences",
    }.isdisjoint(tallydiff.__all__)


@pytest.mark.parametrize("wrong", [None, {}, "snapshot", 1])
@pytest.mark.parametrize("side", [0, 1])
def test_invalid_snapshot_types_raise_visible_type_errors(wrong, side):
    snapshots = [_snapshot([("V", "I")], Source.A), _snapshot([("V", "J")], Source.B)]
    snapshots[side] = wrong
    with pytest.raises(TypeError, match="must be a DiscoverySnapshot"):
        discovery.find_possible_key_differences(*snapshots, key_pairs=(("A0", "B0"), ("A1", "B1")))


def test_detector_reuses_observations_without_reingestion_or_normalization(monkeypatch):
    a, b = (
        _snapshot([("Vendor-17", "INV-002")], Source.A),
        _snapshot([("Vendor-17", "INV-003")], Source.B),
    )

    def fail(*args, **kwargs):
        raise AssertionError("detector must consume existing baseline observations only")

    for name in ("discover_csv", "discover_xlsx", "parse_amount", "_open", "_key", "_amount"):
        monkeypatch.setattr(discovery, name, fail)
    assert (
        discovery.find_possible_key_differences(
            a, b, key_pairs=(("A0", "B0"), ("A1", "B1"))
        ).total_candidate_count
        == 1
    )


def test_twenty_thousand_two_component_keys_are_fully_evaluated_with_bounded_display():
    result = _find(
        [(f"V{index:05}", "LEFT") for index in range(20_000)],
        [(f"V{index:05}", "RIGHT") for index in range(20_000)],
    )
    assert result.evaluated and result.skip_reason is None
    assert result.signature_entry_count == 80_000
    assert result.total_candidate_count == 20_000
    assert result.display_truncated and len(result.candidates) == 20
    assert not result.ambiguity_suppressed
    assert [candidate.key_a for candidate in result.candidates] == [
        (f"V{index:05}", "LEFT") for index in range(20)
    ]
    assert [candidate.key_b for candidate in result.candidates] == [
        (f"V{index:05}", "RIGHT") for index in range(20)
    ]


def test_large_ambiguous_signature_group_retains_linear_entries_and_only_constructs_clean_pair(
    monkeypatch,
):
    original_index = discovery._key_difference_signature_index
    original_candidate = discovery.PossibleKeyDifference
    indexed_sizes = []
    constructed = []

    def record_index(keys, prefixes, suffixes):
        result = original_index(keys, prefixes, suffixes)
        indexed_sizes.append(
            (len(keys), sum(map(len, result.values())), max(map(len, result.values())))
        )
        return result

    def record_candidate(*args):
        constructed.append(args)
        return original_candidate(*args)

    monkeypatch.setattr(discovery, "_key_difference_signature_index", record_index)
    monkeypatch.setattr(discovery, "PossibleKeyDifference", record_candidate)
    rows_a = [("shared", f"left-{index}") for index in range(128)] + [("clean", "left")]
    rows_b = [("shared", f"right-{index}") for index in range(128)] + [("clean", "right")]
    result = _find(rows_a, rows_b)
    assert indexed_sizes == [(129, 258, 128), (129, 258, 128)]
    assert result.evaluated and result.ambiguity_suppressed
    assert result.signature_entry_count == 516
    assert result.total_candidate_count == len(result.candidates) == 1
    assert not result.display_truncated
    assert constructed == [(("clean", "left"), ("clean", "right"), 1, "A1", "B1")]


def test_duplicate_heavy_rows_use_distinct_key_budget_without_losing_unique_evidence(monkeypatch):
    rows_a = [("repeat", "left")] * 500 + [("clean", "left")]
    rows_b = [("repeat", "right")] * 400 + [("clean", "right")]
    a, b = _snapshot(rows_a, Source.A), _snapshot(rows_b, Source.B)
    before = (a._key_rows, b._key_rows, hash(a), hash(b))
    original_index = discovery._key_difference_signature_index
    indexed_keys = []

    def record_index(keys, prefixes, suffixes):
        indexed_keys.append(keys)
        return original_index(keys, prefixes, suffixes)

    monkeypatch.setattr(discovery, "MAX_KEY_DIFFERENCE_SIGNATURE_ENTRIES", 8)
    monkeypatch.setattr(discovery, "_key_difference_signature_index", record_index)
    result = discovery.find_possible_key_differences(a, b, key_pairs=(("A0", "B0"), ("A1", "B1")))
    assert result.evaluated and result.signature_entry_count == 8
    assert [len(keys) for keys in indexed_keys] == [2, 2]
    assert result.candidates == (
        discovery.PossibleKeyDifference(("clean", "left"), ("clean", "right"), 1, "A1", "B1"),
    )
    assert result.total_candidate_count == 1
    assert a.profile.row_count == 501 and b.profile.row_count == 401
    assert a._key_rows.count(("repeat", "left")) == 500
    assert b._key_rows.count(("repeat", "right")) == 400
    assert before == (a._key_rows, b._key_rows, hash(a), hash(b))


@pytest.mark.parametrize(
    "rows_a,rows_b",
    (
        ([], []),
        ([("X", "1")] * 4 + [("Y", "2")] * 2, [("Y", "2"), ("X", "1")]),
        ([(" ", "present"), ("present", "\t")], [("present", " "), ("\t", "present")]),
    ),
)
def test_matched_or_ineligible_keys_consume_no_signature_budget(rows_a, rows_b, monkeypatch):
    original_index = discovery._key_difference_signature_index
    indexed_keys = []

    def record_index(keys, prefixes, suffixes):
        indexed_keys.append(keys)
        return original_index(keys, prefixes, suffixes)

    monkeypatch.setattr(discovery, "MAX_KEY_DIFFERENCE_SIGNATURE_ENTRIES", 0)
    monkeypatch.setattr(discovery, "_key_difference_signature_index", record_index)
    result = _find(rows_a, rows_b)
    assert indexed_keys == [(), ()]
    assert result.evaluated and result.skip_reason is None
    assert result.signature_entry_count == 0
    assert result.candidates == () and result.total_candidate_count == 0
    assert not result.display_truncated and not result.ambiguity_suppressed


def test_mixed_four_component_evidence_preserves_exact_count_and_order_under_permutations():
    rows_a = []
    rows_b = []
    expected = []
    pairs = tuple((f"A{index}", f"B{index}") for index in range(4))
    for group in range(25):
        left = tuple(f"C{group:02}-{component}" for component in range(4))
        differing = group % 4
        right = tuple(
            f"changed-{value}" if component == differing else value
            for component, value in enumerate(left)
        )
        rows_a.append(left)
        rows_b.append(right)
        expected.append(discovery.PossibleKeyDifference(left, right, differing, *pairs[differing]))
    rows_a.extend(
        [
            ("ambiguous-0", "ambiguous-1", "ambiguous-2", "left"),
            *(("duplicate-0", "duplicate-1", "duplicate-2", "left"),) * 3,
            ("exact-0", "exact-1", "exact-2", "exact-3"),
            ("missing-0", "missing-1", "missing-2", " "),
        ]
    )
    rows_b.extend(
        [
            ("ambiguous-0", "ambiguous-1", "ambiguous-2", "right-1"),
            ("ambiguous-0", "ambiguous-1", "ambiguous-2", "right-2"),
            ("duplicate-0", "duplicate-1", "duplicate-2", "right"),
            ("exact-0", "exact-1", "exact-2", "exact-3"),
            ("missing-0", "missing-1", "missing-2", "\t"),
        ]
    )
    expected.sort(
        key=lambda candidate: (candidate.component_index, candidate.key_a, candidate.key_b)
    )
    result = _find(rows_a, rows_b, 4)
    assert result.evaluated and result.ambiguity_suppressed
    assert result.signature_entry_count == 220
    assert result.total_candidate_count == 25
    assert result.display_truncated and result.candidates == tuple(expected[:20])
    permutations = (
        (list(reversed(rows_a)), list(reversed(rows_b))),
        (rows_a[7:] + rows_a[:7], rows_b[1::2] + rows_b[::2]),
        (rows_a[::2] + rows_a[1::2], rows_b[11:] + rows_b[:11]),
    )
    for permuted_a, permuted_b in permutations:
        assert _find(permuted_a, permuted_b, 4) == result
    a, b = _snapshot(rows_a, Source.A, 4), _snapshot(rows_b, Source.B, 4)
    selected_order = (2, 0, 3, 1)
    reordered_pairs = tuple(pairs[index] for index in selected_order)
    reordered = discovery.find_possible_key_differences(a, b, key_pairs=reordered_pairs)
    expected_reordered = sorted(
        (
            discovery.PossibleKeyDifference(
                tuple(candidate.key_a[index] for index in selected_order),
                tuple(candidate.key_b[index] for index in selected_order),
                selected_order.index(candidate.component_index),
                candidate.column_a,
                candidate.column_b,
            )
            for candidate in expected
        ),
        key=lambda candidate: (candidate.component_index, candidate.key_a, candidate.key_b),
    )
    assert reordered.key_pairs == reordered_pairs
    assert reordered.total_candidate_count == result.total_candidate_count
    assert reordered.signature_entry_count == result.signature_entry_count
    assert reordered.candidates == tuple(expected_reordered[:20])


@pytest.mark.parametrize(
    "left,right",
    (
        (("ab", "c", "shared"), ("a", "bc", "shared")),
        (("a|b", "c", "shared"), ("a", "b|c", "shared")),
        (("α|β", "雪", "shared"), ("α", "β|雪", "shared")),
    ),
)
def test_omitted_component_signatures_do_not_collide_through_concatenation(left, right):
    result = _find([left], [right], 3)
    assert result.evaluated and result.signature_entry_count == 6
    assert result.candidates == () and result.total_candidate_count == 0
    assert not result.ambiguity_suppressed


@pytest.mark.parametrize("format_name", ["csv", "xlsx"])
def test_invalid_unselected_amount_evidence_cannot_change_possible_key_differences(
    xlsx_bytes, format_name
):
    rows_a = [(f"V{index}", "left", "10") for index in range(3)]
    rows_b = [(f"V{index}", "right", value) for index, value in enumerate(("NaN", "#N/A", "=1+1"))]
    a = _snapshot(rows_a, Source.A, 3)
    if format_name == "csv":
        b = _snapshot(rows_b, Source.B, 3)
    else:
        data = xlsx_bytes({"Data": [["B0", "B1", "B2"], *rows_b]})
        b = discovery.discover_xlsx(data, source=Source.B, worksheet="Data")
    before = (a._key_rows, b._key_rows, hash(a), hash(b))
    pairs = (("A0", "B0"), ("A1", "B1"))
    amount = discovery.compare_amount_columns(a, b, column_a="A2", column_b="B2")
    key = discovery.compare_key_mapping(a, b, key_pairs=pairs)
    result = discovery.find_possible_key_differences(a, b, key_pairs=pairs)
    assert amount.parseable_count_a == 3 and amount.parseable_count_b == 0
    assert amount.total_b is None and amount.control_difference is None
    assert key.eligible_row_count_a == key.eligible_row_count_b == 3
    assert key.overlap_distinct_count == 0
    assert result.evaluated and result.total_candidate_count == 3
    assert result.signature_entry_count == 12
    assert [(candidate.key_a, candidate.key_b) for candidate in result.candidates] == [
        ((f"V{index}", "left"), (f"V{index}", "right")) for index in range(3)
    ]
    assert before == (a._key_rows, b._key_rows, hash(a), hash(b))
