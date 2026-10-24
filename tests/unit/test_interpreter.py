"""Semantics of the core: scopes, types, control flow, errors, pipelines."""
from helpers import error_of, run


# -- variables and scopes ---------------------------------------------------

def test_immutable_and_var():
    assert error_of("x = 1\nx = 2\n").key == "immutable"
    assert error_of("x = 1\nx += 1\n").key == "immutable"
    assert run("var x = 1\nx = 2\nx += 3\nprint(x)\n") == "5\n"
    assert error_of("var x = 1\nvar x = 2\n").key == "already_declared"


def test_loop_bodies_get_a_fresh_scope():
    assert run("for i in 1..3:\n    y = i * 2\n    print(y)\n") == "2\n4\n6\n"


def test_if_and_try_blocks_bind_in_the_enclosing_scope():
    assert run("if true:\n    x = 1\nelse:\n    x = 2\nprint(x)\n") == "1\n"
    assert run('try:\n    d = read("nope")\nelse e:\n    d = "default"\nprint(d)\n') == "default\n"


def test_two_handlers_with_the_same_error_name():
    src = 'try:\n    read("a")\nelse e:\n    print(1)\ntry:\n    read("b")\nelse e:\n    print(2)\n'
    assert run(src) == "1\n2\n"


def test_functions_can_change_var_globals_and_shadow_constants():
    src = "var total = 0\nname = \"g\"\nfn add(n):\n    total += n\n    name = \"local\"\n    return name\nprint(add(5))\nprint(total)\nprint(name)\n"
    assert run(src) == "local\n5\ng\n"


def test_builtins_can_be_shadowed():
    assert run("list = [1]\nprint(list)\n") == "[1]\n"


def test_name_error_with_suggestion():
    e = error_of('name = "Ana"\nprint(nmae)\n')
    assert e.key == "name_not_found" and e.args_["suggestion"] == "name"
    assert e.text() == '"nmae" no existe. ¿Quisiste decir "name"?'
    e = error_of('name = "Ana"\nprint(nmae)\n', lang="en")
    assert e.text() == '"nmae" does not exist. Did you mean "name"?'
    assert error_of("print(zzzzzz)\n").text() == '"zzzzzz" no existe. Defínelo antes de usarlo.'


def test_type_annotations_are_checked():
    assert error_of('age: int = "x"\n').key == "type_mismatch"
    assert error_of("age: int = none\n").key == "none_not_allowed"
    assert run("email: str? = none\nprint(email ?? \"-\")\n") == "-\n"
    assert error_of('var n: int = 1\nn = "x"\n').key == "type_mismatch"
    assert run("x: float = 1\nprint(x)\n") == "1.0\n"


def test_conditions_must_be_bool():
    assert error_of("if 1:\n    pass\n").key == "not_bool"
    assert error_of('x = 1 and true\n').key == "not_bool"


# -- functions ----------------------------------------------------------

def test_named_and_default_arguments():
    src = 'fn greet(person, greeting = "Hello", end = "!"):\n    return "{greeting}, {person}{end}"\n'
    assert run(src + 'print(greet("Ana"))\nprint(greet(greeting: "Hi", person: "Eva"))\n') == "Hello, Ana!\nHi, Eva!\n"
    assert error_of(src + "greet()\n").key == "arg_missing"
    assert error_of(src + 'greet("a", gretting: "x")\n').key == "arg_unknown"
    assert error_of(src + 'greet("a", "b", "c", "d")\n').key == "arg_too_many"
    assert error_of(src + 'greet(person: "a", "b")\n').key == "positional_after_named"


def test_return_is_explicit():
    assert run("fn f():\n    1 + 1\nprint(f())\n") == "none\n"


def test_return_types_are_checked():
    assert error_of('fn f() -> int:\n    return "x"\nf()\n').key == "return_mismatch"
    assert run('fn f() -> str?:\n    return none\nprint(f())\n') == "none\n"


def test_lambdas_and_closures():
    assert run("double = x -> x * 2\nprint(double(4))\nn = 10\nadd = x -> x + n\nprint(add(1))\n") == "8\n11\n"


def test_recursion():
    assert run("fn fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)\nprint(fact(20))\n") == "2432902008176640000\n"


