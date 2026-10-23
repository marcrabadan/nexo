"""Standard library: io and data (JSON, CSV)."""
import pytest

from helpers import error_of, run


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_write_read_and_read_lines(in_tmp):
    src = 'write("a.txt", "one\\ntwo\\nthree\\n")\nprint(read("a.txt"))\nfor l in read_lines("a.txt") |> take 2:\n    print("> {l}")\n'
    assert run(src) == "one\ntwo\nthree\n\n> one\n> two\n"
    assert (in_tmp / "a.txt").read_text() == "one\ntwo\nthree\n"


def test_csv_columns_have_one_type(in_tmp):
    (in_tmp / "t.csv").write_text("a,b,c\n1,1,x\n2,2.5,3\n")
    assert run('print(read("t.csv"))\n') == '[{"a": 1, "b": 1.0, "c": "x"}, {"a": 2, "b": 2.5, "c": "3"}]\n'


def test_read_missing_file_is_not_found():
    e = error_of('read("missing.txt")\n')
    assert e.kind == "NotFound" and e.message == 'No se encuentra el archivo "missing.txt"'
    e = error_of('read_lines("missing.txt")\n', lang="en")
    assert e.message == 'File "missing.txt" not found'


def test_csv_round_trip(in_tmp):
    (in_tmp / "p.csv").write_text("name,price,qty,note\npear,1.5,3,\ngrape,2,10,ripe\n")
    src = ('rows = read("p.csv")\nprint(rows)\n'
           'rows |> where qty > 5 |> pick name, qty |> save("out.csv")\nprint(read("out.csv"))\n')
    assert run(src) == ('[{"name": "pear", "price": 1.5, "qty": 3, "note": none}, {"name": "grape", "price": 2.0, "qty": 10, "note": "ripe"}]\n'
                        '[{"name": "grape", "qty": 10}]\n')
    assert (in_tmp / "out.csv").read_text() == "name,qty\ngrape,10\n"


def test_save_records_as_csv_and_json(in_tmp):
    src = ('type P:\n    name: str\n    price: decimal\nps = [P("a", 1.10), P("b", 2)]\nps |> save("p.csv")\nps |> save("p.json")\n'
           'print(read("p.csv"))\n')
    assert run(src) == '[{"name": "a", "price": 1.1}, {"name": "b", "price": 2.0}]\n'
    assert (in_tmp / "p.json").read_text() == '[\n  {\n    "name": "a",\n    "price": 1.10\n  },\n  {\n    "name": "b",\n    "price": 2\n  }\n]\n'


def test_json_parse_with_and_without_type():
    src = ('type Server:\n    host: str\n    port: int\ntype Config:\n    name: str\n    server: Server\n    tags: list[str]\n    debug: bool = false\n'
           'text = """{{"name": "app", "server": {{"host": "h", "port": 80}, "tags": ["a"]}"""\n'
           'try:\n    c = json.parse(text, as: Config)\n    print(c.server.port)\n    print(c.debug)\n    print(json.parse("[1, 2.5, null]"))\nelse e:\n    print(e.message)\n')
    assert run(src) == "80\nfalse\n[1, 2.5, none]\n"


def test_json_errors_are_invalid_data():
    src = 'type C:\n    port: int\ntry:\n    json.parse("{{\\"port\\": \\"x\\"}", as: C)\nelse e is InvalidData:\n    print(e.message)\ntry:\n    json.parse("{{bad")\nelse e:\n    print(e.kind)\ntry:\n    json.parse("{{}", as: C)\nelse e:\n    print(e.message)\n'
    assert run(src) == ('Datos no válidos: el campo "port" debe ser int, pero es str\nInvalidData\n'
                        'Datos no válidos: falta el campo "port" de C\n')


def test_validate():
    src = ('fn age(text) -> int!:\n    return validate(text, as: int, range: 0..120)?\n'
           'for t in ["42", "x", "200"]:\n    try:\n        print(age(t))\n    else e:\n        print(e.message)\n')
    assert run(src) == '42\n"x" no es un valor int válido\n200 está fuera del rango 0..120\n'


def test_len_and_range():
    assert run('print(len([1, 2]))\nprint(len({"a": 1}))\nprint(range(3))\nprint(range(1, 7, 2))\n') == "2\n1\n[0, 1, 2]\n[1, 3, 5]\n"
    assert error_of("print(len(3))\n").key == "bad_argument"


def test_print_options():
    assert run('print("a", 1, true, end: "")\nprint("!")\n') == "a 1 true!\n"


def test_builtin_argument_errors():
    assert error_of("print(round(1.5, digitz: 1))\n").key == "arg_unknown"
    assert error_of("print(len())\n").key == "arg_missing"


def test_input(monkeypatch):
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.StringIO("Ana\n"))
    assert run('name = input("Name: ")\nprint("Hi {name}")\n') == "Name: Hi Ana\n"
