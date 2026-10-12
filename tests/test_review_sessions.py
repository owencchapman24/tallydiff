"""Canonical review JSON, bounded hostile-input parsing, and atomic replacement."""

import builtins
import json
from dataclasses import replace
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

import pytest

import tallydiff
import tallydiff.review as domain
from tallydiff import (
    ColumnMapping,
    FindingReview,
    ReviewDisposition,
    ReviewSession,
    ReviewSessionError,
    Source,
    SourceRecord,
    bind_review_result,
    export_exceptions_csv,
    export_review_session,
    ingest_csv,
    load_review_session,
    reconcile,
)

DATA_A = b"id,amount\nA,3\nB,5\nC,7\nclean,2\ntolerated,2\n"
DATA_B = b"id,amount\nA,1\nB,3\nC,4\nclean,2\ntolerated,1\n"
MAPPING = ColumnMapping((("id", "id"),), "amount", "amount")
FINGERPRINT = "d345c07226b3fd7d96cc0d75d59b4e19f4a87de5c50682a665c1cfce658f4cb8"
EXPECTED = (
    r'{"format":"tallydiff-review-session","version":1,'
    r'"fingerprint_algorithm":"sha256:tallydiff-review-binding-v1",'
    r'"result_fingerprint":"d345c07226b3fd7d96cc0d75d59b4e19f4a87de5c50682a665c1cfce658f4cb8",'
    r'"reviews":[{"matching_key":["A"],"disposition":"unreviewed","note":""},'
    r'{"matching_key":["B"],"disposition":"needs_follow_up","note":"'
    " Confirm \u00e9 e\u0301 \U0001f600"
    r'\t\r\n"},'
    r'{"matching_key":["C"],"disposition":"review_complete","note":"Quoted \"text\" \\ =1+2"}]}'
    "\n"
).encode("utf-8")


@pytest.fixture
def binding():
    records = tuple(
        ingest_csv(data.decode("utf-8"), source=source, key_columns=("id",), amount_column="amount")
        for source, data in ((Source.A, DATA_A), (Source.B, DATA_B))
    )
    result = reconcile(*records, amount_tolerance=Decimal("1"))
    return bind_review_result(
        result, data_a=DATA_A, data_b=DATA_B, mapping=MAPPING, format_a="csv", format_b="csv"
    )


@pytest.fixture
def session(binding):
    session = ReviewSession.unreviewed(binding)
    session = session.with_review(
        FindingReview(
            ("B",), ReviewDisposition.NEEDS_FOLLOW_UP, " Confirm \u00e9 e\u0301 \U0001f600\t\r\n"
        )
    )
    return session.with_review(
        FindingReview(("C",), ReviewDisposition.REVIEW_COMPLETE, 'Quoted "text" \\ =1+2')
    )


def _document(session):
    return json.loads(export_review_session(session))


def _encode(document, **kwargs):
    return json.dumps(document, ensure_ascii=False, **kwargs).encode("utf-8")


def _forbidden(*args, **kwargs):
    pytest.fail("operation must not be called before complete validation")


def test_exact_literal_canonical_export_bytes(session):
    assert session.binding.result_fingerprint == FINGERPRINT
    assert export_review_session(session) == EXPECTED
    assert export_review_session(session) == export_review_session(session)
    assert EXPECTED.endswith(b"}\n")
    assert not EXPECTED.endswith(b"\n\n")
    assert not EXPECTED.startswith(b"\xef\xbb\xbf")
    assert b"\\u00e9" not in EXPECTED
    assert "\u00e9 e\u0301 \U0001f600".encode("utf-8") in EXPECTED
    assert export_review_session(load_review_session(EXPECTED, binding=session.binding)) == EXPECTED