def test_functions_as_values():
    src = 'fn domestic(p):\n    return 5\nfn world(p):\n    return 20\nrates = {"ES": domestic}\nprint((rates["FR"] ?? world)(1))\nprint((rates["ES"] ?? world)(1))\n'
    assert run(src) == "20\n5\n"


# -- types ----------------------------------------------------------------

def test_records_methods_and_mutation():
    src = ("type Account:\n    owner: str\n    balance: float = 0.0\n    fn deposit(self, amount):\n        self.balance += amount\n"
           'a = Account(owner: "Ana")\na.deposit(10)\nprint(a.balance)\nprint(a)\n')
    assert run(src) == '10.0\nAccount(owner: "Ana", balance: 10.0)\n'


def test_record_errors():
    src = "type P:\n    x: int\n"
    assert error_of(src + "P()\n").key == "arg_missing"
    assert error_of(src + 'P(x: "a")\n').key == "type_mismatch"
    assert error_of(src + "p = P(x: 1)\nprint(p.y)\n").key == "no_member"
    assert error_of(src + 'p = P(x: 1)\np.x = "s"\n').key == "type_mismatch"


def test_enums_with_data_and_equality():
    src = "type Shape:\n    Dot\n    Sq(side: int)\nprint(Sq(2) == Sq(2))\nprint(Sq(2) == Sq(3))\nprint(Dot == Dot)\nprint(Sq(2).side)\nprint(Sq(1) is Shape)\n"
    assert run(src) == "true\nfalse\ntrue\n2\ntrue\n"


def test_union_of_values():
    src = 'type Status = "active" | "closed"\nfn set_status(s: Status):\n    return s\nprint(set_status("closed"))\n'
    assert run(src) == "closed\n"
    e = error_of(src + 'set_status("open")\n')
    assert e.key == "type_mismatch" and e.fix == "type_mismatch_union"


def test_traits():
    src = "trait Shape:\n    fn area(self) -> float\ntype Sq is Shape:\n    side: float\n    fn area(self) -> float:\n        return self.side ** 2\n"
    assert run(src + "s = Sq(side: 3.0)\nprint(s.area())\nprint(s is Shape)\nfn total(x: Shape):\n    return x.area()\nprint(total(s))\n") == "9.0\ntrue\n9.0\n"
    assert error_of("type T is Nope:\n    a: int\n").key == "not_a_trait"


def test_is_with_builtin_types():
    assert run('print(1 is int)\nprint(1.0 is int)\nprint("a" is str)\nprint(none is none)\n') == "true\nfalse\ntrue\ntrue\n"


# -- control flow -------------------------------------------------------

def test_match():
    src = ("type Event:\n    Click(x: int, y: int)\n    Key(code: str)\n    Quit\n"
           "fn show(e):\n    match e:\n        Click(x, y) if x > 100:\n            return \"right {y}\"\n"
           "        Click(_, y): return \"left {y}\"\n        Key(\"q\") or Quit: return \"bye\"\n        Key(c): return \"key {c}\"\n")
    out = run(src + 'print(show(Click(150, 1)))\nprint(show(Click(5, 2)))\nprint(show(Quit))\nprint(show(Key("q")))\nprint(show(Key("a")))\n')
    assert out == "right 1\nleft 2\nbye\nbye\nkey a\n"


def test_match_literals_and_no_arm():
    src = 'fn f(n):\n    match n:\n        0: return "zero"\n        1 or 2: return "small"\n        _: return "big"\n'
    assert run(src + "print(f(0))\nprint(f(2))\nprint(f(9))\n") == "zero\nsmall\nbig\n"
    assert error_of("match 3:\n    1: pass\n").key == "no_match"


def test_match_inside_loop_rebinds_names():
    src = "type O:\n    S(v: int)\nfor o in [S(1), S(2)]:\n    match o:\n        S(v): print(v)\nmatch S(3):\n    S(v): print(v)\nmatch S(4):\n    S(v): print(v)\n"
    assert run(src) == "1\n2\n3\n4\n"


def test_while_repeat_and_ranges():
    assert run("var i = 0\nwhile i < 3:\n    i += 1\nrepeat 2:\n    print(i)\nprint(5 in 1..10)\nprint(1..3)\n") == "3\n3\ntrue\n1..3\n"


