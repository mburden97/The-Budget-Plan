from decimal import Decimal

import pytest

from budgetapp.money import (
    cents_to_input,
    format_money,
    format_quantity,
    parse_money,
    parse_percent,
)


@pytest.mark.parametrize(
    ("value", "text"),
    [("20", "20"), ("2E+1", "20"), ("350.000", "350"), ("0.00012300", "0.000123"),
     ("1234.5", "1,234.5"), ("0", "0")],
)
def test_format_quantity_never_uses_exponents(value, text):
    assert format_quantity(Decimal(value)) == text


@pytest.mark.parametrize(
    ("text", "cents"),
    [("1234.56", 123456), ("$1,234.56", 123456), ("50", 5000), (" 0.5 ", 50), ("0", 0)],
)
def test_parse_money(text, cents):
    assert parse_money(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1.234", "-5", "nan", "inf", "1e20"])
def test_parse_money_rejects(text):
    with pytest.raises(ValueError):
        parse_money(text)


def test_parse_money_allows_negative_when_asked():
    assert parse_money("-12.30", allow_negative=True) == -1230


def test_format_money():
    assert format_money(123456) == "$1,234.56"
    assert format_money(-500) == "-$5.00"
    assert format_money(7) == "$0.07"
    assert cents_to_input(123456) == "1234.56"
    assert cents_to_input(-7) == "-0.07"


def test_parse_percent():
    assert parse_percent("5.25") == Decimal("0.0525")
    assert parse_percent("6%") == Decimal("0.06")
    with pytest.raises(ValueError):
        parse_percent("101")