def test_exact_public_fields_order_and_full_population(session):
    document = _document(session)
    assert list(document) == [
        "format",
        "version",
        "fingerprint_algorithm",
        "result_fingerprint",
        "reviews",
    ]
    assert document["format"] == "tallydiff-review-session"
    assert type(document["version"]) is int and document["version"] == 1
    assert document["fingerprint_algorithm"] == "sha256:tallydiff-review-binding-v1"
    assert all(
        list(review) == ["matching_key", "disposition", "note"] for review in document["reviews"]
    )
    assert [tuple(review["matching_key"]) for review in document["reviews"]] == list(
        session.binding.exception_keys
    )
    assert document["reviews"][0] == {
        "matching_key": ["A"],
        "disposition": "unreviewed",
        "note": "",
    }


def test_default_export_includes_every_unreviewed_entry(binding):
    session = ReviewSession.unreviewed(binding)
    document = _document(session)
    assert len(document["reviews"]) == 3
    assert all(
        item["disposition"] == "unreviewed" and item["note"] == "" for item in document["reviews"]
    )
    assert load_review_session(export_review_session(session), binding=binding) == session


@pytest.mark.parametrize("empty", [True, False])
def test_zero_exception_session_round_trip(empty):
    rows = () if empty else (SourceRecord(Source.A, 2, ("K",), Decimal("1")),)
    other = () if empty else (SourceRecord(Source.B, 2, ("K",), Decimal("1")),)
    binding = bind_review_result(
        reconcile(rows, other),
        data_a=b"a",
        data_b=b"b",
        mapping=MAPPING,
        format_a="csv",
        format_b="csv",
    )
    session = ReviewSession.unreviewed(binding)
    assert export_review_session(session).endswith(b'"reviews":[]}\n')
    assert load_review_session(export_review_session(session), binding=binding) == session


@pytest.mark.parametrize("disposition", list(ReviewDisposition))
@pytest.mark.parametrize(
    "note",
    [
        "",
        " leading and trailing ",
        "   ",
        "\t",
        "\n",
        "\r",
        "first\r\nsecond\rthird\nfourth",
        "caf\u00e9 / cafe\u0301 / \U0001f600",
        '"quotes" \\backslashes\\',
        "=1+2",
        "+SUM(A1:A2)",
        "-1+2",
        "@SUM(A1:A2)",
    ],
)
def test_notes_and_dispositions_round_trip_exactly(binding, disposition, note):
    original = ReviewSession.unreviewed(binding).with_review(
        FindingReview(("A",), disposition, note)
    )
    loaded = load_review_session(export_review_session(original), binding=binding)
    assert loaded == original
    assert loaded.binding is binding
    assert loaded.reviews[0].note.encode("utf-8") == note.encode("utf-8")
    assert loaded.reviews[0].disposition is disposition


def test_display_collision_and_control_character_keys_round_trip_without_normalization():
    keys = (("A", "B / C"), ("A / B", "C"), ("K\x00\x7f", " \u00e9 e\u0301 "))
    rows = tuple(
        SourceRecord(Source.A, index, key, Decimal("1")) for index, key in enumerate(keys, 2)
    )
    mapping = ColumnMapping((("id", "id"), ("other", "other")), "amount", "amount")
    binding = bind_review_result(
        reconcile(rows, ()),
        data_a=b"a",
        data_b=b"b",
        mapping=mapping,
        format_a="csv",
        format_b="csv",
    )
    original = ReviewSession.unreviewed(binding).with_review(
        FindingReview(keys[1], ReviewDisposition.REVIEW_COMPLETE)
    )
    loaded = load_review_session(export_review_session(original), binding=binding)
    assert loaded == original
    assert tuple(review.matching_key for review in loaded.reviews) == keys
    assert loaded.reviews[0].disposition is ReviewDisposition.UNREVIEWED
    assert loaded.reviews[1].disposition is ReviewDisposition.REVIEW_COMPLETE


@pytest.mark.parametrize("pretty", [False, True])
def test_input_object_and_review_order_are_canonicalized(session, pretty):
    document = dict(reversed(tuple(_document(session).items())))
    document["reviews"] = [
        dict(reversed(tuple(review.items()))) for review in document["reviews"][::-1]
    ]
    incoming = b" \r\n\t " + _encode(document, indent=4 if pretty else None) + b" \r\n\t "
    loaded = load_review_session(incoming, binding=session.binding)
    assert loaded == session
    assert loaded.reviews[0].matching_key == ("A",)
    assert export_review_session(loaded) == EXPECTED