def test_comprehensions():
    assert run("print([n * n for n in 1..5 if n % 2 == 1])\n") == "[1, 9, 25]\n"


# -- errors -------------------------------------------------------------

def test_error_values_carry_message_cause_and_line():
    src = 'try:\n    x = read("missing.txt")\nelse e:\n    print(e.message)\n    print(e.line)\n    print(e.kind)\n    print(e is NotFound)\n'
    out = run(src)
    assert out.splitlines() == ['No se encuentra el archivo "missing.txt"', "2", "NotFound", "true"]


def test_specific_handlers_in_order():
    src = ('fn f(k):\n    if k == 1:\n        fail("custom")\n    return read("nope")?\n'
           'for k in [1, 2]:\n    try:\n        f(k)\n    else e is NotFound:\n        print("not found")\n    else e:\n        print("other: {e.message}")\n')
    assert run(src) == "other: custom\nnot found\n"


def test_unmatched_handler_reraises():
    e = error_of('try:\n    read("x")\nelse e is ContractBroken:\n    pass\n')
    assert e.kind == "NotFound"


def test_fail_with_interpolation_and_rethrow():
    src = 'fn f(n) -> int!:\n    if n > 2:\n        fail("too big: {n}")\n    return n\ntry:\n    f(5)\nelse e:\n    try:\n        fail(e)\n    else e2:\n        print(e2.message)\n'
    assert run(src) == "too big: 5\n"


def test_try_within():
    src = "try within 1s:\n    x = 1\nelse e:\n    print(e.kind)\nprint(x)\n"
    assert run(src) == "1\n"
    slow = "try within 1ms:\n    var i = 0\n    while i < 100000:\n        i += 1\nelse e is Timeout:\n    print(\"timeout\")\n"
    assert run(slow) == "timeout\n"


# -- contracts ----------------------------------------------------------

def test_expect_and_ensure():
    src = ("fn withdraw(balance, amount):\n    expect amount > 0\n    ensure balance >= 0\n    return balance - amount\n"
           "print(withdraw(10, 3))\ntry:\n    withdraw(10, -1)\nelse e is ContractBroken:\n    print(e.message)\n")
    assert run(src) == "7\nNo se cumple expect amount > 0 (amount = -1)\n"
    assert run(src, lang="en").splitlines()[1] == "expect amount > 0 does not hold (amount = -1)"


def test_old_keeps_the_entry_value():
    src = ("type Box:\n    items: list[int]\nfn push(b: Box, x: int):\n    ensure len(b.items) == len(old(b.items)) + 1\n    b.items.add(x)\n"
           "b = Box(items: [])\npush(b, 1)\nprint(b.items)\n"
           "fn bad(b: Box):\n    ensure len(b.items) == old(len(b.items))\n    b.items.add(0)\n"
           "try:\n    bad(b)\nelse e:\n    print(e.kind)\n")
    assert run(src) == "[1]\nContractBroken\n"


# -- optionals ----------------------------------------------------------

def test_optionals():
    assert run('m = {"a": 1}\nprint(m["b"] ?? 0)\nprint(m["a"] ?? 0)\nx: int? = none\nprint(x ?? 7)\n') == "0\n1\n7\n"
    assert error_of("x: int? = none\nprint(x.foo)\n").key == "none_member"


# -- pipelines ----------------------------------------------------------

def test_pipelines_over_lists():
    assert run("n = [5, 3, 8, 1]\nprint(n |> where it > 2 |> sort by it |> map it * 10)\n") == "[30, 50, 80]\n"
    assert run("print([1, 2, 3, 4] |> where it % 2 == 0 |> sum)\nprint([2, 4] |> avg)\nprint([] |> sum)\n") == "6\n3.0\n0\n"
    assert run('print(["b", "a", "c"] |> sort by it desc |> take 2)\n') == '["c", "b"]\n'


def test_pipelines_over_records():
    src = ('type Sale:\n    region: str\n    amount: int\n'
           'sales = [Sale("n", 10), Sale("s", 5), Sale("n", 7)]\n'
           'print(sales |> where amount > 6 |> map region)\n'
           'print(sales |> group by region |> sum amount)\n'
           'print(sales |> pick region |> take 1)\n')
    assert run(src) == '["n", "n"]\n[{"region": "n", "amount": 17}, {"region": "s", "amount": 5}]\n[{"region": "n"}]\n'


