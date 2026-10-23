"""Section 17: int, float, decimal and money."""
from helpers import error_of, run


def test_int_overflow_is_an_error():
    e = error_of("x = 9223372036854775807\nprint(x + 1)\n")
    assert e.kind == "Overflow"
    assert error_of("print(-9223372036854775807 - 2)\n").kind == "Overflow"
    assert error_of("print(2 ** 64)\n").kind == "Overflow"
    assert error_of("print(9223372036854775808)\n").kind == "Overflow"
    assert run("print(2 ** 62)\n") == "4611686018427387904\n"


def test_division():
    assert run("print(7 / 2)\nprint(7 // 2)\nprint(-7 // 2)\nprint(7 % 3)\nprint(4 / 2)\n") == "3.5\n3\n-4\n1\n2.0\n"
    assert error_of("print(1 / 0)\n").kind == "DivisionByZero"
    assert error_of("print(1.5 // 0)\n").kind == "DivisionByZero"


def test_decimal_from_context():
    assert run("x: decimal = 0.1\nprint(x + 0.2)\nprint(x * 3)\n") == "0.3\n0.3\n"
    assert run("price: decimal = 19.99\nprint(price * 3)\n") == "59.97\n"
    assert run("fn f(d: decimal):\n    return d + 0.2\nprint(f(0.1))\n") == "0.3\n"
    assert run("print(decimal(1) / 3)\n") == "0.3333333333333333333333333333\n"


def test_mixing_float_and_decimal():
    assert error_of("x: decimal = 0.1\ny = 0.2\nprint(x + y)\n").key == "mix_numbers"
    assert error_of("x: decimal = 0.1\nprint(x == 0.5 * 2)\n").key == "mix_numbers"
    assert run("x: decimal = 0.1\ny = 0.2\nprint(x + decimal(y))\nprint(float(x) + y)\n") == "0.3\n0.30000000000000004\n"


def test_float_stays_float():
    assert run("print(0.1 + 0.2)\n") == "0.30000000000000004\n"
    assert run("y = 0.5\nprint(y * 2)\n") == "1.0\n"


def test_money():
    assert run("invoice: money = 120.00 EUR\nprint(invoice * 21%)\n") == "25.20 EUR\n"
    assert run("print(19.99 EUR * 3)\nprint(10.00 EUR / 4)\nprint(10.00 EUR - 2.50 EUR)\n") == "59.97 EUR\n2.50 EUR\n7.50 EUR\n"
    assert run("print(split(100.00 EUR, 3))\n") == "[33.34 EUR, 33.33 EUR, 33.33 EUR]\n"
    assert run("print(split(0.05 EUR, 2))\n") == "[0.03 EUR, 0.02 EUR]\n"
    assert run("print(1.5 * 10.00 EUR)\n") == "15.00 EUR\n"
    assert run("print(10.00 EUR > 5.00 EUR)\n") == "true\n"


def test_money_errors():
    assert error_of("print(10.00 EUR + 5.00 USD)\n").key == "currency_mismatch"
    assert error_of("print(10.00 EUR + 5)\n").key == "bad_operands"
    assert error_of("x = 1.5\nprint(10.00 EUR * x)\n").key == "mix_numbers"


def test_convert_currency():
    assert run("print(convert(10.00 EUR, to: USD, rate: 1.1))\n") == "11.00 USD\n"


def test_rounding():
    assert run("print(round(2.5))\nprint(round(3.5))\n") == "2.0\n4.0\n"
    assert run("print(round(decimal(2.675), 2))\nprint(round(decimal(2.665), 2))\n") == "2.68\n2.66\n"
    assert run("print(round(decimal(2.665), 2, mode: half_up))\n") == "2.67\n"
    assert run("print(round(7))\n") == "7\n"


def test_units():
    assert run("print(30s + 1m)\nprint(1m + 30s)\nprint(2h > 90m)\nprint(5 km * 2)\n") == "90s\n90s\ntrue\n10 km\n"
    assert run("print(200 * 10%)\nprint(10% + 5%)\n") == "20\n15%\n"
    assert error_of("print(5 km + 3s)\n").key == "unit_mismatch"


def test_conversions_do_not_parse_text():
    assert error_of('print(int("12"))\n').key == "bad_argument"
    assert run('print(int(3.9))\nprint(float(2))\nprint(decimal(2.5))\n') == "3\n2.0\n2.5\n"