@pytest.mark.parametrize("as_text", [False, True])
@pytest.mark.parametrize("bom_count", [0, 1, 2])
def test_exact_bom_policy(session, as_text, bom_count):
    incoming = b"\xef\xbb\xbf" * bom_count + EXPECTED
    if as_text:
        incoming = incoming.decode("utf-8")
    if bom_count == 2:
        with pytest.raises(ReviewSessionError, match="valid JSON"):
            load_review_session(incoming, binding=session.binding)
    else:
        assert (
            export_review_session(load_review_session(incoming, binding=session.binding))
            == EXPECTED
        )


@pytest.mark.parametrize(
    "incoming",
    [b"\xff", b"\xc3", b"\xc3(", b"\xff\xfe{\x00}\x00", "\ud800", "\udfff", '"before\ud800after"'],
)
def test_invalid_encoding_is_rejected(session, incoming):
    with pytest.raises(ReviewSessionError, match="UTF-8"):
        load_review_session(incoming, binding=session.binding)


@pytest.mark.parametrize(
    "incoming",
    [
        b"",
        b"{",
        b'{"broken":',
        b'{"broken":"unterminated}',
        b'{"broken":"trailing\\',
        b"]",
        b"[}",
        b"null",
        b"[]",
        b"1",
        b'"text"',
        b"{} {}",
        b"{} trailing",
    ],
)
def test_malformed_json_and_nonobject_documents_are_rejected(session, incoming):
    with pytest.raises(ReviewSessionError):
        load_review_session(incoming, binding=session.binding)


@pytest.mark.parametrize("level", ["top", "review", "unknown_nested", "escaped_name"])
def test_duplicate_object_fields_are_rejected_at_every_level(session, level):
    text = EXPECTED.decode("utf-8")
    if level == "top":
        text = text.replace('"version":1', '"version":1,"version":1', 1)
    elif level == "review":
        text = text.replace('"note":""', '"note":"","note":""', 1)
    elif level == "escaped_name":
        text = text.replace('"note":""', '"note":"","\\u006eote":""', 1)
    else:
        text = text.replace('"reviews":[', '"extra":{"nested":{"x":1,"x":1}},"reviews":[', 1)
    with pytest.raises(ReviewSessionError, match="duplicate object field"):
        load_review_session(text, binding=session.binding)


@pytest.mark.parametrize(
    "field", ["format", "version", "fingerprint_algorithm", "result_fingerprint", "reviews"]
)
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_top_level_fields_are_exact(session, field, change):
    document = _document(session)
    if change == "missing":
        del document[field]
    else:
        document[f"extra_{field}"] = None
    with pytest.raises(ReviewSessionError, match="exactly these fields"):
        load_review_session(_encode(document), binding=session.binding)


@pytest.mark.parametrize("field", ["matching_key", "disposition", "note"])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_review_fields_are_exact(session, field, change):
    document = _document(session)
    if change == "missing":
        del document["reviews"][0][field]
    else:
        document["reviews"][0][f"extra_{field}"] = None
    with pytest.raises(ReviewSessionError, match="review 1.*exactly these fields"):
        load_review_session(_encode(document), binding=session.binding)


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "tallydiff-mapping-profile"),
        ("format", None),
        ("version", True),
        ("version", 1.0),
        ("version", "1"),
        ("version", 0),
        ("version", 2),
        ("version", None),
        ("fingerprint_algorithm", "sha256"),
        ("fingerprint_algorithm", None),
        ("result_fingerprint", None),
        ("result_fingerprint", 1),
        ("result_fingerprint", "0" * 63),
        ("result_fingerprint", "0" * 65),
        ("result_fingerprint", FINGERPRINT.upper()),
        ("result_fingerprint", "g" * 64),
        ("result_fingerprint", "0" * 64),
        ("reviews", None),
        ("reviews", {}),
        ("reviews", "array"),
    ],
)
def test_schema_values_types_algorithms_and_fingerprints_are_strict(session, field, value):
    document = _document(session)
    document[field] = value
    with pytest.raises(ReviewSessionError):
        load_review_session(_encode(document), binding=session.binding)


