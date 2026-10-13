"""Independent, standard-library-only truth for the review scale fixture.

This module imports no TallyDiff code. Input bytes, canonical key order, analyst
annotations, progress counts, financial CSV cells, and physical source-row
ordinals are computed from the fixture rules. Artifact verification parses JSON
and CSV directly; no product parser, serializer, or display helper supplies the
expected values. The supplied fingerprint is an opaque value to verify, rather
than a reimplementation of the frozen binding protocol.
"""

import csv
import json
from dataclasses import dataclass
from io import StringIO
from itertools import zip_longest

HEADERS_A = ("ledger_id", "booked_amount", "evidence_a")
HEADERS_B = ("payment_id", "settled_amount", "evidence_b")
DISPOSITIONS = ("unreviewed", "needs_follow_up", "review_complete")
DISPOSITION_LABELS = ("Unreviewed", "Needs follow-up", "Review complete")
NOTE_CYCLE = (
    "",
    "Analyst checked source records.",
    "  leading and trailing whitespace  ",
    "Unicode: café / 東京",
    "Combining: cafe\u0301",
    "Emoji: 🧾 ✅",
    "line one\r\nline two\rline three\nline four",
    "=SUM(A1:A2)",
    "+123",
    "-123",
    "@lookup",
    'Comma, and "quoted" text',
    "Backslash: C:\\review\\source",
)
ORIGINAL_COLUMNS = (
    "Category",
    "Matching key",
    "File A amount",
    "File B amount",
    "Delta (A - B)",
    "File A records",
    "File B records",
)
SESSION_FORMAT = "tallydiff-review-session"
SESSION_VERSION = 1
FINGERPRINT_ALGORITHM = "sha256:tallydiff-review-binding-v1"


@dataclass(frozen=True, slots=True)
class ExpectedReview:
    matching_key: tuple[str, ...]
    disposition: str
    note: str


def _check_groups(groups: int) -> None:
    if type(groups) is not int or groups < 0:
        raise ValueError("groups must be a nonnegative integer")


def _key(index: int) -> tuple[str, ...]:
    return (f"ID{index:06d}",)


def _amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def generate_inputs(groups: int) -> tuple[bytes, bytes]:
    """Produce all-mismatch UNIQUE inputs with deliberately different row orders.

    File A is in reversed key order; File B has ascending even keys followed by
    ascending odd keys. Each amount is exact cents: A = 10000 + index % 97,
    B = A - 1. Directional columns and unused raw evidence fields differ by side.
    """

    _check_groups(groups)
    output_a = StringIO(newline="")
    output_b = StringIO(newline="")
    writer_a = csv.writer(output_a, lineterminator="\r\n")
    writer_b = csv.writer(output_b, lineterminator="\r\n")
    writer_a.writerow(HEADERS_A)
    writer_b.writerow(HEADERS_B)
    for index in reversed(range(groups)):
        writer_a.writerow((_key(index)[0], _amount(10000 + index % 97), f"A evidence {index}"))
    for parity in (0, 1):
        for index in range(parity, groups, 2):
            writer_b.writerow((_key(index)[0], _amount(9999 + index % 97), f"B evidence {index}"))
    return output_a.getvalue().encode("utf-8"), output_b.getvalue().encode("utf-8")


def expected_reviews(groups: int) -> tuple[ExpectedReview, ...]:
    """Compute canonical annotation truth before any product exporter is called."""

    _check_groups(groups)
    return tuple(
        ExpectedReview(_key(index), DISPOSITIONS[index % 3], NOTE_CYCLE[index % 13])
        for index in range(groups)
    )


