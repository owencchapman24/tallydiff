"""Portable mapping profiles containing configuration only, with no file I/O."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from tallydiff.amounts import AmountParseError, parse_amount
from tallydiff.configuration import ColumnMapping
from tallydiff.models import ReconciliationMode

_FORMAT = "tallydiff-mapping-profile"
_VERSION = 2


class ProfileError(ValueError):
    """A profile is invalid or incompatible with the current input columns."""


def _column_name(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{label} must be a nonblank column name.")


@dataclass(frozen=True, slots=True)
class MappingProfile:
    """Validated directional configuration, independent of any uploaded records."""

    mapping: ColumnMapping
    amount_tolerance: Decimal = Decimal("0")
    reconciliation_mode: ReconciliationMode = ReconciliationMode.UNIQUE

    def __post_init__(self) -> None:
        if not isinstance(self.mapping, ColumnMapping):
            raise ProfileError("mapping must be a ColumnMapping.")
        pairs = self.mapping.key_pairs
        if not isinstance(pairs, tuple) or not pairs:
            raise ProfileError("key_pairs must contain at least one key mapping.")
        for index, pair in enumerate(pairs, start=1):
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ProfileError(f"Key mapping {index} must contain File A and File B columns.")
            _column_name(pair[0], f"Key mapping {index} File A")
            _column_name(pair[1], f"Key mapping {index} File B")
        for side, names in (("A", self.mapping.keys_a), ("B", self.mapping.keys_b)):
            if len(set(names)) != len(names):
                raise ProfileError(f"Each File {side} key column must be selected only once.")
        _column_name(self.mapping.amount_a, "File A amount column")
        _column_name(self.mapping.amount_b, "File B amount column")
        if not isinstance(self.amount_tolerance, Decimal):
            raise ProfileError("amount_tolerance must be a Decimal.")
        if not self.amount_tolerance.is_finite() or self.amount_tolerance < 0:
            raise ProfileError("amount_tolerance must be finite and zero or greater.")
        if not isinstance(self.reconciliation_mode, ReconciliationMode):
            raise ProfileError("reconciliation_mode must be a ReconciliationMode enum member.")

    def validate_columns(self, columns_a: Sequence[str], columns_b: Sequence[str]) -> None:
        """Reject missing directional columns before any configuration is applied."""

        missing = []
        for side, required, available in (
            ("A", (*self.mapping.keys_a, self.mapping.amount_a), columns_a),
            ("B", (*self.mapping.keys_b, self.mapping.amount_b), columns_b),
        ):
            for name in dict.fromkeys(required):
                if name not in available:
                    missing.append(f"File {side} is missing required column {json.dumps(name)}.")
        if missing:
            raise ProfileError("Cannot apply profile: " + " ".join(missing))


def export_mapping_profile(
    mapping: ColumnMapping,
    *,
    amount_tolerance: Decimal = Decimal("0"),
    reconciliation_mode: ReconciliationMode = ReconciliationMode.UNIQUE,
) -> bytes:
    """Serialize a v2 profile to UTF-8 JSON; all tolerance digits are retained."""

    profile = MappingProfile(mapping, amount_tolerance, reconciliation_mode)
    document = {
        "format": _FORMAT,
        "version": _VERSION,
        "key_pairs": [{"file_a": a, "file_b": b} for a, b in profile.mapping.key_pairs],
        "amount_columns": {"file_a": profile.mapping.amount_a, "file_b": profile.mapping.amount_b},
        "amount_tolerance": format(profile.amount_tolerance, "f"),
        "reconciliation_mode": profile.reconciliation_mode.value,
    }
    return (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def _object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProfileError(f"{label} must be an object.")
    if set(value) != fields:
        raise ProfileError(
            f"{label} must contain only these required fields: {', '.join(sorted(fields))}."
        )
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ProfileError("Profile JSON must not contain duplicate field names.")
        result[name] = value
    return result


def _reject_json_number(value: str) -> None:
    raise ProfileError(
        "Profile tolerance must be decimal text, not a JSON number or non-finite value."
    )


def load_mapping_profile(data: bytes | str) -> MappingProfile:
    """Parse and validate a profile atomically, returning structured configuration.

    Column compatibility is checked separately by MappingProfile.validate_columns.
    Version 1 defaults to UNIQUE; version 2 requires an exact supported mode value.
    No numeric tolerance is coerced, and no files or reconciliation data are read.
    """

    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_reject_json_number,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, InvalidOperation) as exc:
        if isinstance(exc, ProfileError):
            raise
        raise ProfileError("Profile must contain valid UTF-8 JSON.") from None
    if not isinstance(document, dict):
        raise ProfileError("Profile must be an object.")
    if "version" not in document:
        raise ProfileError("Profile must contain the required fields: version.")
    version = document["version"]
    if type(version) is not int or version not in (1, 2):
        raise ProfileError("Unsupported profile version; expected version 1 or 2.")
    fields = {"format", "version", "key_pairs", "amount_columns", "amount_tolerance"}
    if version == 2:
        fields.add("reconciliation_mode")
    document = _object(document, fields, "Profile")
    if document["format"] != _FORMAT:
        raise ProfileError("Unsupported profile format.")
    mode = ReconciliationMode.UNIQUE
    if version == 2:
        raw_mode = document["reconciliation_mode"]
        if not isinstance(raw_mode, str):
            raise ProfileError("reconciliation_mode must be a string.")
        try:
            mode = ReconciliationMode(raw_mode)
        except ValueError:
            raise ProfileError(
                "Unsupported reconciliation_mode; expected unique or grouped_by_key."
            ) from None
    raw_pairs = document["key_pairs"]
    if not isinstance(raw_pairs, list):
        raise ProfileError("key_pairs must be an array of key mappings.")
    pairs = []
    for index, raw_pair in enumerate(raw_pairs, start=1):
        pair = _object(raw_pair, {"file_a", "file_b"}, f"Key mapping {index}")
        pairs.append((pair["file_a"], pair["file_b"]))
    amounts = _object(document["amount_columns"], {"file_a", "file_b"}, "amount_columns")
    tolerance_text = document["amount_tolerance"]
    if isinstance(tolerance_text, (int, Decimal)) and not isinstance(tolerance_text, bool):
        _reject_json_number(str(tolerance_text))
    if not isinstance(tolerance_text, str):
        raise ProfileError("amount_tolerance must be decimal text.")
    try:
        tolerance = parse_amount(tolerance_text)
    except AmountParseError:
        raise ProfileError("amount_tolerance must be valid finite monetary text.") from None
    return MappingProfile(
        ColumnMapping(tuple(pairs), amounts["file_a"], amounts["file_b"]),
        tolerance,
        reconciliation_mode=mode,
    )