@pytest.mark.parametrize(
    "token", ["NaN", "Infinity", "-Infinity", "1.0", "1e0", "-1.25", "1" * 33, "1" * 1000]
)
@pytest.mark.parametrize("position", ["version", "note", "extra"])
def test_hostile_numeric_tokens_are_rejected_anywhere(session, token, position):
    text = EXPECTED.decode("utf-8")
    if position == "version":
        text = text.replace('"version":1', f'"version":{token}', 1)
    elif position == "note":
        text = text.replace('"note":""', f'"note":{token}', 1)
    else:
        text = text.replace('"reviews":[', f'"extra":{token},"reviews":[', 1)
    with pytest.raises(ReviewSessionError, match="numbers|integer token"):
        load_review_session(text, binding=session.binding)


@pytest.mark.parametrize("token", ["1" * 32, "-" + "1" * 31])
def test_32_character_integer_tokens_are_bounded_but_still_schema_invalid(session, token):
    text = EXPECTED.decode("utf-8").replace('"version":1', f'"version":{token}', 1)
    with pytest.raises(ReviewSessionError, match="version"):
        load_review_session(text, binding=session.binding)


def test_ninth_container_level_is_rejected_before_parsing(session, monkeypatch):
    assert domain._MAX_JSON_DEPTH == 8
    monkeypatch.setattr(domain.json, "loads", _forbidden)
    with pytest.raises(ReviewSessionError, match="nesting-depth"):
        load_review_session("[" * 9 + "0" + "]" * 9, binding=session.binding)


def test_eighth_container_level_reaches_authoritative_parser(session, monkeypatch):
    calls = []
    original = json.loads

    def parser(text, **kwargs):
        calls.append(text)
        return original(text, **kwargs)

    monkeypatch.setattr(domain.json, "loads", parser)
    incoming = "[" * 8 + "0" + "]" * 8
    with pytest.raises(ReviewSessionError, match="must be an object"):
        load_review_session(incoming, binding=session.binding)
    assert calls == [incoming]


@pytest.mark.parametrize(
    "note",
    [
        "{" * 100 + "[" * 100 + "]" * 100 + "}" * 100,
        'before \\" { [ \\ after " ] }',
        '\\"[{{{{{{{{{}}}}}}}}}]"\\',
        "\t\r\n{[unicode \U0001f600]}",
    ],
)
def test_scanner_ignores_brackets_escaped_quotes_and_backslashes_in_strings(binding, note):
    session = ReviewSession.unreviewed(binding).with_review(
        FindingReview(("A",), ReviewDisposition.UNREVIEWED, note)
    )
    assert load_review_session(export_review_session(session), binding=binding) == session


def test_scanner_ignores_brackets_in_matching_key_strings():
    key = ("[" * 20 + '\\"{' + "]" * 20,)
    result = reconcile((SourceRecord(Source.A, 2, key, Decimal("1")),), ())
    binding = bind_review_result(
        result, data_a=b"a", data_b=b"b", mapping=MAPPING, format_a="csv", format_b="csv"
    )
    session = ReviewSession.unreviewed(binding)
    assert load_review_session(export_review_session(session), binding=binding) == session


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "unknown",
        "clean",
        "tolerated",
        "duplicate_identical",
        "duplicate_different",
        "empty",
    ],
)
def test_review_population_must_be_complete_and_unique(session, change):
    document = _document(session)
    if change == "missing":
        document["reviews"].pop()
    elif change in ("unknown", "clean", "tolerated"):
        document["reviews"][0]["matching_key"] = [change]
    elif change == "duplicate_identical":
        document["reviews"].append(dict(document["reviews"][0]))
    elif change == "duplicate_different":
        duplicate = dict(document["reviews"][0], note="different")
        document["reviews"].append(duplicate)
    else:
        document["reviews"] = []
    with pytest.raises(ReviewSessionError, match="exception keys|duplicates"):
        load_review_session(_encode(document), binding=session.binding)


