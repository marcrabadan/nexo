"""Section 5: the tasks of `parallel` (sequential in phase 0, with the
rules that make them safe to run at the same time in phase 1)."""
from helpers import error_of, run


def test_results_and_output_keep_the_order():
    src = "r = parallel for n in [3, 1, 2]:\n    print(n)\n    return n * 10\nprint(r)\n"
    assert run(src) == "3\n1\n2\n[30, 10, 20]\n"


def test_the_first_failure_stops_the_rest():
    src = (
        "fn f(n):\n    if n == 2:\n        fail(\"bad {n}\")\n    print(n)\n    return n\n"
        "try:\n    r = parallel for n in [1, 2, 3]:\n        return f(n)\nelse e:\n    print(e.message)\n"
    )
    assert run(src) == "1\nbad 2\n"


def test_a_task_cannot_change_outer_variables():
    assert error_of("var t = 0\nparallel for n in [1]:\n    t += n\n").key == "task_write_var"
    assert error_of("var xs = []\nparallel for n in [1]:\n    xs.add(n)\n").key == "task_write_var"
    src = "var t = 0\nfn bump(n):\n    t += n\nparallel for n in [1]:\n    bump(n)\n"
    assert error_of(src).key == "task_write_var"


def test_a_task_cannot_change_records_created_before_it():
    src = "type C:\n    v: int\nc = C(0)\nparallel for n in [1]:\n    c.v += n\n"
    assert error_of(src).key == "record_shared"
    src = "type C:\n    v: int\nr = parallel for n in [1, 2]:\n    c = C(n)\n    c.v += 1\n    return c\nr[0].v = 9\nprint(r)\n"
    assert run(src) == "[C(v: 9), C(v: 3)]\n"


def test_parallel_block_rules():
    assert run("parallel:\n    a = 1\n    b = 2\nprint(a + b)\n") == "3\n"
    assert error_of("parallel:\n    a = 1\n    b = a\n").key == "parallel_sibling"
    assert error_of("parallel:\n    a = 1\n    a = 2\n").key == "parallel_twice"
    assert error_of("fn f():\n    parallel:\n        return 1\n").key == "parallel_return"
    src = "fn f():\n    var acc = 0\n    parallel:\n        acc = 5\n        b = 1\n    return acc + b\nprint(f())\n"
    assert run(src) == "6\n"
