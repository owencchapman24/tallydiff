"""Explicit key normalization and same-source collision preflight, independent of reconciliation.

Enabled rules always run in this order: Unicode casefold, whitespace collapse,
Unicode punctuation removal, then ASCII-only leading-zero stripping. The same
configuration applies to the corresponding mapped components in both sources.
No rule performs numeric parsing, transliteration, or Unicode canonicalization.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from unicodedata import category

from tallydiff.models import CompositeKey, Source, SourceRecord


class KeyNormalizationError(ValueError):
    """A key component would be blank after applying the explicit rules."""


@dataclass(frozen=True, slots=True)
class KeyNormalizationRules:
    """Opt-in transformations for one key component; defaults preserve exact text."""

    casefold: bool = False
    collapse_whitespace: bool = False
    remove_punctuation: bool = False
    strip_leading_zeros: bool = False

    def __post_init__(self) -> None:
        for name in (
            "casefold",
            "collapse_whitespace",
            "remove_punctuation",
            "strip_leading_zeros",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True)
class KeyNormalizationConfig:
    """One rule set per mapped key component, in key order, shared by A and B.

    The default is an exact single-component key. Composite keys must explicitly
    supply one rule set per component, including default rules for exact fields.
    """

    component_rules: tuple[KeyNormalizationRules, ...] = (KeyNormalizationRules(),)

    def __post_init__(self) -> None:
        if not isinstance(self.component_rules, tuple) or any(
            not isinstance(rules, KeyNormalizationRules) for rules in self.component_rules
        ):
            raise TypeError("component_rules must be a tuple of KeyNormalizationRules")
        if not self.component_rules:
            raise ValueError("component_rules must contain at least one rule set")


@dataclass(frozen=True, slots=True)
class OriginalKeyEvidence:
    """One distinct original key and all its unchanged records, ordered by source row."""

    original_key: CompositeKey
    records: tuple[SourceRecord, ...]


@dataclass(frozen=True, slots=True)
class NormalizationCollision:
    """Distinct original keys converging within one source; originals are sorted by key."""

    source: Source
    normalized_key: CompositeKey
    originals: tuple[OriginalKeyEvidence, ...]


def normalize_key(key: CompositeKey, configuration: KeyNormalizationConfig) -> CompositeKey:
    """Return a new key without modifying the input or reading source evidence.

    Configuration arity must match the nonempty tuple of strings exactly. Each
    component follows the module's fixed rule order. Whitespace collapse uses
    " ".join(value.split()); punctuation removal drops only Unicode categories
    starting with P. Zero stripping applies only to nonempty ASCII digits and
    keeps one zero for all-zero values. A blank result always raises visibly.

    Later rules see earlier results: punctuation removal can make a signed or
    decimal-looking original eligible for zero stripping. Whitespace exposed
    by punctuation removal is not collapsed again.
    """

    if not isinstance(configuration, KeyNormalizationConfig):
        raise TypeError("configuration must be a KeyNormalizationConfig")
    if not isinstance(key, tuple) or any(not isinstance(component, str) for component in key):
        raise TypeError("key must be a tuple of strings")
    if not key:
        raise ValueError("key must contain at least one component")
    if len(configuration.component_rules) != len(key):
        raise ValueError("configuration length must equal key length")

    normalized: list[str] = []
    for index, (value, rules) in enumerate(zip(key, configuration.component_rules, strict=True)):
        if rules.casefold:
            value = value.casefold()
        if rules.collapse_whitespace:
            value = " ".join(value.split())
        if rules.remove_punctuation:
            value = "".join(
                character for character in value if not category(character).startswith("P")
            )
        if rules.strip_leading_zeros and value and value.isascii() and value.isdecimal():
            value = value.lstrip("0") or "0"
        if not value.strip():
            raise KeyNormalizationError(
                f"key component {index + 1} normalizes to blank (original value: {key[index]!r})"
            )
        normalized.append(value)
    return tuple(normalized)


def find_normalization_collisions(
    records: Iterable[SourceRecord],
    configuration: KeyNormalizationConfig,
    *,
    source: Source,
) -> tuple[NormalizationCollision, ...]:
    """Preflight one source, retaining all original evidence without merging rows.

    A collision requires at least two distinct original *full* composite keys to
    share a normalized full key. Exact duplicates alone are not collisions, and
    sources must be checked separately with the same configuration. Records must
    be validated SourceRecord instances with unique row numbers for the declared
    source. One-pass iterables are consumed once. Errors abort the whole preflight.

    Results are immutable tuples sorted by normalized key, then original key,
    then source row. Records are retained by reference, including raw_fields and
    original keys. This helper never invokes or configures reconciliation.
    """

    if not isinstance(configuration, KeyNormalizationConfig):
        raise TypeError("configuration must be a KeyNormalizationConfig")
    if not isinstance(source, Source):
        raise TypeError("source must be a Source enum member")
    if not isinstance(records, Iterable):
        raise TypeError("records must be an iterable of SourceRecord objects")

    grouped: dict[CompositeKey, dict[CompositeKey, list[SourceRecord]]] = {}
    seen_rows: set[int] = set()
    for record in records:
        if not isinstance(record, SourceRecord):
            raise TypeError("records must contain only SourceRecord objects")
        if record.source is not source:
            raise ValueError(
                f"record at source row {record.source_row} belongs to File {record.source.value}, "
                f"not File {source.value}"
            )
        if record.source_row in seen_rows:
            raise ValueError(
                f"File {source.value} contains duplicate source row {record.source_row}"
            )
        seen_rows.add(record.source_row)
        try:
            normalized_key = normalize_key(record.key, configuration)
        except KeyNormalizationError as error:
            raise KeyNormalizationError(
                f"File {source.value} source row {record.source_row}: {error}"
            ) from error
        grouped.setdefault(normalized_key, {}).setdefault(record.key, []).append(record)

    return tuple(
        NormalizationCollision(
            source=source,
            normalized_key=normalized_key,
            originals=tuple(
                OriginalKeyEvidence(
                    original_key=original_key,
                    records=tuple(sorted(rows, key=lambda record: record.source_row)),
                )
                for original_key, rows in sorted(originals.items())
            ),
        )
        for normalized_key, originals in sorted(grouped.items())
        if len(originals) > 1
    )