@pytest.mark.parametrize(
    "field,value",
    [
        ("matching_key", "A"),
        ("matching_key", None),
        ("matching_key", []),
        ("matching_key", [1]),
        ("matching_key", [True]),
        ("matching_key", [None]),
        ("matching_key", [[]]),
        ("matching_key", [""]),
        ("matching_key", [" \t\r\n"]),
        ("matching_key", ["\u2003"]),
        ("matching_key", [" A "]),
        ("matching_key", ["a"]),
        ("matching_key", ["A", "extra"]),
        ("disposition", None),
        ("disposition", True),
        ("disposition", "UNREVIEWED"),
        ("disposition", "Unreviewed"),
        ("disposition", "accepted"),
        ("disposition", " unreviewed "),
        ("note", None),
        ("note", 1),
        ("note", True),
        ("note", []),
    ],
)
def test_review_shapes_values_and_key_identity_are_strict(session, field, value):
    document = _document(session)
    document["reviews"][0][field] = value
    with pytest.raises(ReviewSessionError):
        load_review_session(_encode(document), binding=session.binding)


@pytest.mark.parametrize("item", [None, [], "review", True, 1])
def test_every_review_item_must_be_an_object(session, item):
    document = _document(session)
    document["reviews"][0] = item
    with pytest.raises(ReviewSessionError, match="review 1.*object"):
        load_review_session(_encode(document), binding=session.binding)


@pytest.mark.parametrize(
    "codepoint",
    [value for value in range(0x20) if value not in (9, 10, 13)] + list(range(0x7F, 0xA0)),
)
def test_file_notes_enforce_every_prohibited_control(session, codepoint):
    document = _document(session)
    document["reviews"][-1]["note"] = "confidential note" + chr(codepoint)
    with pytest.raises(ReviewSessionError, match="review 3 note") as error:
        load_review_session(_encode(document), binding=session.binding)
    assert "confidential" not in str(error.value)


@pytest.mark.parametrize("field", ["matching_key", "note"])
@pytest.mark.parametrize("surrogate", ["\\ud800", "\\udfff"])
def test_json_escaped_surrogates_are_rejected_with_index_context(session, field, surrogate):
    text = EXPECTED.decode("utf-8")
    if field == "note":
        text = text.replace('"note":""', f'"note":"{surrogate}"', 1)
    else:
        text = text.replace('"matching_key":["A"]', f'"matching_key":["{surrogate}"]', 1)
    with pytest.raises(ReviewSessionError, match="review 1"):
        load_review_session(text, binding=session.binding)


def test_json_escaped_valid_surrogate_pair_round_trips_as_emoji(session):
    text = EXPECTED.decode("utf-8").replace('"note":""', '"note":"\\ud83d\\ude00"', 1)
    loaded = load_review_session(text, binding=session.binding)
    assert loaded.reviews[0].note == "\U0001f600"
    assert "\U0001f600".encode("utf-8") in export_review_session(loaded)


@pytest.mark.parametrize(
    "note,valid",
    [("a" * 16_384, True), ("\u00e9" * 8_192, True), ("\u00e9" * 8_192 + "a", False)],
    ids=["ascii-limit", "unicode-limit", "unicode-one-byte-over"],
)
def test_file_note_byte_boundaries(session, note, valid):
    document = _document(session)
    document["reviews"][0]["note"] = note
    incoming = _encode(document)
    if valid:
        assert load_review_session(incoming, binding=session.binding).reviews[0].note == note
    else:
        with pytest.raises(ReviewSessionError, match="review 1 note"):
            load_review_session(incoming, binding=session.binding)


def test_real_size_and_integer_limits_are_fixed():
    assert domain._MAX_SESSION_BYTES == 64 * 1024 * 1024
    assert domain._MAX_INTEGER_CHARS == 32