def test_pipelines_are_lazy():
    src = ("var calls = 0\nfn seen(x):\n    calls += 1\n    return x\n"
           "first = [1, 2, 3, 4, 5] |> map seen(it) |> take 2\nprint(first)\nprint(calls)\n"
           "pending = [1, 2, 3] |> map seen(it)\n")
    assert run(src) == "[1, 2]\n2\n"


def test_each_and_function_steps():
    src = 'fn shout(x):\n    print("{x}!")\n["a", "b"] |> each shout\n[1, 2] |> each print(it * 2)\n'
    assert run(src) == "a!\nb!\n2\n4\n"


def test_where_needs_bool():
    assert error_of("[1] |> where it |> each print\n").key == "not_bool"


# -- concurrency --------------------------------------------------------

def test_parallel_for_keeps_order_and_propagates_errors():
    src = ("fn check(n):\n    if n == 2:\n        fail(\"bad {n}\")\n    return n * 10\n"
           "print(parallel for n in [1, 3]:\n    return check(n)\n)\n")
    # a parallel for written inside an argument list is not valid: the
    # block must end the statement
    e = error_of(src)
    assert e.key == "unexpected"
    src = ("fn check(n):\n    if n == 2:\n        fail(\"bad {n}\")\n    return n * 10\n"
           "r = parallel for n in [1, 3]:\n    return check(n)\nprint(r)\n"
           "try:\n    r2 = parallel for n in [1, 2, 3]:\n        return check(n)\nelse e:\n    print(e.message)\n")
    assert run(src) == "[10, 30]\nbad 2\n"


def test_parallel_block_runs_every_task():
    assert run("parallel:\n    a = 1\n    b = 2\nprint(a + b)\n") == "3\n"


# -- events ---------------------------------------------------------------

def test_on_and_emit():
    src = 'on paid(order, amount):\n    print("{order}: {amount}")\nemit paid("A1", 10)\nemit nothing_listens(1)\n'
    assert run(src) == "A1: 10\n"


# -- text ---------------------------------------------------------------

def test_text():
    assert run('x = 3\nprint("a{{b} {x * 2} \\"q\\"")\n') == 'a{b} 6 "q"\n'
    assert run('print("AbC".lower())\nprint(" x ".trim())\nprint("a,b".split(","))\nprint("hello" + " " + "world")\n') == 'abc\nx\n["a", "b"]\nhello world\n'
    assert run('print("ell" in "hello")\nprint(len("ñandú"))\n') == "true\n5\n"


def test_collections():
    src = 'var xs = [1, 2]\nxs.add(3)\nxs[0] = 9\nprint(xs)\nm = {"a": 1}\nm["b"] = 2\nprint(m)\nprint(m.keys())\ns = set(1, 2, 2)\nprint(s)\nprint(len(s))\nfor k in m:\n    print(k)\n'
    assert run(src) == '[9, 2, 3]\n{"a": 1, "b": 2}\n["a", "b"]\nset(1, 2)\n2\na\nb\n'
    assert error_of("xs = [1]\nprint(xs[5])\n").kind == "OutOfRange"


def test_not_supported_features_are_reported():
    assert error_of("use nexo.ui\n").key == "not_supported"
    assert error_of('policy:\n    deny shell\n').key == "not_supported"
    assert error_of("@cache(10m)\nfn f():\n    return 1\n").key == "not_supported"
    assert error_of("every 5m:\n    pass\n").key == "not_supported"


def test_map_keys_must_be_simple_values():
    assert error_of("type A:\n    x: int\nm = {A(1): 2}\n").key == "unhashable"
    assert error_of("m = {[1]: 2}\n").key == "unhashable"
    assert run("type C:\n    Red\n    Blue\nm = {Red: 1}\nprint(m[Red])\nprint(m[Blue] ?? 0)\n") == "1\n0\n"


def test_errors_inside_interpolation_point_at_the_brace():
    e = error_of('print("{1 + }")\n')
    assert e.key == "unexpected" and e.args_["got"] == '"}"'


def test_type_settings_are_not_available():
    assert error_of('type C:\n    name: str\n    purpose = "billing"\n').key == "not_supported"
