"""Stage 1 annotation values, with no result binding or persistence behavior."""

import ast
import sys
from dataclasses import FrozenInstanceError, fields, replace
from enum import StrEnum
from pathlib import Path

import pytest

import tallydiff.review as review_module
from tallydiff.review import FindingReview, ReviewDisposition, ReviewSessionError


def test_dispositions_have_exact_members_values_and_no_aliases():
    expected = {
        "UNREVIEWED": "unreviewed",
        "NEEDS_FOLLOW_UP": "needs_follow_up",
        "REVIEW_COMPLETE": "review_complete",
    }
    assert issubclass(ReviewDisposition, StrEnum)
    assert {
        name: member.value for name, member in ReviewDisposition.__members__.items()
    } == expected
    assert len(ReviewDisposition.__members__) == len(ReviewDisposition) == 3
    for name, value in expected.items():
        member = ReviewDisposition[name]
        assert str(member) == value
        assert ReviewDisposition(value) is member


@pytest.mark.parametrize("value", ["accepted", "explained", "complete", "UNREVIEWED", ""])
def test_unknown_disposition_values_are_rejected(value):
    with pytest.raises(ValueError):
        ReviewDisposition(value)


@pytest.mark.parametrize(
    "disposition,reviewed,open_item",
    [
        (ReviewDisposition.UNREVIEWED, False, True),
        (ReviewDisposition.NEEDS_FOLLOW_UP, True, True),
        (ReviewDisposition.REVIEW_COMPLETE, True, False),
    ],
)
def test_disposition_workflow_properties(disposition, reviewed, open_item):
    assert disposition.is_reviewed is reviewed
    assert disposition.is_open is open_item


def test_workflow_properties_support_locked_aggregate_identities():
    dispositions = [ReviewDisposition.UNREVIEWED] * 2
    dispositions += [ReviewDisposition.NEEDS_FOLLOW_UP] * 3
    dispositions += [ReviewDisposition.REVIEW_COMPLETE] * 4
    total = len(dispositions)
    unreviewed = dispositions.count(ReviewDisposition.UNREVIEWED)
    follow_up = dispositions.count(ReviewDisposition.NEEDS_FOLLOW_UP)
    complete = dispositions.count(ReviewDisposition.REVIEW_COMPLETE)
    assert total == unreviewed + follow_up + complete
    assert (
        sum(item.is_reviewed for item in dispositions) == follow_up + complete == total - unreviewed
    )
    assert sum(item.is_open for item in dispositions) == unreviewed + follow_up == total - complete


def test_finding_review_has_only_the_approved_fields_and_default_note():
    review = FindingReview(("INV",), ReviewDisposition.UNREVIEWED)
    assert [item.name for item in fields(FindingReview)] == ["matching_key", "disposition", "note"]
    assert review.matching_key == ("INV",)
    assert review.disposition is ReviewDisposition.UNREVIEWED
    assert review.note == ""


@pytest.mark.parametrize("disposition", list(ReviewDisposition))
@pytest.mark.parametrize("note", ["", "Still collecting evidence."])
def test_note_is_optional_and_independent_of_disposition(disposition, note):
    review = FindingReview(("INV", "001"), disposition, note)
    assert review.disposition is disposition
    assert review.note == note


@pytest.mark.parametrize(
    "field,value",
    [
        ("matching_key", ("OTHER",)),
        ("disposition", ReviewDisposition.REVIEW_COMPLETE),
        ("note", "changed"),
    ],
)
def test_finding_review_fields_are_frozen(field, value):
    review = FindingReview(("INV",), ReviewDisposition.UNREVIEWED)
    with pytest.raises(FrozenInstanceError):
        setattr(review, field, value)
    with pytest.raises(FrozenInstanceError):
        delattr(review, field)


def test_finding_review_uses_slots_without_an_instance_dictionary():
    review = FindingReview(("INV",), ReviewDisposition.UNREVIEWED)
    assert not hasattr(review, "__dict__")
    with pytest.raises(AttributeError):
        object.__setattr__(review, "unexpected", "value")


def test_equality_and_hash_use_all_immutable_annotation_values():
    review = FindingReview(("INV", "001"), ReviewDisposition.UNREVIEWED, " evidence ")
    equal = FindingReview(("INV", "001"), ReviewDisposition.UNREVIEWED, " evidence ")
    assert review == equal
    assert hash(review) == hash(equal) == hash(review)
    assert len({review, equal}) == 1
    assert review != replace(review, matching_key=("INV", "002"))
    assert review != replace(review, disposition=ReviewDisposition.REVIEW_COMPLETE)
    assert review != replace(review, note="evidence")


@pytest.mark.parametrize(
    "key",
    [
        ("INV",),
        ("Vendor", "00042"),
        ("  MiXeD.Key  ", "\t00042\r\n"),
        ("caf\u00e9", "e\u0301", "\U0001f600"),
        ("A / B", "C"),
        ("A", "B / C"),
        ("=1+2",),
        ("\x00", "\x01\x7f\x80\x9f"),
    ],
)
def test_matching_keys_are_retained_exactly_without_note_restrictions(key):
    review = FindingReview(key, ReviewDisposition.UNREVIEWED)
    assert review.matching_key is key
    assert review.matching_key == key


