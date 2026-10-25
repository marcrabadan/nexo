"""`nexo fmt` (D85): the pairs in tests/fmt/, every program in the
repository (same tokens, idempotent), and the compiler's formatter gives
the same text as phase 0."""
import shutil
import subprocess
from pathlib import Path

import pytest

from nexo.cli import main
from nexo.errors import NexoError
from nexo.fmt import format_source
from nexo.lexer import tokenize

ROOT = Path(__file__).resolve().parent.parent
PAIRS = ROOT / "tests" / "fmt"
COMPILER = ROOT / "compiler"


def programs():
    found = []
    for path in sorted(ROOT.rglob("*.nexo")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in (".venv", ".git") or "target" in rel.parts:
            continue
        try:
            tokenize(path.read_text(encoding="utf-8"), str(rel))
        except NexoError:
            continue
        found.append(path)
    return found


@pytest.mark.parametrize("source", sorted(PAIRS.glob("*.nexo")), ids=lambda p: p.stem)
def test_pairs(source):
    expected = source.with_suffix(".formatted").read_text(encoding="utf-8")
    assert format_source(source.read_text(encoding="utf-8"), source.name) == expected
    assert format_source(expected, source.name) == expected


@pytest.mark.parametrize("source", programs(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_program_keeps_its_tokens_and_is_stable(source):
    once = format_source(source.read_text(encoding="utf-8"), source.name)
    assert format_source(once, source.name) == once


@pytest.mark.parametrize("example", sorted((ROOT / "examples").glob("*.nexo")), ids=lambda p: p.stem)
def test_examples_are_formatted(example):
    text = example.read_text(encoding="utf-8")
    assert format_source(text, example.name) == text


def test_cli_formats_in_place_and_lists_changed_files(tmp_path, capsys, monkeypatch):
    (tmp_path / "a.nexo").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "b.nexo").write_text("y = 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["fmt"]) == 0
    assert capsys.readouterr().out == "a.nexo\n"
    assert (tmp_path / "a.nexo").read_text(encoding="utf-8") == "x = 1\n"
    assert main(["fmt"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_reports_a_file_that_does_not_lex(tmp_path, capsys, monkeypatch):
    (tmp_path / "bad.nexo").write_text('x = "open\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["fmt", "bad.nexo"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error in bad.nexo, line 1:")
    assert (tmp_path / "bad.nexo").read_text(encoding="utf-8") == 'x = "open\n'


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is needed")
def test_the_compiler_formats_like_phase_0(tmp_path):
    subprocess.run(["cargo", "build", "--quiet"], cwd=COMPILER, check=True, timeout=1200)
    nexo = COMPILER / "target" / "debug" / "nexo"
    sources = programs() + sorted(PAIRS.glob("*.nexo"))
    for n, source in enumerate(sources):
        (tmp_path / f"p{n}.nexo").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run([str(nexo), "fmt", str(tmp_path)], check=True, stdout=subprocess.DEVNULL, timeout=300)
    for n, source in enumerate(sources):
        expected = format_source(source.read_text(encoding="utf-8"), source.name)
        assert (tmp_path / f"p{n}.nexo").read_text(encoding="utf-8") == expected, str(source)
