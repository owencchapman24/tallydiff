"""Immutable key-normalization configuration; depends only on the standard library."""

from dataclasses import dataclass


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