@pytest.mark.parametrize("as_text", [False, True])
def test_input_byte_boundary_and_one_extra_byte(session, monkeypatch, as_text):
    incoming = EXPECTED.decode("utf-8") if as_text else EXPECTED
    monkeypatch.setattr(domain, "_MAX_SESSION_BYTES", len(EXPECTED))
    assert load_review_session(incoming, binding=session.binding) == session
    oversized = incoming + (" " if as_text else b" ")
    monkeypatch.setattr(domain.json, "loads", _forbidden)
    with pytest.raises(ReviewSessionError, match="input exceeds"):
        load_review_session(oversized, binding=session.binding)


def test_multibyte_text_limit_uses_encoded_bytes(session, monkeypatch):
    incoming = EXPECTED.decode("utf-8")
    assert len(incoming) < len(EXPECTED)
    monkeypatch.setattr(domain, "_MAX_SESSION_BYTES", len(incoming))
    monkeypatch.setattr(domain.json, "loads", _forbidden)
    with pytest.raises(ReviewSessionError, match="input exceeds"):
        load_review_session(incoming, binding=session.binding)


def test_bytes_size_is_checked_before_decoding_or_parsing(session, monkeypatch):
    monkeypatch.setattr(domain, "_MAX_SESSION_BYTES", 1)
    monkeypatch.setattr(domain.json, "loads", _forbidden)
    with pytest.raises(ReviewSessionError, match="input exceeds"):
        load_review_session(b"\xff\xff", binding=session.binding)


def test_initial_bom_contributes_to_raw_byte_size(session, monkeypatch):
    monkeypatch.setattr(domain, "_MAX_SESSION_BYTES", len(EXPECTED) + 3)
    assert load_review_session(b"\xef\xbb\xbf" + EXPECTED, binding=session.binding) == session
    monkeypatch.setattr(domain, "_MAX_SESSION_BYTES", len(EXPECTED) + 2)
    with pytest.raises(ReviewSessionError, match="input exceeds"):
        load_review_session(b"\xef\xbb\xbf" + EXPECTED, binding=session.binding)


def test_export_byte_boundary_includes_final_lf_without_truncation(session, monkeypatch):
    monkeypatch.setattr(domain, "_MAX_SESSION_BYTES", len(EXPECTED))
    assert export_review_session(session) == EXPECTED
    monkeypatch.setattr(domain, "_MAX_SESSION_BYTES", len(EXPECTED) - 1)
    with pytest.raises(ReviewSessionError, match="export exceeds"):
        export_review_session(session)
    assert session.reviews[1].note == " Confirm \u00e9 e\u0301 \U0001f600\t\r\n"


def test_late_invalid_review_rejects_atomically_without_constructing_a_session(
    session, monkeypatch
):
    binding = session.binding
    result = binding.result
    original_reviews = session.reviews
    original_findings = result.findings
    original_keys = binding.exception_keys
    raw = tuple(
        (row, row.raw_fields, tuple(row.raw_fields.items()))
        for finding in result.findings
        for row in (*finding.rows_a, *finding.rows_b)
    )
    original_csv = export_exceptions_csv(result)
    original_result_payload = domain._result_payload(result, 1)
    incoming = _document(session)
    incoming["reviews"][-1]["note"] = "late confidential note\x00"
    encoded = _encode(incoming)
    with monkeypatch.context() as patch:
        patch.setattr(domain, "ReviewSession", _forbidden)
        with pytest.raises(ReviewSessionError, match="review 3 note") as error:
            load_review_session(encoded, binding=binding)
    assert "confidential" not in str(error.value)
    assert session.reviews is original_reviews
    assert session.binding is binding and binding.result is result
    assert result.findings is original_findings and binding.exception_keys is original_keys
    assert all(
        row.raw_fields is mapping and tuple(row.raw_fields.items()) == evidence
        for row, mapping, evidence in raw
    )
    assert domain._result_payload(result, 1) == original_result_payload
    assert export_exceptions_csv(result) == original_csv
    assert export_review_session(session) == EXPECTED


