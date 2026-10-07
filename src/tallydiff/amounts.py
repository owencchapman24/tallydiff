"""Strict parsing of U.S.-style monetary text directly into Decimal."""

import re
from decimal import Decimal

_INTEGER = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)"
_AMOUNT = re.compile(rf"(?P<sign>[+-]?)\$?(?P<number>{_INTEGER}(?:\.[0-9]*)?|\.[0-9]+)")


class AmountParseError(ValueError):
    """Source text does not satisfy the supported monetary grammar."""


def parse_amount(text: str) -> Decimal:
    """Parse an exact finite amount without floats, rounding, or sign guessing.

    Accept ASCII digits, period decimals, correctly grouped thousands commas,
    an optional dollar prefix, and a leading sign or accounting parentheses.
    Only whitespace surrounding the entire value is ignored. Scientific
    notation, locale inference, and currency conversion are not supported.
    """

    if not isinstance(text, str):
        raise TypeError("amount text must be a string")
    value = text.strip()
    if not value:
        raise AmountParseError("amount is blank")

    accounting_negative = value.startswith("(") and value.endswith(")")
    body = value[1:-1] if accounting_negative else value
    match = _AMOUNT.fullmatch(body)
    if match is None:
        raise AmountParseError(
            "expected a U.S.-style decimal amount with optional dollar prefix, "
            "leading sign or parentheses, and correctly grouped thousands commas"
        )
    if accounting_negative and match["sign"]:
        raise AmountParseError("accounting parentheses cannot be combined with a sign")

    sign = "-" if accounting_negative or match["sign"] == "-" else ""
    return Decimal(sign + match["number"].replace(",", ""))
