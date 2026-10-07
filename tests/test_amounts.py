from decimal import Decimal, localcontext

import pytest

from tallydiff import AmountParseError, parse_amount


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1200", "1200"),
        ("1200.00", "1200.00"),
        ("1,200.00", "1200.00"),
        ("$1,200.00", "1200.00"),
        ("-1200.00", "-1200.00"),
        ("-$1,200.00", "-1200.00"),
        ("(1,200.00)", "-1200.00"),
        ("($1,200.00)", "-1200.00"),
        ("0", "0"),
        ("0.00", "0.00"),
        ("+1200.00", "1200.00"),
        ("+$1,200.00", "1200.00"),
        ("(1200)", "-1200"),
        ("-0.00", "-0.00"),
        ("(0.00)", "-0.00"),
        (".1250", "0.1250"),
        ("1200.", "1200"),
        ("00012.3400", "12.3400"),
        ("1,234,567.8901", "1234567.8901"),
        (" \t-$1,200.0010\r\n", "-1200.0010"),
        ("0.100000000000000000000000000001", "0.100000000000000000000000000001"),
    ],
)
def test_supported_amounts_preserve_exact_value_scale_and_sign(text: str, expected: str) -> None:
    amount = parse_amount(text)

    assert isinstance(amount, Decimal)
    assert amount.is_finite()
    assert amount.as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \t",
        "abc",
        "12.34.56",
        "$1,2,00",
        "--",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "+Infinity",
        "inf",
        "(NaN)",
        "$Infinity",
        "1,00",
        "12,34",
        "1234,567",
        "1,2345",
        "1,,234",
        ",123",
        "123,",
        "1 200.00",
        "$-1200",
        "-$ 1,200.00",
        "(-1200)",
        "(+$1200)",
        "1.200,00",
        "1_200.00",
        "1e3",
        "1E-3",
        "EUR 1200.00",
        "1.00-",
        "(1.00",
        "1.00)",
        "((1.00))",
        ".",
        "$",
        "\uff11\uff12.\uff13\uff14",
    ],
)
def test_invalid_or_unsupported_amounts_are_never_coerced_to_zero(text: str) -> None:
    with pytest.raises(AmountParseError):
        parse_amount(text)


@pytest.mark.parametrize("value", [10, 0.1, Decimal("1.00"), None])
def test_parser_requires_source_text(value: object) -> None:
    with pytest.raises(TypeError, match="text must be a string"):
        parse_amount(value)


def test_parsing_does_not_round_or_change_the_callers_decimal_context() -> None:
    expected = Decimal("-1200.0000000000000010")
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.Emin = -2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        before = context.copy()

        amount = parse_amount("($1,200.0000000000000010)")

        assert amount.as_tuple() == expected.as_tuple()
        assert context.prec == before.prec
        assert context.Emax == before.Emax
        assert context.Emin == before.Emin
        assert context.traps == before.traps
        assert context.flags == before.flags
