"""`nexo mcp` (section 26, D146): the same answers from both phases, and
nothing that changes the infrastructure or leaves the project."""
import json

import pytest

from phases import commands, run

PHASES = commands()
IDS = [c[0] for c in PHASES]

APP = '''fn add(a: int, b: int) -> int:
    return a+b

test "add":
    assert add(2, 3) == 5
'''

BAD = '''fn load(path: str) -> str!:
    return read(path)?

fn main():
    load("x.txt")
'''


def session(phase, cwd, requests):
    lines = "".join(json.dumps(r) + "\n" for r in requests) + "not json\n"
    proc = run(phase[1], ["mcp"], cwd, stdin=lines.encode())
    assert proc.returncode == 0
    return proc.stdout.decode(), [json.loads(line) for line in proc.stdout.decode().splitlines()]


def call(n, tool, **args):
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": tool, "arguments": args}}


REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    call(3, "check", path="bad.nexo"),
    call(4, "run_tests", path="app.nexo"),
    call(5, "format", path="app.nexo"),
    call(6, "docs_for_ai", path="app.nexo"),
    call(7, "check", path="../outside.nexo"),
    call(8, "apply"),
    call(9, "format"),
    call(10, "check", file="app.nexo"),
    {"jsonrpc": "2.0", "id": "last", "method": "ping"},
]


@pytest.fixture(scope="module")
def answers(tmp_path_factory):
    out = {}
    for phase in PHASES:
        work = tmp_path_factory.mktemp(phase[0])
        (work / "app.nexo").write_text(APP)
        (work / "bad.nexo").write_text(BAD)
        out[phase[0]] = session(phase, work, REQUESTS) + ((work / "app.nexo").read_text(),)
    return out


def test_both_phases_answer_the_same(answers):
    texts = {k: v[0] for k, v in answers.items()}
    assert len(set(texts.values())) == 1, texts


@pytest.mark.parametrize("phase", IDS)
def test_tools(answers, phase):
    _, replies, formatted = answers[phase]
    by_id = {r["id"]: r for r in replies}
    assert by_id[1]["result"]["capabilities"] == {"tools": {}}
    names = [t["name"] for t in by_id[2]["result"]["tools"]]
    assert names == ["check", "run_tests", "docs_for_ai", "plan", "format"]
    assert by_id[2]["result"]["tools"][4]["inputSchema"]["required"] == ["path"]
    check = by_id[3]["result"]
    assert check["isError"] and 'The call to "load" can fail and its error is ignored.' in check["content"][0]["text"]
    assert by_id[4]["result"] == {"content": [{"type": "text", "text": "✓ add\n1 test: 1 passed, 0 failed\n"}],
                                  "isError": False}
    assert not by_id[5]["result"]["isError"] and formatted == APP.replace("a+b", "a + b")
    assert "## This program (app.nexo)\nfn add(a: int, b: int) -> int\n" in by_id[6]["result"]["content"][0]["text"]
    assert by_id[7]["result"]["isError"]
    assert by_id[7]["result"]["content"][0]["text"].startswith('The path "../outside.nexo" is outside the project.')
    assert by_id[8]["error"]["code"] == -32602
    assert by_id[9]["result"]["content"][0]["text"].startswith('The tool "format" needs the argument "path".')
    assert by_id[10]["result"]["content"][0]["text"].startswith('The tool "check" has no argument "file".')
    assert by_id["last"]["result"] == {}
    assert replies[-1] == {"jsonrpc": "2.0", "id": None, "error": {
        "code": -32700, "message": "The message is not a JSON-RPC request. Send one JSON object per line."}}
    # the notification has no answer
    assert len(replies) == len(REQUESTS)


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_a_link_out_of_the_project_is_outside(phase, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "secret.nexo").write_text("print(1)\n")
    (work / "link.nexo").symlink_to(tmp_path / "secret.nexo")
    _, replies = session(phase, work, [call(1, "check", path="link.nexo"), call(2, "check", path="/etc/passwd")])
    for r in replies[:2]:
        assert r["result"]["isError"] and "is outside the project" in r["result"]["content"][0]["text"]
