"""Independent scale acceptance for persisted analyst reviews, without UI or timing gates."""

import ast
import csv
import inspect
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from scripts import review_benchmark
from scripts import review_oracle as oracle
from tallydiff import (
    ColumnMapping,
    FindingCategory,
    FindingReview,
    ReconciliationMode,
    ReviewDisposition,
    ReviewSession,
    ReviewSessionError,
    Source,
    bind_review_result,
    export_exceptions_csv,
    export_review_session,
    export_reviewed_exceptions_csv,
    ingest_csv,
    load_review_session,
    reconcile,
)

MAPPING = ColumnMapping((("ledger_id", "payment_id"),), "booked_amount", "settled_amount")
COUNTS_20K = {
    "total": 20_000,
    "unreviewed_count": 6_667,
    "needs_follow_up_count": 6_667,
    "complete_count": 6_666,
    "reviewed_count": 13_333,
    "open_count": 13_334,
}
NOTES = (
    "",
    "Analyst checked source records.",
    "  leading and trailing whitespace  ",
    "Unicode: caf\u00e9 / \u6771\u4eac",
    "Combining: cafe\u0301",
    "Emoji: \U0001f9fe \u2705",
    "line one\r\nline two\rline three\nline four",
    "=SUM(A1:A2)",
    "+123",
    "-123",
    "@lookup",
    'Comma, and "quoted" text',
    "Backslash: C:\\review\\source",
)
DISPOSITIONS = ("unreviewed", "needs_follow_up", "review_complete")


def _rows(data):
    return list(csv.reader(StringIO(data.decode("utf-8"), newline=""), strict=True))


