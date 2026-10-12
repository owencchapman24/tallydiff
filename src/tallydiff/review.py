"""Analyst annotations kept separate from reconciliation truth."""

from dataclasses import dataclass
from enum import StrEnum

_MAX_NOTE_BYTES = 16_384


class ReviewDisposition(StrEnum):
    """Analyst workflow state, without asserting accounting correctness."""

    UNREVIEWED = "unreviewed"
    NEEDS_FOLLOW_UP = "needs_follow_up"
    REVIEW_COMPLETE = "review_complete"

    @property
    def is_reviewed(self) -> bool:
        """Whether the analyst has committed a workflow decision."""

        return self is not ReviewDisposition.UNREVIEWED

    @property
    def is_open(self) -> bool:
        """Whether initial review or follow-up work remains."""

        return self is not ReviewDisposition.REVIEW_COMPLETE


class ReviewSessionError(ValueError):
    """An expected review binding or persisted-session validation failure."""


def _validate_note(note: str) -> None:
    if not isinstance(note, str):
        raise TypeError("note must be a str")
    try:
        size = len(note.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("note must be UTF-8 encodable, without Unicode surrogates") from None
    if size > _MAX_NOTE_BYTES:
        raise ValueError(f"note must not exceed {_MAX_NOTE_BYTES:,} UTF-8 bytes")
    for character in note:
        codepoint = ord(character)
        if (codepoint < 0x20 and character not in "\t\n\r") or 0x7F <= codepoint <= 0x9F:
            raise ValueError(f"note must not contain control character U+{codepoint:04X}")


@dataclass(frozen=True, slots=True)
class FindingReview:
    """One immutable annotation; keys and valid plain-text notes are retained exactly."""

    matching_key: tuple[str, ...]
    disposition: ReviewDisposition
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.matching_key, tuple) or any(
            not isinstance(component, str) for component in self.matching_key
        ):
            raise TypeError("matching_key must be a tuple of strings")
        if not self.matching_key:
            raise ValueError("matching_key must contain at least one component")
        if any(not component.strip() for component in self.matching_key):
            raise ValueError("matching_key components must not be blank")
        if not isinstance(self.disposition, ReviewDisposition):
            raise TypeError("disposition must be a ReviewDisposition enum member")
        _validate_note(self.note)
