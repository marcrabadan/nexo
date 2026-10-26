"""`use c` and `use python` (section 18, D110-D111) in both phases."""
import math
import shutil
import subprocess
import sys

import pytest

from phases import commands, run

needs_cc = pytest.mark.skipif(shutil.which("cc") is None, reason="needs a C compiler")

GREET_H = """#include <stdbool.h>
#include <stdint.h>
const char *greet(const char *name);
double mix(int a, double b, const char *s, float f, unsigned char c, long long d, bool flag);
float half(float x);
int8_t neg8(int8_t x);
uint32_t big32(void);
bool is_even(long n);
void touch(void);
int sum(int count, ...);
int fill(char *out);
struct point { int x; int y; };
struct point origin(void);
"""

GREET_C = """#include <stdio.h>
#include <string.h>
#include "greet.h"
static char buf[64];
const char *greet(const char *name) { snprintf(buf, sizeof buf, "hello %s", name); return buf; }
double mix(int a, double b, const char *s, float f, unsigned char c, long long d, bool flag) {
    return a + b + strlen(s) + f + c + d + (flag ? 1000 : 0);
}
float half(float x) { return x / 2; }
int8_t neg8(int8_t x) { return -x; }
uint32_t big32(void) { return 4000000000u; }
bool is_even(long n) { return n % 2 == 0; }
void touch(void) { }
int sum(int count, ...) { return count; }
int fill(char *out) { out[0] = 0; return 0; }
struct point origin(void) { struct point p = {0, 0}; return p; }
"""

C_PROGRAM = """use c "libz"
use c "libm"
use c "./libgreet.so" as greet

print(libz.crc32(0, "hello", 5), libz.compressBound(100))
print(libm.sqrt(2.0), libm.pow(2, 10), libm.lround(2.5), libm.cbrtf(27.0))
print(greet.greet("Ana"))
print(greet.mix(1, 2.5, "abc", 0.25, 255, 10000000000, true))
print(greet.half(3), greet.neg8(-128), greet.big32(), greet.is_even(4), greet.touch())
print(greet.neg8(200))
print(greet)
"""

C_EXPECTED = """907060870 113
1.4142135623730951 1024.0 3 3.0
hello Ana
10000001261.75
1.5 -128 4000000000 true none
56
c_library("./libgreet.so")
"""

C_ERRORS = [
    ('greet.sum(1, 2)', 'greet.sum takes a variable number of arguments, which Nexo cannot pass to C.'),
    ('greet.fill("x")', 'greet.fill uses the type "char *", which Nexo cannot pass to C.'),
    ('greet.origin()', 'greet.origin uses the type "struct point", which Nexo cannot pass to C.'),
    ('greet.half("x")', 'greet.half: expects float, but gets str.'),
    ('greet.half(1, 2)', '"greet.half" takes at most 1 arguments, but 2 are given.'),
    ('greet.gret("x")', 'c_library has no field or method "gret". Did you mean "greet"?'),
]


@pytest.fixture(scope="module")
def greet_lib(tmp_path_factory):
    d = tmp_path_factory.mktemp("clib")
    (d / "greet.h").write_text(GREET_H)
    (d / "greet.c").write_text(GREET_C)
    subprocess.run(["cc", "-shared", "-fPIC", "-o", str(d / "libgreet.so"), str(d / "greet.c")], check=True)
    return d


@needs_cc
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_use_c(phase, greet_lib):
    (greet_lib / "app.nexo").write_text(C_PROGRAM)
    proc = run(phase[1], ["app.nexo"], greet_lib)
    assert proc.stdout.decode() == C_EXPECTED
    for line, message in C_ERRORS:
        (greet_lib / "bad.nexo").write_text(f'use c "./libgreet.so" as greet\n{line}\n')
        out = run(phase[1], ["bad.nexo"], greet_lib).stdout.decode()
        assert out.splitlines()[3].startswith(message), (line, out)


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_use_c_missing(phase, tmp_path):
    (tmp_path / "app.nexo").write_text('print("start")\nuse c "libnexo_not_there"\n')
    out = run(phase[1], ["app.nexo"], tmp_path).stdout.decode()
    assert out == ('start\nError in app.nexo, line 2:\n  use c "libnexo_not_there"\n  ^^^^^^^^^^^^^^^^^^^^^^^^^\n'
                   'The C library "libnexo_not_there" cannot be found. Install the library (and its development package, with the '
                   'header) or write its path: use c "./lib/mylib.so" as mylib.\n')


PY_PROGRAM = """use python math
use python statistics as stats
use python collections
use python decimal as pydec
use python json
use python fractions

print(math.sqrt(16), math.pi > 3, math.floor(2.7))
print(stats.mean([1, 2, 3, 4]), stats.median([3, 1, 2]))
c = collections.Counter("banana")
print(c.most_common(1), c)
print(pydec.Decimal("1.10") + 0, pydec.Decimal(2.5))
print(json.dumps({"b": [1, 2.5, true, none], "a": 1}, sort_keys: true))
print(json.loads("[1, 2, 3]"), json.loads("1e400"))
type Point:
    x: int
    y: int
print(json.dumps(Point(x: 1, y: 2)))
f = fractions.Fraction(1, 3)
print(f, f.numerator, f.limit_denominator(10))
print(collections.namedtuple("P", ["a", "b"])(1, 2))
print(math)
try:
    math.sqrt(-1)
else e:
    print(e.kind, e.message)
try:
    json.loads("{{")
else e:
    print(e.message)
"""

PY_EXPECTED = """4.0 true 2
2.5 2
[["a", 3]] Counter({'a': 3, 'n': 2, 'b': 1})
1.10 2.5
{"a": 1, "b": [1, 2.5, true, null]}
[1, 2, 3] inf
{"x": 1, "y": 2}
Fraction(1, 3) 1 Fraction(1, 3)
P(a=1, b=2)
<module 'math' (built-in)>
Error ValueError in Python: math domain error
JSONDecodeError in Python: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)
"""

PY_ERRORS = [
    ("use python nexo_no_such_module", 'The Python module "nexo_no_such_module" cannot be imported '
                                       "(ModuleNotFoundError: No module named 'nexo_no_such_module')."),
    ("use python math\nmath.sqroot(2)", 'python has no field or method "sqroot". Did you mean "sqrt"?'),
    ("use python math\nmath.floor(5 EUR)", "A money value cannot be passed to Python."),
    ("use python math\nmath.factorial(30)", "Unhandled error (Overflow)"),
]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_use_python(phase, tmp_path):
    env = {"NEXO_PYTHON": sys.executable}
    (tmp_path / "app.nexo").write_text(PY_PROGRAM)
    proc = run(phase[1], ["app.nexo"], tmp_path, env=env)
    # CPython may build math into the executable or load it from a .so.
    expected = PY_EXPECTED.replace("<module 'math' (built-in)>", repr(math))
    assert proc.stdout.decode() == expected
    for source, message in PY_ERRORS:
        (tmp_path / "bad.nexo").write_text(source + "\n")
        out = run(phase[1], ["bad.nexo"], tmp_path, env=env).stdout.decode()
        assert out.splitlines()[3].startswith(message), (source, out)
