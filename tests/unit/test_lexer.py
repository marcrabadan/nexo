import pytest

from nexo.errors import NexoError
from nexo.lexer import Interp, tokenize


def kinds(src):
    return [(t.kind, t.value) for t in tokenize(src, "t.nexo")]


def test_simple_assignment():
    assert kinds("x = 1\n") == [("NAME", "x"), ("OP", "="), ("INT", "1"), ("NEWLINE", None), ("EOF", None)]


def test_keywords_and_unicode_identifiers():
    toks = kinds("var año_ñ = true\n")
    assert toks[:4] == [("KEYWORD", "var"), ("NAME", "año_ñ"), ("OP", "="), ("KEYWORD", "true")]


def test_indent_dedent():
    src = "if x:\n    y = 1\n    if z:\n        w\nv\n"
    ks = [k for k, _ in kinds(src)]
    assert ks.count("INDENT") == 2
    assert ks.count("DEDENT") == 2
    assert ks.index("DEDENT") < ks.index("EOF")


def test_dedent_at_eof():
    ks = [k for k, _ in kinds("if x:\n    y")]
    assert ks[-3:] == ["NEWLINE", "DEDENT", "EOF"]


def test_blank_and_comment_lines_do_not_change_indentation():
    src = "if x:\n    a\n\n        # comment\n    b\n"
    ks = [k for k, _ in kinds(src)]
    assert ks.count("INDENT") == 1 and ks.count("DEDENT") == 1


def test_tab_in_indentation_is_an_error():
    with pytest.raises(NexoError) as e:
        tokenize("if x:\n\ty\n", "t.nexo")
    assert e.value.key == "indent_tab"
    assert e.value.span.line == 2


def test_indentation_must_be_four_spaces():
    with pytest.raises(NexoError) as e:
        tokenize("if x:\n  y\n", "t.nexo")
    assert e.value.key == "indent_size"
    assert e.value.args_ == {"n": 2, "expected": 4}
    with pytest.raises(NexoError):
        tokenize("if x:\n        y\n", "t.nexo")


def test_dedent_to_unknown_level():
    with pytest.raises(NexoError) as e:
        tokenize("if x:\n    if y:\n        z\n  w\n", "t.nexo")
    assert e.value.key in ("dedent_mismatch", "indent_size")


def test_newlines_ignored_inside_brackets():
    ks = [k for k, _ in kinds("x = [1,\n    2]\n")]
    assert ks.count("NEWLINE") == 1 and "INDENT" not in ks


def test_pipe_continuation_line():
    ks = kinds('read("a")\n  |> where x > 1\n  |> take 2\nprint(1)\n')
    assert [k for k, _ in ks].count("NEWLINE") == 2
    assert ("OP", "|>") in ks and "INDENT" not in [k for k, _ in ks]


def test_numbers():
    assert kinds("1_000 3.14 1e-3 2E5 1..10")[:7] == [
        ("INT", "1000"), ("FLOAT", "3.14"), ("FLOAT", "1e-3"), ("FLOAT", "2E5"),
        ("INT", "1"), ("OP", ".."), ("INT", "10"),
    ]


def test_bad_number():
    with pytest.raises(NexoError) as e:
        tokenize("x = 1__0\n", "t.nexo")
    assert e.value.key == "bad_number"


def test_unit_literals():
    toks = kinds("30s 10% 300ms 5 km 19.99 EUR 2 days 512 MB")
    units = [v for k, v in toks if k == "UNIT"]
    assert units == [
        ("30", False, "s"), ("10", False, "%"), ("300", False, "ms"), ("5", False, "km"),
        ("19.99", True, "EUR"), ("2", False, "days"), ("512", False, "MB"),
    ]


def test_percent_between_expressions_is_remainder():
    assert kinds("a % 3")[:3] == [("NAME", "a"), ("OP", "%"), ("INT", "3")]
    assert kinds("10 % 3")[:3] == [("INT", "10"), ("OP", "%"), ("INT", "3")]


def test_single_letter_units_must_be_attached():
    assert kinds("5 s")[:2] == [("INT", "5"), ("NAME", "s")]


def test_unknown_attached_unit():
    with pytest.raises(NexoError) as e:
        tokenize("x = 3xyz\n", "t.nexo")
    assert e.value.key == "unknown_unit"


def test_string_interpolation():
    (tok, *_rest) = tokenize('"Hello {name}, {a + 1}!"', "t.nexo")
    assert tok.kind == "STRING"
    parts = tok.value
    assert parts[0] == "Hello "
    assert isinstance(parts[1], Interp) and parts[1].source == "name"
    assert parts[1].col == 9
    assert parts[2] == ", "
    assert parts[3].source == "a + 1"
    assert parts[4] == "!"


def test_literal_brace_and_escapes():
    (tok, *_r) = tokenize(r'"{{x} \"q\" a\nb"', "t.nexo")
    assert tok.value == ['{x} "q" a\nb']


def test_interpolation_with_nested_string():
    (tok, *_r) = tokenize('"v: {m["k"]}"', "t.nexo")
    assert tok.value[1].source == 'm["k"]'


def test_multiline_text_is_dedented():
    src = 'text = """\n    Several lines\n    with {name}\n"""\nx = 1\n'
    toks = tokenize(src, "t.nexo")
    s = toks[2]
    assert s.value[0] == "Several lines\nwith "
    assert s.value[1].source == "name" and s.value[1].line == 3
    x = [t for t in toks if t.kind == "NAME" and t.value == "x"][0]
    assert x.span.line == 5


def test_unterminated_string():
    with pytest.raises(NexoError) as e:
        tokenize('x = "abc\n', "t.nexo")
    assert e.value.key == "unterminated_string"


def test_comments_and_doc_comments():
    assert kinds("## doc\nx # c\n") == [("NAME", "x"), ("NEWLINE", None), ("EOF", None)]


def test_operators():
    values = [v for k, v in kinds("a |> b -> c ?? d ** e // f != g?")[:-2]]
    assert values == ["a", "|>", "b", "->", "c", "??", "d", "**", "e", "//", "f", "!=", "g", "?"]


def test_unmatched_bracket():
    with pytest.raises(NexoError) as e:
        tokenize("x = (1]\n", "t.nexo")
    assert e.value.key == "unmatched_bracket"
    with pytest.raises(NexoError) as e:
        tokenize("x = (1\n", "t.nexo")
    assert e.value.key == "unclosed_bracket"


def test_spans():
    toks = tokenize("    \nprint(nmae)\n", "t.nexo")
    name = toks[2]
    assert (name.span.line, name.span.col, name.span.end_col) == (2, 7, 11)