def _csv_bytes(rows):
    stream = StringIO(newline="")
    csv.writer(stream, lineterminator="\r\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def _build(groups):
    data_a, data_b = oracle.generate_inputs(groups)
    rows_a = ingest_csv(
        data_a.decode("utf-8"),
        source=Source.A,
        key_columns=("ledger_id",),
        amount_column="booked_amount",
    )
    rows_b = ingest_csv(
        data_b.decode("utf-8"),
        source=Source.B,
        key_columns=("payment_id",),
        amount_column="settled_amount",
    )
    result = reconcile(rows_a, rows_b, mode=ReconciliationMode.UNIQUE)
    binding = _bind(result, data_a, data_b)
    return data_a, data_b, result, binding


def _bind(result, data_a, data_b):
    return bind_review_result(
        result,
        data_a=data_a,
        data_b=data_b,
        mapping=MAPPING,
        format_a="csv",
        format_b="csv",
    )


def _populate(binding, expected):
    return ReviewSession(
        binding,
        tuple(
            FindingReview(item.matching_key, ReviewDisposition(item.disposition), item.note)
            for item in expected
        ),
    )


def _record_evidence(record):
    return (
        id(record),
        record.source,
        record.source_row,
        record.key,
        record.amount,
        id(record.raw_fields),
        tuple(record.raw_fields.items()),
    )


def _truth(result):
    """Include evidence omitted from dataclass equality, and every original reference."""

    return (
        id(result),
        id(result.findings),
        tuple(
            (
                id(finding),
                finding.key,
                finding.category,
                finding.amount_a,
                finding.amount_b,
                finding.delta,
                id(finding.rows_a),
                id(finding.rows_b),
                tuple(_record_evidence(record) for record in finding.rows_a),
                tuple(_record_evidence(record) for record in finding.rows_b),
                finding.field_comparisons,
                finding.correspondence_analysis,
            )
            for finding in result.findings
        ),
        tuple(id(finding) for finding in result.exceptions),
        result.total_a,
        result.total_b,
        result.control_difference,
        result.finding_delta_sum,
        result.amount_tolerance,
        result.mode,
        result.key_normalization,
        result.comparison_fields,
        result.one_to_many_policy,
    )


def _assert_counts(session, expected):
    assert {name: getattr(session, name) for name in expected} == expected


def _artifacts(groups=13):
    _, _, result, binding = _build(groups)
    expected = oracle.expected_reviews(groups)
    session = _populate(binding, expected)
    return (
        export_review_session(session),
        export_exceptions_csv(result),
        export_reviewed_exceptions_csv(binding, session),
        expected,
        binding.result_fingerprint,
    )


def test_twenty_thousand_exception_review_workflow_is_deterministic_complete_and_immutable():
    groups = 20_000
    # Expected keys, annotations, and counts exist before any review serialization.
    expected = oracle.expected_reviews(groups)
    expected_by_key = {item.matching_key: item for item in expected}
    expected_counts = oracle.expected_counts(groups)
    assert expected_counts == COUNTS_20K
    assert len(expected_by_key) == groups
    data_a, data_b, result, binding = _build(groups)
    assert result.mode is ReconciliationMode.UNIQUE
    assert len(result.findings) == len(result.exceptions) == len(binding.exception_keys) == groups
    assert binding.exception_keys == tuple((f"ID{index:06d}",) for index in range(groups))
    expected_total_a = Decimal(sum(10_000 + index % 97 for index in range(groups))) / 100
    assert result.total_a == expected_total_a
    assert result.total_b == expected_total_a - Decimal(groups) / 100
    assert result.control_difference == result.finding_delta_sum == Decimal("200.00")
    for index, finding in enumerate(result.findings):
        assert finding.key == expected[index].matching_key
        assert finding.category is FindingCategory.AMOUNT_MISMATCH
        assert finding.delta == Decimal("0.01")
        assert len(finding.rows_a) == len(finding.rows_b) == 1
        assert finding.rows_a[0].source_row == groups - index + 1
        expected_b_position = index // 2 + (0 if index % 2 == 0 else (groups + 1) // 2)
        assert finding.rows_b[0].source_row == expected_b_position + 2
    findings = result.findings
    exceptions = result.exceptions
    fingerprint = binding.result_fingerprint
    original_csv = export_exceptions_csv(result)
    before = _truth(result)

    def unchanged():
        assert binding.result is result
        assert result.findings is findings
        assert all(
            current is original
            for current, original in zip(result.exceptions, exceptions, strict=True)
        )
        assert _truth(result) == before
        assert export_exceptions_csv(result) == original_csv
        assert binding.result_fingerprint == fingerprint

    repeated_binding = _bind(result, data_a, data_b)
    assert repeated_binding is not binding and repeated_binding.result is result
    assert repeated_binding.result_fingerprint == fingerprint
    assert repeated_binding.exception_keys == binding.exception_keys
    unchanged()

    empty_annotations = ReviewSession.unreviewed(binding)
    assert empty_annotations.binding is binding
    assert empty_annotations.total == empty_annotations.unreviewed_count == groups
    assert all(review.note == "" for review in empty_annotations.reviews)
    unchanged()

    session = _populate(binding, expected)
    assert session.binding is binding
    assert len(session.reviews) == groups
    assert tuple(review.matching_key for review in session.reviews) == binding.exception_keys
    _assert_counts(session, expected_counts)
    for review in session.reviews:
        item = expected_by_key[review.matching_key]
        assert (review.disposition.value, review.note) == (item.disposition, item.note)
    unchanged()

    session_json = export_review_session(session)
    assert export_review_session(session) == session_json
    unchanged()

    loaded = load_review_session(session_json, binding=binding)
    assert loaded.binding is binding
    assert loaded == session
    assert export_review_session(loaded) == session_json
    _assert_counts(loaded, expected_counts)
    unchanged()

    reviewed_csv = export_reviewed_exceptions_csv(binding, loaded)
    assert export_reviewed_exceptions_csv(binding, session) == reviewed_csv
    unchanged()

    summary = oracle.verify_artifacts(
        session_json, original_csv, reviewed_csv, expected, fingerprint
    )
    assert {name: summary[name] for name in expected_counts} == expected_counts
    assert summary["original_rows"] == summary["reviewed_rows"] == groups
    assert summary["json_bytes"] == len(session_json)
    assert summary["original_csv_bytes"] == len(original_csv)
    assert summary["reviewed_csv_bytes"] == len(reviewed_csv)
    unchanged()

    # A hypothetical visible subset never replaces the complete export population.
    visible_reviews = session.reviews[101:104]
    assert len(visible_reviews) == 3
    assert len(json.loads(session_json)["reviews"]) == groups
    assert len(_rows(reviewed_csv)) - 1 == groups
    assert empty_annotations.binding.result_fingerprint == session.binding.result_fingerprint


def test_fixture_rules_are_independently_checked_before_using_the_oracle():
    groups = 14
    data_a, data_b = oracle.generate_inputs(groups)
    assert oracle.generate_inputs(groups) == (data_a, data_b)
    a, b = _rows(data_a), _rows(data_b)
    assert a[0] == ["ledger_id", "booked_amount", "evidence_a"]
    assert b[0] == ["payment_id", "settled_amount", "evidence_b"]
    assert [row[0] for row in a[1:]] == [f"ID{index:06d}" for index in reversed(range(groups))]
    b_order = (*range(0, groups, 2), *range(1, groups, 2))
    assert [row[0] for row in b[1:]] == [f"ID{index:06d}" for index in b_order]
    for rows, offset in ((a[1:], 0), (b[1:], -1)):
        for row in rows:
            index = int(row[0][2:])
            assert Decimal(row[1]) == Decimal(10_000 + index % 97 + offset) / 100
            assert row[2]
    expected = oracle.expected_reviews(groups)
    assert tuple(item.matching_key for item in expected) == tuple(
        (f"ID{index:06d}",) for index in range(groups)
    )
    assert tuple(item.disposition for item in expected) == tuple(
        DISPOSITIONS[index % 3] for index in range(groups)
    )
    assert tuple(item.note for item in expected) == NOTES + ("",)
    assert oracle.expected_counts(groups) == {
        "total": 14,
        "unreviewed_count": 5,
        "needs_follow_up_count": 5,
        "complete_count": 4,
        "reviewed_count": 9,
        "open_count": 10,
    }
    with pytest.raises(FrozenInstanceError):
        expected[0].note = "changed"
    assert not hasattr(expected[0], "__dict__")


def test_oracle_has_only_explicit_standard_library_imports():
    source = Path(oracle.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module
            roots.append(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            assert not (isinstance(node.func, ast.Name) and node.func.id == "__import__")
            assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
    assert roots
    assert set(roots) <= sys.stdlib_module_names
    assert "tallydiff" not in roots and "streamlit" not in roots


def test_one_source_byte_changes_fingerprint_and_filenames_are_absent():
    data_a, data_b, result, binding = _build(13)
    # Change only raw evidence, preserving key/amount/result supplied to the binder.
    last_comma = data_a.rfind(b",")
    position = last_comma + 1
    assert position > 0 and data_a[position] not in b"\r\n"
    replacement = b"X" if data_a[position : position + 1] != b"X" else b"Y"
    changed = data_a[:position] + replacement + data_a[position + 1 :]
    assert len(changed) == len(data_a)
    assert sum(left != right for left, right in zip(changed, data_a, strict=True)) == 1
    other = _bind(result, changed, data_b)
    assert other.result is result
    assert other.exception_keys == binding.exception_keys
    assert other.result_fingerprint != binding.result_fingerprint
    assert all("filename" not in name for name in inspect.signature(bind_review_result).parameters)
    expected = oracle.expected_reviews(13)
    session = _populate(binding, expected)
    with pytest.raises(ReviewSessionError, match="fingerprint"):
        load_review_session(export_review_session(session), binding=other)
    with pytest.raises(ReviewSessionError, match="fingerprint"):
        export_reviewed_exceptions_csv(other, session)


@pytest.mark.parametrize("invalid", ["reordered", "omitted"])
def test_small_session_rejects_incomplete_or_reordered_population(invalid):
    _, _, _, binding = _build(5)
    reviews = _populate(binding, oracle.expected_reviews(5)).reviews
    invalid_reviews = reviews[::-1] if invalid == "reordered" else reviews[:-1]
    with pytest.raises(ReviewSessionError, match="exactly the exception keys in binding order"):
        ReviewSession(binding, invalid_reviews)


def test_smaller_exports_repeat_exactly_and_notes_are_single_logical_cells():
    data_a, data_b, result, binding = _build(13)
    expected = oracle.expected_reviews(13)
    session = _populate(binding, expected)
    json_bytes = export_review_session(session)
    original = export_exceptions_csv(result)
    reviewed = export_reviewed_exceptions_csv(binding, session)
    assert export_review_session(session) == json_bytes
    assert export_reviewed_exceptions_csv(binding, session) == reviewed
    assert _bind(result, data_a, data_b).result_fingerprint == binding.result_fingerprint
    summary = oracle.verify_artifacts(
        json_bytes, original, reviewed, expected, binding.result_fingerprint
    )
    assert summary["total"] == summary["original_rows"] == summary["reviewed_rows"] == 13
    rows = _rows(reviewed)
    assert len(rows) == 14
    assert all(len(row) == len(rows[0]) for row in rows)
    assert [row[-1] for row in rows[1:]] == [
        "" if note == "" else "Note: " + note for note in NOTES
    ]
    assert json_bytes.endswith(b"\n") and not json_bytes.endswith(b"\n\n")
    assert not json_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in json_bytes[:-1]
    assert b"caf\xc3\xa9" in json_bytes and b"\\u00e9" not in json_bytes


@pytest.mark.parametrize(
    "corruption",
    [
        "json-format",
        "json-fingerprint",
        "json-order",
        "json-note",
        "json-missing-review",
        "csv-base-cell",
        "csv-paired-base-cell",
        "csv-label",
        "csv-note",
        "csv-missing-row",
    ],
)
def test_oracle_rejects_independently_corrupted_output(corruption):
    session_json, original_csv, reviewed_csv, expected, fingerprint = _artifacts()
    if corruption.startswith("json-"):
        document = json.loads(session_json)
        if corruption == "json-format":
            document["format"] = "unexpected"
        elif corruption == "json-fingerprint":
            document["result_fingerprint"] = "0" * 64
        elif corruption == "json-order":
            document["reviews"][0], document["reviews"][1] = (
                document["reviews"][1],
                document["reviews"][0],
            )
        elif corruption == "json-note":
            document["reviews"][1]["note"] += " altered"
        elif corruption == "json-missing-review":
            document["reviews"].pop()
        session_json = (
            json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    else:
        rows = _rows(reviewed_csv)
        if corruption in ("csv-base-cell", "csv-paired-base-cell"):
            rows[1][2] = "9999"
            if corruption == "csv-paired-base-cell":
                original_rows = _rows(original_csv)
                original_rows[1][2] = "9999"
                original_csv = _csv_bytes(original_rows)
        elif corruption == "csv-label":
            rows[1][-2] = "Review complete"
        elif corruption == "csv-note":
            rows[2][-1] = "Note: altered"
        elif corruption == "csv-missing-row":
            rows.pop()
        reviewed_csv = _csv_bytes(rows)
    with pytest.raises(AssertionError):
        oracle.verify_artifacts(session_json, original_csv, reviewed_csv, expected, fingerprint)


@pytest.mark.parametrize("invocation", ["direct", "module"])
def test_small_benchmark_cli_uses_public_workflow_and_reports_without_thresholds(invocation):
    root = Path(__file__).resolve().parents[1]
    entry = (
        [str(root / "scripts" / "review_benchmark.py")]
        if invocation == "direct"
        else ["-m", "scripts.review_benchmark"]
    )
    completed = subprocess.run(
        [sys.executable, *entry, "--groups", "1"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "Python allocations observed by tracemalloc" in completed.stdout
    assert "exceptions=1" in completed.stdout
    assert "JSON=" in completed.stdout and "reviewed CSV=" in completed.stdout
    assert "binding_s=" in completed.stdout and "independent_verification_s=" in completed.stdout
    assert "Wrote " not in completed.stdout


def test_benchmark_without_output_option_does_not_write_files(monkeypatch, capsys):
    def unexpected_write(*args, **kwargs):
        pytest.fail("benchmark must not write artifacts without --output")

    monkeypatch.setattr(sys, "argv", ["review_benchmark.py", "--groups", "1"])
    monkeypatch.setattr(Path, "write_text", unexpected_write)
    review_benchmark.main()
    assert "exceptions=1" in capsys.readouterr().out