def test_valid_load_is_a_complete_replacement_and_does_not_mutate_existing_session(session):
    replacement = ReviewSession.unreviewed(session.binding)
    loaded = load_review_session(export_review_session(replacement), binding=session.binding)
    assert loaded == replacement and loaded is not replacement and loaded is not session
    assert session.reviews[1].disposition is ReviewDisposition.NEEDS_FOLLOW_UP
    assert loaded.reviews[1].disposition is ReviewDisposition.UNREVIEWED
    assert loaded.reviews[1].note == ""


@pytest.mark.parametrize("change", ["bytes", "config", "result"])
def test_same_json_rejects_different_bound_run(session, change):
    binding = session.binding
    result = binding.result
    data_a = DATA_A
    if change == "bytes":
        data_a = b"\xef\xbb\xbf" + data_a
    elif change == "config":
        result = replace(result, amount_tolerance=Decimal("0.5"))
    else:
        finding = result.findings[0]
        row = replace(finding.rows_a[0], raw_fields={"retained": "changed evidence"})
        result = replace(result, findings=(replace(finding, rows_a=(row,)),) + result.findings[1:])
    changed = bind_review_result(
        result, data_a=data_a, data_b=DATA_B, mapping=MAPPING, format_a="csv", format_b="csv"
    )
    with pytest.raises(ReviewSessionError, match="result_fingerprint.*binding"):
        load_review_session(EXPECTED, binding=changed)


def test_identical_binding_can_restore_the_same_file_after_a_conceptual_rename(session):
    recreated = bind_review_result(
        session.binding.result,
        data_a=DATA_A,
        data_b=DATA_B,
        mapping=MAPPING,
        format_a="csv",
        format_b="csv",
    )
    assert recreated is not session.binding
    assert load_review_session(EXPECTED, binding=recreated).binding is recreated


@pytest.mark.parametrize(
    "incoming", [None, 1, bytearray(b"{}"), memoryview(b"{}"), Path("session.json")]
)
def test_invalid_input_python_types_remain_type_errors(binding, incoming):
    with pytest.raises(TypeError, match="bytes or str"):
        load_review_session(incoming, binding=binding)


def test_programmer_session_and_binding_misuse_remain_type_errors():
    with pytest.raises(TypeError, match="ReviewSession"):
        export_review_session("not a session")
    with pytest.raises(TypeError, match="ReviewBinding"):
        load_review_session(b"{}", binding="not a binding")


@pytest.mark.parametrize("operation", ["export", "load"])
def test_unrelated_internal_exceptions_are_not_reclassified(session, monkeypatch, operation):
    def broken(*args, **kwargs):
        raise RuntimeError("internal defect")

    monkeypatch.setattr(domain.json, "dumps" if operation == "export" else "loads", broken)
    with pytest.raises(RuntimeError, match="internal defect"):
        if operation == "export":
            export_review_session(session)
        else:
            load_review_session(EXPECTED, binding=session.binding)


def test_expected_json_recursion_error_is_wrapped(session, monkeypatch):
    def recursive(*args, **kwargs):
        raise RecursionError("decoder limit")

    monkeypatch.setattr(domain.json, "loads", recursive)
    with pytest.raises(ReviewSessionError, match="valid JSON"):
        load_review_session(EXPECTED, binding=session.binding)


def test_session_persistence_does_not_access_filesystem(session, monkeypatch):
    monkeypatch.setattr(builtins, "open", _forbidden)
    monkeypatch.setattr(Path, "read_bytes", _forbidden)
    monkeypatch.setattr(Path, "write_bytes", _forbidden)
    assert load_review_session(export_review_session(session), binding=session.binding) == session


def test_public_exports_and_package_version_remain_compatible():
    for name in (
        "ReviewDisposition",
        "FindingReview",
        "ReviewSessionError",
        "ReviewBinding",
        "ReviewSession",
        "bind_review_result",
        "export_review_session",
        "load_review_session",
    ):
        assert name in tallydiff.__all__
        assert getattr(tallydiff, name) is getattr(domain, name)
    assert export_review_session is domain.export_review_session
    assert load_review_session is domain.load_review_session
    assert version("tallydiff") == "0.6.0"
