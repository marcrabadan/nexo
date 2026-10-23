"""Compile errors of section 3 (and pub types of section 2)."""
import pytest

from helpers import error_of, run

DIVIDE = 'fn divide(a, b):\n    if b == 0:\n        fail("zero")\n    return a / b\n'


def key(src):
    e = error_of(src)
    return getattr(e, "key", None)


def test_ignoring_an_error_inside_a_function():
    assert key(DIVIDE + "fn half(x):\n    return divide(x, 2)\nprint(half(4))\n") == "ignored_error"


def test_ignoring_a_builtin_error():
    assert key('fn load(p):\n    return read(p)\n') == "ignored_error"
    assert key('fn load(p):\n    text = json.parse(p)\n    return text\n') == "ignored_error"


def test_ignored_error_is_reported_before_running():
    # nothing is printed: the check happens before execution
    e = error_of('print("start")\nfn f():\n    x = read("a")\n')
    assert e.key == "ignored_error"


def test_question_passes_the_error():
    out = run(DIVIDE + "fn half(x) -> float!:\n    return divide(x, 2)?\n"
              "try:\n    print(half(4))\n    print(half(0))\nelse e:\n    print(e.message)\n")
    assert out == "2.0\n0.0\n"
    out = run(DIVIDE + "fn half(x) -> float!:\n    return divide(x, 0)?\n"
              "try:\n    print(half(4))\nelse e:\n    print(e.message)\n")
    assert out == "zero\n"


def test_try_inside_a_function_handles_the_error():
    out = run(DIVIDE + 'fn safe(a, b):\n    try:\n        return divide(a, b)\n    else e:\n        return 0\nprint(safe(1, 0))\n')
    assert out == "0\n"


def test_question_at_top_level():
    assert key('x = read("a")?\n') == "question_outside_fn"


def test_question_needs_bang_in_declared_return_type():
    assert key('fn f(p) -> str:\n    return read(p)?\n') == "fallible_needs_bang"
    assert key('fn f(p) -> int:\n    fail("no")\n') == "fallible_needs_bang"


def test_pub_functions_write_their_types():
    assert key('pub fn f(p: str):\n    return read(p)?\n') == "pub_fallible_untyped"
    assert key('pub fn f(p) -> int:\n    return 1\n') == "pub_param_type"
    assert key('pub fn f(p: int):\n    return p\n') == "pub_return_type"
    assert run('pub fn f(p: int) -> int:\n    return p\nprint(f(2))\n') == "2\n"


def test_question_on_a_value_that_cannot_fail():
    assert key('fn f(x: str?) -> str!:\n    return x?\n') == "question_not_error"
    assert key('fn g():\n    return 1\nfn f() -> int!:\n    return g()?\n') == "question_cannot_fail"


def test_coalesce_is_only_for_missing_values():
    assert key('x = read("a") ?? ""\n') == "coalesce_on_error"


def test_ensure_and_old_outside_functions():
    assert key("ensure 1 == 1\n") == "ensure_outside_fn"
    assert key("expect 1 == 1\n") == "ensure_outside_fn"
    assert key("x = old(1)\n") == "old_outside_ensure"


def test_return_outside_function():
    assert key("return 1\n") == "return_outside_fn"


def test_methods_that_can_fail():
    src = ('type Box:\n    v: int\n    fn get(self) -> int!:\n        if self.v < 0:\n            fail("neg")\n        return self.v\n'
           'fn f(b):\n    return b.get()\n')
    assert key(src) == "ignored_error"


def test_top_level_errors_end_the_program():
    e = error_of(DIVIDE + "print(divide(1, 0))\n")
    assert e.kind == "Error" and e.message == "zero" and e.line == 3
