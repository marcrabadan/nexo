"""`nexo mcp` (section 26, D146): a Model Context Protocol server on
standard input and output. It offers an assistant the commands of the
project that only read or format it: `check`, `run_tests`, `docs_for_ai`,
`plan` and `format`. Each tool runs this same `nexo` on a file of the
project (the folder the server was started in) and answers with what the
command printed; `apply`, deployments and secrets are never offered."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import errors

VERSION = "2025-06-18"

PATH = {"type": "string", "description": "A .nexo file or folder of the project, relative to it (default: the project)."}
FILE = {"type": "string", "description": "A .nexo file of the project, relative to it."}
TOOLS = [
    ("check", "Compile without running: every error with its fix, or that the code is correct.", {"path": PATH}, []),
    ("run_tests", "Run the test blocks and report each one.", {"path": PATH}, []),
    ("docs_for_ai", "The exact list of what a program can use, and the declarations of a file.", {"path": FILE}, []),
    ("plan", "What nexo apply would change in the infrastructure, without applying it.",
     {"path": FILE, "stage": {"type": "string", "description": "The stage to plan (default: the first one)."}}, []),
    ("format", "Format a file with nexo fmt (the same tokens, the one layout).", {"path": FILE}, ["path"]),
]


def problem(key: str, **args) -> str:
    return errors.msg(key, **args) + " " + errors.msg("fix." + key, **args)


def tool_list() -> list:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False}}
            for n, d, props, req in TOOLS]


def inside(root: Path, path: str) -> bool:
    """A path of the project: relative, and not out of it through .. or a link."""
    if not path or os.path.isabs(path):
        return False
    try:
        return (root / path).resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def command(name: str, args: dict) -> list[str] | str:
    """The arguments of `nexo` for a tool, or the message of what is wrong."""
    known = {n: (props, req) for n, _, props, req in TOOLS}
    props, req = known[name]
    for key, value in args.items():
        if key not in props:
            return problem("mcp_argument", tool=name, name=key)
        if not isinstance(value, str):
            return problem("mcp_text", name=key)
    for key in req:
        if key not in args:
            return problem("mcp_missing", tool=name, name=key)
    path = args.get("path")
    if path is not None and not inside(Path.cwd(), path):
        return problem("mcp_outside", path=path)
    paths = [path] if path is not None else []
    if name == "check":
        return ["check", *paths]
    if name == "run_tests":
        return ["test", *paths]
    if name == "docs_for_ai":
        return ["docs", "--for-ai", *paths]
    if name == "plan":
        return ["plan", *(["--stage", args["stage"]] if "stage" in args else []), *paths]
    return ["fmt", *paths]


def call_tool(name: str, args: dict) -> dict:
    argv = command(name, args)
    if isinstance(argv, str):
        return {"content": [{"type": "text", "text": argv}], "isError": True}
    p = subprocess.run([sys.executable, "-m", "nexo.cli", *argv], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    text = p.stdout.decode("utf-8", errors="replace")
    return {"content": [{"type": "text", "text": text}], "isError": p.returncode != 0}


def answer(request: dict) -> dict | None:
    """The answer to one message, or None for a notification."""
    method = request.get("method")
    rid = request.get("id")
    if rid is None:
        return None
    params = request.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "nexo", "version": "0.1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments")
        args = {} if args is None else args
        if name not in {t[0] for t in TOOLS} or not isinstance(args, dict):
            return error(rid, -32602, problem("mcp_tool", name=str(name)))
        result = call_tool(name, args)
    else:
        return error(rid, -32601, problem("mcp_method", name=str(method)))
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve() -> int:
    """One JSON-RPC message per line, until standard input ends."""
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            out = error(None, -32700, problem("mcp_json"))
        else:
            out = answer(request) if isinstance(request, dict) else error(None, -32600, problem("mcp_json"))
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0
