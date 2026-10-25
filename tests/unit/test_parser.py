from pathlib import Path

import pytest

from nexo import ast as A
from nexo.errors import NexoError
from nexo.parser import parse

CONFORMANCE = Path(__file__).resolve().parent.parent / "conformance"
SYNTAX_ERRORS = {"s03_else_without_var", "s24_tab", "s24_indent", "s06_sum_without_group"}


def stmt(src):
    return parse(src, "t.nexo").body[0]


def expr(src):
    s = stmt(src + "\n")
    assert isinstance(s, A.ExprStmt)
    return s.expr


@pytest.mark.parametrize(
    "path",
    [p for p in sorted(CONFORMANCE.rglob("*.nexo")) if p.stem not in SYNTAX_ERRORS],
    ids=lambda p: p.stem,
)
def test_conformance_programs_parse(path):
    parse(path.read_text(encoding="utf-8"), path.name)


def test_precedence_arithmetic():
    e = expr("1 + 2 * 3 ** 2")
    assert isinstance(e, A.BinOp) and e.op == "+"
    assert e.right.op == "*" and e.right.right.op == "**"


def test_power_is_right_associative_and_unary_binds_tighter():
    e = expr("-2 ** 3 ** 2")
    assert e.op == "**" and isinstance(e.left, A.Unary) and e.right.op == "**"


def test_logic_precedence():
    e = expr("not a and b or c")
    assert e.op == "or" and e.left.op == "and" and isinstance(e.left.left, A.Unary)


def test_comparison_chain_and_in_is():
    e = expr("1 < x <= 10")
    assert isinstance(e, A.Compare) and e.ops == ["<", "<="]
    assert expr("x is Shape").ops == ["is"]
    assert expr('"a" in xs').ops == ["in"]


def test_coalesce_and_range():
    e = expr("a ?? b .. c")
    assert isinstance(e, A.Coalesce) and isinstance(e.right, A.RangeExpr)


def test_lambda():
    s = stmt("double = x -> x * 2\n")
    assert isinstance(s, A.Assign) and isinstance(s.value, A.Lambda) and s.value.param == "x"


def test_postfix_chain_and_named_args():
    e = expr('json.parse(text, as: Config)?')
    assert isinstance(e, A.Propagate)
    call = e.expr
    assert isinstance(call.func, A.Attr) and call.func.name == "parse"
    assert [a.name for a in call.args] == [None, "as"]


def test_assignment_forms():
    s = stmt("age: int = 30\n")
    assert isinstance(s, A.Assign) and s.type.name == "int"
    s = stmt("email: str? = none\n")
    assert s.type.optional
    s = stmt("source.balance -= amount\n")
    assert s.op == "-=" and isinstance(s.target, A.Attr)
    s = stmt("var counter = 0\n")
    assert isinstance(s, A.VarDecl) and s.name == "counter"


def test_invalid_target():
    with pytest.raises(NexoError) as e:
        parse("f(x) = 3\n", "t.nexo")
    assert e.value.key == "invalid_target"


def test_fn_def_with_defaults_and_return_type():
    s = stmt('pub fn load_config(path: str, n = 3) -> Config!:\n    return 1\n')
    assert s.pub and s.name == "load_config"
    assert s.params[0].type.name == "str" and s.params[1].default.text == "3"
    assert s.ret.fallible and s.ret.name == "Config"


def test_type_record_enum_union_trait():
    prog = parse(
        "type User:\n    name: str\n    email: str? = none\n    fn f(self) -> bool:\n        return true\n"
        'type Status = "active" | "closed"\n'
        "type Event:\n    Click(x: int, y: int)\n    Quit\n"
        "trait Shape:\n    fn area(self) -> float\n"
        "type Circle is Shape:\n    radius: float\n",
        "t.nexo",
    ).body
    user, status, event, shape, circle = prog
    assert [f.name for f in user.fields] == ["name", "email"] and user.methods[0].name == "f"
    assert isinstance(status.alias, A.LiteralUnion) and len(status.alias.literals) == 2
    assert [v.name for v in event.variants] == ["Click", "Quit"]
    assert [p.name for p in event.variants[0].params] == ["x", "y"]
    assert shape.sigs[0].name == "area"
    assert circle.trait == "Shape"


def test_type_mixing_fields_and_variants():
    with pytest.raises(NexoError) as e:
        parse("type T:\n    a: int\n    B\n", "t.nexo")
    assert e.value.key == "type_mixed"


def test_match_arms():
    s = stmt(
        "match event:\n"
        '    Click(x, y) if x > 100: print("right")\n'
        '    Key("q") or Quit:       close()\n'
        "    _:\n        pass\n"
    )
    a, b, c = s.arms
    assert isinstance(a.patterns[0], A.PCtor) and a.guard is not None
    assert isinstance(b.patterns[0].args[0], A.PLit) and isinstance(b.patterns[1], A.PName)
    assert isinstance(c.patterns[0], A.PWild) and isinstance(c.body[0], A.Pass)


