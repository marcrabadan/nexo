"""The versioned check interface is identical in both implementations."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs/diagnostics-v1.schema.json").read_text())


def validate(value, schema):
    """Validate the subset used by our wire schema without a new dependency."""
    if "$ref" in schema:
        return validate(value, SCHEMA["$defs"][schema["$ref"].rsplit("/", 1)[1]])
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate(value, option)
                return
            except AssertionError:
                pass
        raise AssertionError(value)
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        assert type(value) in tuple(types[k] for k in kinds)
    if isinstance(value, dict):
        assert set(schema.get("required", [])) <= value.keys()
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert value.keys() <= props.keys()
        for key in value.keys() & props.keys():
            validate(value[key], props[key])
    if isinstance(value, list):
        assert len(value) >= schema.get("minItems", 0)
        for item in value:
            validate(item, schema.get("items", {}))
    if "minimum" in schema:
        assert value >= schema["minimum"]

NATIVE = ROOT / "compiler/target/debug/nexo"


@pytest.fixture(scope="module")
def commands():
    cmds = [[sys.executable, "-m", "nexo.cli"]]
    if shutil.which("cargo"):
        subprocess.run(["cargo", "build", "--locked"], cwd=ROOT / "compiler", check=True,
                       capture_output=True)
        cmds.append([str(NATIVE)])
    return cmds


def invoke(cmd, args, cwd):
    import os
    result = subprocess.run([*cmd, "check", *args], cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=30)
    doc = json.loads(result.stdout)
    validate(doc, SCHEMA)
    assert result.stderr == ""
    assert doc["schema_version"] == 1
    assert doc["position_encoding"] == "utf-16"
    assert doc["success"] == (result.returncode == 0)
    assert set(doc) == {"schema_version", "position_encoding", "success", "diagnostics"}
    for d in doc["diagnostics"]:
        assert set(d) == {"file", "range", "severity", "source", "code", "message", "data"}
        assert d["severity"] == 1 and d["source"] == "nexo"
        assert isinstance(d["data"]["fixes"], list)
    return result.returncode, doc


@pytest.mark.parametrize("source", [
    'print("ok")\n',
    'var unused = 1\n',  # check does not become lint
    'print(\n',
    'print("😀", @)\n',
    'fn f(x, x):\n    print(x)\n',
    'if true:\n  print(1)\n',
    '',
])
def test_cli_parity(commands, tmp_path, source):
    (tmp_path / "app.nexo").write_text(source)
    results = [invoke(cmd, ["--json", "app.nexo"], tmp_path) for cmd in commands]
    assert all(r == results[0] for r in results)


def test_io_usage_and_empty_directory(commands, tmp_path):
    (tmp_path / "invalid.nexo").write_bytes(b"\xff")
    for args, code, key in [
        (["--json", "missing.nexo"], 1, "check_io"),
        (["invalid.nexo", "--json"], 1, "check_io"),
        (["--json", "--intent"], 2, "check_usage"),
        (["--intent", "--json"], 2, "check_usage"),
        (["--json", "--json"], 2, "check_usage"),
        (["--json", "--bad"], 2, "check_usage"),
    ]:
        results = [invoke(cmd, args, tmp_path) for cmd in commands]
        assert all(r == results[0] for r in results)
        assert results[0][0] == code
        assert results[0][1]["diagnostics"][0]["code"] == key
    (tmp_path / "invalid.nexo").unlink()
    for cmd in commands:
        assert invoke(cmd, ["--json"], tmp_path)[1]["diagnostics"] == []


def test_multiple_files_and_utf16_fix(commands, tmp_path):
    (tmp_path / "a.nexo").write_text('fn f():\n    return read("😀")\n')
    (tmp_path / "b.nexo").write_text('print(\n')
    (tmp_path / ".hidden.nexo").write_text('print(\n')
    results = [invoke(cmd, ["--json", "."], tmp_path) for cmd in commands]
    assert all(r == results[0] for r in results)
    ds = results[0][1]["diagnostics"]
    assert [d["file"] for d in ds] == ["a.nexo", "b.nexo"]
    d = ds[0]
    assert d["code"] == "ignored_error"
    assert d["range"]["end"] == {"line": 1, "character": 21}
    fix = d["data"]["fixes"][0]
    assert fix["edits"][0]["newText"] == "?"
    (tmp_path / "a.nexo").write_text('fn f():\n    return read("😀")?\n')
    for cmd in commands:
        assert invoke(cmd, ["--json", "a.nexo"], tmp_path)[0] == 0


def test_imported_module_location(commands, tmp_path):
    (tmp_path / "lib.nexo").write_text('pub fn broken() -> str:\n    return read("😀")?\n')
    (tmp_path / "app.nexo").write_text('use lib\n')
    results = [invoke(cmd, ["--json", "app.nexo"], tmp_path) for cmd in commands]
    assert all(r == results[0] for r in results)
    assert results[0][1]["diagnostics"][0]["file"].endswith("lib.nexo")


def test_human_output_unchanged(commands, tmp_path):
    import os
    (tmp_path / "app.nexo").write_text('print("ok")\n')
    for cmd in commands:
        p = subprocess.run([*cmd, "check", "app.nexo"], cwd=tmp_path, capture_output=True,
                           text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
        assert p.returncode == 0 and "app.nexo" in p.stdout
        assert not p.stdout.startswith("{") and p.stderr == ""


def test_spanless_error_metadata():
    from nexo.errors import NexoError
    from nexo.lsp import error_diagnostic
    d = error_diagnostic(NexoError("policy_missing", None), "")
    assert d["code"] == "policy_missing" and d["data"]["fixes"] == []


def test_check_never_runs_source(commands, tmp_path):
    (tmp_path / "app.nexo").write_text('print("this must not reach stdout")\n')
    for cmd in commands:
        assert invoke(cmd, ["--json", "app.nexo"], tmp_path)[1]["diagnostics"] == []


def test_secret_literal_has_no_source_snippet(commands, tmp_path, monkeypatch):
    monkeypatch.setenv("NEXO_DIAGNOSTICS_TEST_SECRET", "env-value-must-stay-private")
    (tmp_path / "app.nexo").write_text(
        'fn connect_api(url, key):\n    return url\n'
        'print(connect_api("https://api.example.com", key: "sk-private-test-literal"))\n')
    for cmd in commands:
        code, doc = invoke(cmd, ["--json", "app.nexo"], tmp_path)
        assert code == 1
        assert doc["diagnostics"][0]["code"] == "secret_literal"
        serialized = json.dumps(doc)
        assert "sk-private-test-literal" not in serialized
        assert "env-value-must-stay-private" not in serialized


def test_explicit_dot_path_parity(commands, tmp_path):
    (tmp_path / "app.nexo").write_text('print(\n')
    results = [invoke(cmd, ["--json", "./app.nexo"], tmp_path) for cmd in commands]
    assert all(r == results[0] for r in results)
    assert results[0][1]["diagnostics"][0]["file"] == "app.nexo"