def expected_counts(groups: int) -> dict[str, int]:
    """Compute progress counts algebraically, without inspecting a product session."""

    _check_groups(groups)
    unreviewed = (groups + 2) // 3
    follow_up = (groups + 1) // 3
    complete = groups // 3
    return {
        "total": groups,
        "unreviewed_count": unreviewed,
        "needs_follow_up_count": follow_up,
        "complete_count": complete,
        "reviewed_count": follow_up + complete,
        "open_count": unreviewed + follow_up,
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _decode(data: bytes, label: str) -> str:
    _require(not data.startswith(b"\xef\xbb\xbf"), f"{label} has a UTF-8 BOM")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(f"{label} is not UTF-8") from exc


def _csv_rows(data: bytes, label: str):
    try:
        yield from csv.reader(StringIO(_decode(data, label), newline=""), strict=True)
    except csv.Error as exc:
        raise AssertionError(f"{label} is not valid CSV") from exc


def verify_artifacts(
    session_json: bytes,
    original_csv: bytes,
    reviewed_csv: bytes,
    expected: tuple[ExpectedReview, ...],
    fingerprint: str,
) -> dict[str, int]:
    """Verify every artifact against independent fixture truth and return sizes.

    JSON is parsed directly and compared field by field before an independent
    standard-library serialization checks its exact compact representation.
    CSV rows are streamed in parallel, checking original financial evidence as
    well as cell-for-cell preservation and the exact labels/prefixed notes.
    Embedded note newlines, delimiters, quotes, and backslashes must remain one
    logical CSV cell. No physical-line splitting is used.
    """

    groups = len(expected)
    _require(expected == expected_reviews(groups), "expected annotations differ from fixture rules")
    _require(
        session_json.endswith(b"\n") and not session_json.endswith(b"\n\n"),
        "session JSON must have exactly one terminal LF",
    )
    text = _decode(session_json, "session JSON")
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise AssertionError("session JSON is not a valid document") from exc
    _require(isinstance(document, dict), "session JSON root is not an object")
    expected_document = {
        "format": SESSION_FORMAT,
        "version": SESSION_VERSION,
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "result_fingerprint": fingerprint,
        "reviews": [
            {
                "matching_key": list(review.matching_key),
                "disposition": review.disposition,
                "note": review.note,
            }
            for review in expected
        ],
    }
    _require(tuple(document) == tuple(expected_document), "session JSON schema/order mismatch")
    _require(document["format"] == SESSION_FORMAT, "session JSON format mismatch")
    _require(
        type(document["version"]) is int and document["version"] == SESSION_VERSION,
        "session JSON version mismatch",
    )
    _require(
        document["fingerprint_algorithm"] == FINGERPRINT_ALGORITHM,
        "session JSON fingerprint algorithm mismatch",
    )
    _require(document["result_fingerprint"] == fingerprint, "session JSON fingerprint mismatch")
    reviews = document["reviews"]
    _require(isinstance(reviews, list), "session JSON reviews are not an array")
    _require(len(reviews) == groups, "session JSON review population mismatch")
    for index, (actual, review) in enumerate(zip(reviews, expected, strict=True)):
        _require(
            isinstance(actual, dict) and tuple(actual) == ("matching_key", "disposition", "note"),
            f"session JSON review {index} schema mismatch",
        )
        _require(
            actual["matching_key"] == list(review.matching_key),
            f"session JSON review {index} key/order mismatch",
        )
        _require(
            actual["disposition"] == review.disposition,
            f"session JSON review {index} disposition mismatch",
        )
        _require(actual["note"] == review.note, f"session JSON review {index} note mismatch")
    independent_bytes = (
        json.dumps(expected_document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    _require(session_json == independent_bytes, "session JSON compact representation mismatch")

    original_reader = _csv_rows(original_csv, "original CSV")
    reviewed_reader = _csv_rows(reviewed_csv, "reviewed CSV")
    original_header = next(original_reader, None)
    reviewed_header = next(reviewed_reader, None)
    _require(original_header == list(ORIGINAL_COLUMNS), "original CSV header mismatch")
    _require(
        reviewed_header == [*ORIGINAL_COLUMNS, "Review disposition", "Review note"],
        "reviewed CSV header mismatch",
    )
    sentinel = object()
    rows = 0
    for index, (original, reviewed, review) in enumerate(
        zip_longest(original_reader, reviewed_reader, expected, fillvalue=sentinel)
    ):
        _require(
            original is not sentinel and reviewed is not sentinel and review is not sentinel,
            "CSV review population mismatch",
        )
        original_cells = [
            "Amount mismatch",
            _key(index)[0],
            _amount(10000 + index % 97),
            _amount(9999 + index % 97),
            "+0.01",
            str(groups - index + 1),
            str(index // 2 + 2 + ((groups + 1) // 2 if index % 2 else 0)),
        ]
        _require(
            original == original_cells, f"original CSV row {index} financial evidence mismatch"
        )
        _require(len(reviewed) == 9, f"reviewed CSV row {index} cell count mismatch")
        _require(
            reviewed[:-2] == original,
            f"reviewed CSV row {index} altered original evidence cells",
        )
        _require(
            reviewed[-2] == DISPOSITION_LABELS[index % 3],
            f"reviewed CSV row {index} disposition label mismatch",
        )
        note_cell = "Note: " + review.note if review.note else ""
        _require(reviewed[-1] == note_cell, f"reviewed CSV row {index} note cell mismatch")
        rows += 1
    _require(rows == groups, "CSV row count mismatch")
    return {
        **expected_counts(groups),
        "json_bytes": len(session_json),
        "original_csv_bytes": len(original_csv),
        "reviewed_csv_bytes": len(reviewed_csv),
        "original_rows": rows,
        "reviewed_rows": rows,
    }
