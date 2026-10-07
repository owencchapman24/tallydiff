"""Exact Decimal addition isolated from the caller's arithmetic context."""

from collections.abc import Iterable
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)


def sum_decimals(amounts: Iterable[Decimal]) -> Decimal:
    """Sum finite Decimals without silently rounding any significant digits.

    The precision covers the span from the largest leading digit to the
    smallest decimal place, plus room for carries when adding every operand.
    A fresh context also isolates exponent limits, traps, and status flags.
    """

    values = tuple(amounts)
    if not values:
        return Decimal("0")

    smallest_exponent = min(value.as_tuple().exponent for value in values)
    largest_adjusted = max(value.adjusted() for value in values)
    precision = max(1, largest_adjusted - smallest_exponent + 1) + len(str(len(values)))
    context = Context(
        prec=precision,
        Emin=MIN_EMIN,
        Emax=MAX_EMAX,
        traps=[Inexact, InvalidOperation, Overflow],
    )
    with localcontext(context):
        return sum(values, Decimal("0"))
