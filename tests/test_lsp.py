"""`nexo lsp` (D87): a session against phase 0, and the compiler's server
answers like phase 0."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from lsp_client import Client, session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COMPILER = ROOT / "compiler"
PY = [sys.executable, "-m", "nexo.cli", "lsp"]

SAMPLE = '''## Adds two numbers.
fn add(a: int, b: int) -> int:
    return a + b

type Point:
    x: int
    y: int
    fn norm(self) -> int:
        return self.x * self.x + self.y * self.y

var total = add(1, 2)
p = Point(3, 4)
print(total, p.norm())
'''


@pytest.fixture(scope="module")
def answers():
    return session(PY, SAMPLE)


def test_capabilities(answers):
    caps = answers["initialize"]["capabilities"]
    assert caps["documentFormattingProvider"] and caps["hoverProvider"] and caps["definitionProvider"]
    assert answers["exit"] == 0


def test_diagnostics_come_with_their_fix(answers):
    [d] = answers["diagnostics"]
    assert d["code"] == "var_never_changed" and d["severity"] == 2
    assert d["range"] == {"start": {"line": 10, "character": 0}, "end": {"line": 10, "character": 3}}
    assert d["message"].startswith('"total" is declared with var but never changes.')
    kinds = [a["kind"] for a in answers["actions"]]
    assert kinds == ["quickfix", "source.fixAll.nexo"]
    edit = answers["actions"][0]["edit"]["changes"]["file:///work/app.nexo"][0]
    assert edit == {"range": {"start": {"line": 10, "character": 0}, "end": {"line": 10, "character": 4}},
                    "newText": ""}


def test_definition_and_hover(answers):
    # `add` in `var total = add(1, 2)`
    assert answers["definition"]["10:12"]["range"]["start"] == {"line": 1, "character": 3}
    hover = answers["hover"]["10:12"]["contents"]["value"]
    assert hover == "```nexo\nfn add(a: int, b: int) -> int\n```\n\nAdds two numbers."
    # the parameter `a` inside the body
    assert answers["definition"]["2:11"]["range"]["start"] == {"line": 1, "character": 7}
    # `.norm()` goes to the method
    assert answers["definition"]["12:15"]["range"]["start"] == {"line": 7, "character": 7}


def test_outline(answers):
    names = [(s["name"], s["kind"]) for s in answers["symbols"]]
    assert names == [("add", 12), ("Point", 23), ("total", 13), ("p", 14)]
    point = answers["symbols"][1]
    assert [c["name"] for c in point["children"]] == ["x", "y", "norm"]
    assert point["range"]["end"]["line"] == 8


def test_completion(answers):
    labels = [c["label"] for c in answers["completion"]]
    for word in ("add", "Point", "total", "print", "while", "PI"):
        assert word in labels
    assert labels == sorted(labels)


def test_formatting_and_errors():
    c = Client(PY)
    c.request("initialize", {"capabilities": {}, "locale": "es"})
    uri = "file:///x/broken.nexo"
    c.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "nexo", "version": 1,
                                                       "text": "x=[1,2]\nprint( x )\n"}})
    assert c.diagnostics(uri) == []
    edits = c.request("textDocument/formatting", {"textDocument": {"uri": uri}, "options": {}})["result"]
    assert edits[0]["newText"] == "x = [1, 2]\nprint(x)\n"
    c.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": 2},
                                        "contentChanges": [{"text": "try:\n    x = 1\nelse:\n    pass\n"}]})
    [d] = c.diagnostics(uri)
    assert d["severity"] == 1 and d["code"] == "else_needs_var"
    assert d["message"].startswith("else in a try needs a name")  # whatever the client's locale
    assert d["data"]["fixes"][0]["edits"][0]["newText"] == " e"
    assert c.request("textDocument/formatting", {"textDocument": {"uri": uri}, "options": {}})["result"] == []
    assert c.request("nexo/unknown", {})["error"]["code"] == -32601
    assert c.close() == 0


def strip(answers):
    answers["initialize"]["serverInfo"].pop("version")
    return answers


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is needed")
@pytest.mark.parametrize("source", sorted((ROOT / "examples").glob("*.nexo")) + sorted((ROOT / "tests" / "lint").glob("*.nexo")),
                         ids=lambda p: p.stem)
def test_the_compiler_answers_like_phase_0(source):
    subprocess.run(["cargo", "build", "--quiet"], cwd=COMPILER, check=True, timeout=1200)
    nexo = str(COMPILER / "target" / "debug" / "nexo")
    text = source.read_text(encoding="utf-8")
    assert strip(session([nexo, "lsp"], text)) == strip(session(PY, text))