def test_try_else_clauses():
    s = stmt("try:\n    x = 1\nelse e is NotFound:\n    pass\nelse e:\n    pass\n")
    assert [(h.var, h.kind) for h in s.handlers] == [("e", "NotFound"), ("e", None)]


def test_try_else_without_variable():
    with pytest.raises(NexoError) as e:
        parse("try:\n    x = 1\nelse:\n    pass\n", "t.nexo")
    assert e.value.key == "else_needs_var"


def test_try_needs_else():
    with pytest.raises(NexoError):
        parse("try:\n    x = 1\nprint(1)\n", "t.nexo")


def test_pipeline_steps():
    e = expr("read(f) |> where amount > 1000 |> group by region |> sum amount |> sort by amount desc |> take 3 |> save(\"o.csv\")")
    kinds = [s.kind for s in e.steps]
    assert kinds == ["where", "group", "sum", "sort", "take", "call"]
    assert e.steps[3].desc


def test_pipeline_continuation_lines():
    s = stmt('positives = numbers\n  |> where it > 0\n  |> map it * 2\n')
    assert isinstance(s.value, A.Pipeline) and len(s.value.steps) == 2


def test_pick_columns():
    e = expr("t |> pick name, email")
    assert [c.name for c in e.steps[0].columns] == ["name", "email"]


def test_sum_column_needs_group_by():
    with pytest.raises(NexoError) as e:
        parse("x = t |> sum amount\n", "t.nexo")
    assert e.value.key == "pipe_needs_group"


def test_only_aggregates_after_group_by():
    with pytest.raises(NexoError) as e:
        parse("x = t |> group by a |> take 3\n", "t.nexo")
    assert e.value.key == "pipe_group_next"


def test_parallel_for_and_block():
    prog = parse(
        "pages = parallel for url in urls, max: 8:\n    return url\nparallel:\n    a = 1\n    b = 2\n",
        "t.nexo",
    ).body
    assert isinstance(prog[0].value, A.ParallelFor) and prog[0].value.args[0].name == "max"
    assert isinstance(prog[1], A.ParallelBlock) and len(prog[1].body) == 2


def test_parallel_for_statement():
    s = stmt("parallel for img in images, max: 8:\n    compress(img)\nprint(1)\n")
    assert isinstance(s, A.ExprStmt) and isinstance(s.expr, A.ParallelFor)


def test_comprehension_and_collections():
    e = expr("[n * 2 for n in list if n > 0]")
    assert isinstance(e, A.Comprehension) and e.var == "n" and e.condition is not None
    m = expr('{"pear": 1.2, "grape": 3}')
    assert isinstance(m, A.MapLit) and len(m.pairs) == 2
    assert expr("{}").pairs == [] and expr("[]").items == []


def test_string_interpolation_is_parsed():
    e = expr('"Hello {name}, you are {age + 1}"')
    assert isinstance(e.parts[1], A.Name) and isinstance(e.parts[3], A.BinOp)


def test_contracts_tests_and_asserts():
    prog = parse(
        "fn f(a):\n    expect a > 0\n    ensure a == old(a)\n    return a\n"
        'test "t":\n    assert f(1) == 1\n    assert g() fails\n',
        "t.nexo",
    ).body
    f, t = prog
    assert isinstance(f.body[0], A.Expect) and isinstance(f.body[1], A.Ensure)
    assert isinstance(t, A.TestDef) and t.name == "t"
    assert not t.body[0].fails and t.body[1].fails


def test_reserved_word_as_name():
    with pytest.raises(NexoError) as e:
        parse("var if = 3\n", "t.nexo")
    assert e.value.key == "reserved_word"


def test_unexpected_token_message():
    with pytest.raises(NexoError) as e:
        parse("x = (1 + )\n", "t.nexo")
    assert e.value.key == "unexpected"
    assert "an expression" in e.value.text()


def test_events_and_use():
    prog = parse("use nexo.db\nuse python numpy as np\non created(o):\n    pass\nemit created(1)\n", "t.nexo").body
    assert prog[0].name == "nexo.db" and prog[0].lang is None
    assert prog[1].lang == "python" and prog[1].name == "numpy" and prog[1].alias == "np"
    assert isinstance(prog[2], A.Assign) and prog[2].target.name == "np"  # np = __python_import("numpy")
    assert isinstance(prog[3], A.EventStmt) and isinstance(prog[4], A.Emit)