def test_display_label_collisions_do_not_conflate_annotations():
    left = FindingReview(("A / B", "C"), ReviewDisposition.UNREVIEWED)
    right = FindingReview(("A", "B / C"), ReviewDisposition.UNREVIEWED)
    assert " / ".join(left.matching_key) == " / ".join(right.matching_key)
    assert left != right
    assert len({left, right}) == 2


@pytest.mark.parametrize(
    "key", [None, "INV", ["INV"], {"INV"}, {"id": "INV"}, (1,), (None,), (True,), ("INV", [])]
)
def test_matching_key_requires_a_tuple_of_strings(key):
    with pytest.raises(TypeError, match="matching_key.*tuple of strings"):
        FindingReview(key, ReviewDisposition.UNREVIEWED)


@pytest.mark.parametrize("key", [(), ("",), ("INV", ""), (" \t\r\n",), ("\u2003",)])
def test_matching_key_rejects_empty_or_blank_components(key):
    with pytest.raises(ValueError, match="matching_key.*component"):
        FindingReview(key, ReviewDisposition.UNREVIEWED)


@pytest.mark.parametrize("disposition", [None, "unreviewed", "review_complete", 0, True, []])
def test_finding_review_requires_an_actual_disposition_member(disposition):
    with pytest.raises(TypeError, match="disposition.*ReviewDisposition"):
        FindingReview(("INV",), disposition)


@pytest.mark.parametrize(
    "note",
    [
        "",
        " leading and trailing spaces ",
        "   ",
        "\t",
        "\r",
        "\n",
        "first\r\nsecond\rthird\nfourth",
        "\tMultiline caf\u00e9\ncombining e\u0301 and emoji \U0001f600\r\n ",
        '"quotes" and \\backslashes\\',
        "**plain text**, not Markdown semantics",
        "=1+2",
        "+SUM(A1:A2)",
        "-1+2",
        "@SUM(A1:A2)",
        "\t =1+2\r\n",
    ],
)
def test_valid_notes_are_preserved_exactly(note):
    review = FindingReview(("INV",), ReviewDisposition.UNREVIEWED, note)
    assert review.note is note
    assert review.note == note
    assert review.note.encode("utf-8") == note.encode("utf-8")


@pytest.mark.parametrize("note", [None, 0, True, b"text", [], {"text": "note"}])
def test_note_requires_a_string(note):
    with pytest.raises(TypeError, match="note.*str") as error:
        FindingReview(("INV",), ReviewDisposition.UNREVIEWED, note)
    assert type(error.value) is TypeError


@pytest.mark.parametrize(
    "codepoint",
    [value for value in range(0x20) if value not in (0x09, 0x0A, 0x0D)] + list(range(0x7F, 0xA0)),
)
def test_every_prohibited_note_control_is_rejected(codepoint):
    with pytest.raises(ValueError, match=f"control character U\\+{codepoint:04X}") as error:
        FindingReview(("INV",), ReviewDisposition.UNREVIEWED, f"before{chr(codepoint)}after")
    assert type(error.value) is ValueError


@pytest.mark.parametrize("note", ["\ud800", "\udfff", "before\ud800after", "\ud800\udc00"])
def test_surrogate_notes_raise_clean_domain_errors(note):
    with pytest.raises(ValueError, match="UTF-8 encodable") as error:
        FindingReview(("INV",), ReviewDisposition.UNREVIEWED, note)
    assert type(error.value) is ValueError
    assert not isinstance(error.value, (UnicodeEncodeError, ReviewSessionError))
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "note",
    ["a" * 16_384, "\u00e9" * 8_192, "\U0001f600" * 4_096],
    ids=["ascii", "two-byte-unicode", "four-byte-emoji"],
)
def test_exact_utf8_byte_limit_is_accepted_without_rewriting(note):
    assert len(note.encode("utf-8")) == 16_384
    review = FindingReview(("INV",), ReviewDisposition.UNREVIEWED, note)
    assert review.note is note


@pytest.mark.parametrize(
    "note",
    ["a" * 16_385, "\u00e9" * 8_192 + "a", "\U0001f600" * 4_096 + "a", "\u00e9" * 8_193],
    ids=["ascii-16385", "unicode-16385", "emoji-16385", "unicode-16386"],
)
def test_excess_utf8_bytes_are_rejected_without_truncation(note):
    original = note
    assert len(note.encode("utf-8")) > 16_384
    with pytest.raises(ValueError, match="16,384 UTF-8 bytes") as error:
        FindingReview(("INV",), ReviewDisposition.UNREVIEWED, note)
    assert type(error.value) is ValueError
    assert note is original
    assert note == original


def test_review_session_error_is_a_normal_value_error():
    assert issubclass(ReviewSessionError, ValueError)
    error = ReviewSessionError("invalid session", "detail")
    assert error.args == ("invalid session", "detail")
    assert str(error) == "('invalid session', 'detail')"


def test_domain_value_errors_are_not_reclassified_as_session_errors():
    with pytest.raises(ValueError) as error:
        FindingReview((), ReviewDisposition.UNREVIEWED)
    assert type(error.value) is ValueError
    assert not isinstance(error.value, ReviewSessionError)


def test_review_module_respects_dependency_boundaries():
    tree = ast.parse(Path(review_module.__file__).read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imports.add(node.module)
    allowed_domain = {
        "tallydiff.models",
        "tallydiff.configuration",
        "tallydiff.normalization_config",
        "tallydiff._decimal",
    }
    assert all(
        name.split(".", 1)[0] in sys.stdlib_module_names or name in allowed_domain
        for name in imports
    )