def test_config_block_is_recognised():
    prog = parse('policy:\n    deny shell\nprint(1)\n', "t.nexo").body
    assert isinstance(prog[0], A.ConfigBlock) and prog[0].word == "policy"
    prog = parse('ai:\n    provider = "anthropic"\n    key = secret("K")\nprint(1)\n', "t.nexo").body
    assert isinstance(prog[0], A.ExprStmt) and prog[0].expr.func.name == "__ai_config"
    assert isinstance(prog[1], A.ExprStmt)


def test_spans_cover_expressions():
    e = expr("print(x + 1)")
    arg = e.args[0].value
    assert (arg.span.col, arg.span.end_col) == (7, 12)


def test_duplicate_names():
    for src in ("fn f(a, a):\n    pass\n", "type T:\n    a: int\n    a: str\n", "type E:\n    A\n    A\n",
                "type T:\n    a: int\n    fn a(self):\n        pass\n"):
        with pytest.raises(NexoError) as e:
            parse(src, "t.nexo")
        assert e.value.key == "duplicate_name"


MODULES = """use nexo.db
use python numpy as np
use c "libm"
@table
type Customer:
    id: id
    email: str @unique
    orders: many Order
ai models:
    fast = "openai:gpt-mini"
ai:
    model = fast
@tool "Looks up"
fn find(id: str) -> str:
    return id
agent support:
    tools = [find]
server api:
    port = 8080
    @limit(10 per minute per ip)
    get "/users/{id}"(id: int):
        return id
db.transaction:
    print(1)
x = ask("hi", model: fast)
test "t":
    fake ask returns "x"
"""


def summary(body):
    out = []
    for s in body:
        if isinstance(s, A.Use):
            out.append(f"use {s.lang + ' ' if s.lang else ''}{s.name}")
        elif isinstance(s, A.Assign):
            out.append(f"{s.target.name} =")
        elif isinstance(s, A.TypeDef):
            out.append(f"type {s.name} {len(s.fields)}")
        elif isinstance(s, A.FnDef):
            out.append(f"fn {s.name}")
        elif isinstance(s, A.ExprStmt):
            out.append(f"{s.expr.func.name}()")
        elif isinstance(s, A.TryStmt):
            out.append(f"try {len(s.body)}")
        elif isinstance(s, A.TestDef):
            out.append("test")
    return out


def test_module_blocks_desugar():
    # the same list as the compiler's parser test module_blocks_desugar (D90)
    body = parse(MODULES, "t.nexo").body
    assert summary(body) == [
        "use nexo.db", "use python numpy", "np =", "use c libm", "libm =", "type Customer 2", "__db_table()",
        "__ai_models()", "__ai_config()", "fn find", "__ai_tool()", "support =", "api =", "fn GET /users/{id}",
        "__web_route()", "__tx23_1 =", "try 2", "x =", "test"]
    assert isinstance(body[17].value.args[1].value, A.Str)
    for src, key in [("@table\ntype A:\n    id: id\n", "needs_use"), ("type A:\n    b: ref B\n", "table_only"),
                     ("use nexo.db\nfn f():\n    db.transaction:\n        return 1\n", "transaction_return")]:
        with pytest.raises(NexoError) as e:
            parse(src, "t.nexo")
        assert e.value.key == key


def test_phase_5_blocks_desugar_into_entries():
    # D114: each line is a __cfg(head, values, options, body)
    src = ('infra shop:\n    cloud = "aws"\n    require tags ["a"]\n    database customers:\n        size = medium\n'
           '    output db = customers.url, secret\n    dns "x.com" -> api, https\nflags:\n    a = on, expires: 2027-03-01\n'
           'pipeline main:\n    step build:\n        image = "x"\n    step dev:\n        print(image)\n')
    prog = parse(src, "t.nexo").body
    infra = prog[0].expr
    assert infra.func.name == "__infra" and infra.args[0].value.parts == ["shop"]
    entries = infra.args[1].value.items
    assert [e.func.name for e in entries] == ["__cfg"] * 5
    tags = entries[1].args[0].value.items[0].items
    assert [type(a).__name__ for a in tags] == ["Str", "Str", "ListLit"]
    out = entries[3]
    assert out.args[1].value.items[0].items[0].func.name == "__ref"
    assert out.args[1].value.items[1].items[0].parts == ["secret"]
    db = entries[2].args[3].value.items[0]
    assert db.args[1].value.items[0].items[0].parts == ["medium"]
    flags = prog[1].expr
    expires = flags.args[0].value.items[0].args[2].value.pairs[0][1]
    assert expires.func.name == "date" and [a.value.text for a in expires.args] == ["2027", "3", "1"]
    # the steps are functions that keep and get the pipeline's variables
    assert [getattr(s, "name", None) for s in prog[2:4]] == ["main step build", "main step dev"]
    assert prog[4].expr.func.name == "__pipeline"
    dev = prog[3].body
    assert dev[0].value.func.name == "__ci_get" and dev[0].target.name == "image"
    assert prog[2].body[-1].expr.func.name == "__ci_keep"
