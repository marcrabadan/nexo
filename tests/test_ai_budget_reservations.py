"""Atomic per-process provider budget admission, with real local mock RPCs."""
import json
import os
import subprocess
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import ROOT, commands, run


@pytest.fixture(params=commands(), ids=lambda c: c[0])
def backend(request):
    return request.param[1]


@pytest.fixture
def model(tmp_path):
    state = {"provider": "openai", "usage": [3, 2]}
    seen = []
    gate = threading.Event()
    gate.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            ordinal = len(seen)
            gate.wait(20)
            provider = state["provider"]
            stream = body.get("stream") or "streamGenerateContent" in self.path or "converse-stream" in self.path
            pair = state.get("usage")
            names = {"openai": ("prompt_tokens", "completion_tokens"), "anthropic": ("input_tokens", "output_tokens"),
                     "gemini": ("promptTokenCount", "candidatesTokenCount"), "bedrock": ("inputTokens", "outputTokens")}[provider]
            usage = dict(zip(names, pair)) if pair is not None else None
            used = any(m.get("role") == "tool" for m in body.get("messages", []))
            call = {"id": "call-1", "type": "function", "function": {"name": "report", "arguments": "{}"}}
            if provider == "openai":
                message = {"role": "assistant", "content": "ok"}
                if body.get("tools") and not used:
                    message.update(content="", tool_calls=[call])
                answer = {"choices": [{"message": message}]}
            elif provider == "anthropic":
                answer = {"content": [{"type": "text", "text": "ok"}]}
            elif provider == "gemini":
                answer = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
            else:
                answer = {"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}}}
            if usage is not None:
                answer["usageMetadata" if provider == "gemini" else "usage"] = usage
            if state.get("bad_first") and ordinal == 1:
                answer = {"usage": usage}
            if state.get("invalid_typed"):
                answer["choices"][0]["message"]["content"] = '{"score":99}'
            content_type = "application/json"
            payload = state.get("raw", json.dumps(answer)).encode()
            if stream:
                content_type = "text/event-stream"
                if provider == "openai":
                    events = [{"choices": [{"delta": {"content": "part"}}]}, {"choices": [], "usage": usage}]
                elif provider == "anthropic":
                    events = [{"type": "message_start", "message": {"usage": {"input_tokens": pair[0]}}},
                              {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "part"}},
                              {"type": "message_delta", "usage": {"output_tokens": pair[1]}}]
                elif provider == "gemini":
                    events = [{"candidates": [{"content": {"parts": [{"text": "part"}]}}], "usageMetadata": usage}]
                else:
                    from test_ai_providers import frame
                    payload = frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "part"}})
                    payload += frame("metadata", {"usage": usage})
                    events = None
                    content_type = "application/vnd.amazon.eventstream"
                if events is not None:
                    payload = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()
            if state.get("write_fail"):
                path = tmp_path / "config/nexo/usage.csv"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
                path.chmod(0o444)
            try:
                self.send_response(state.get("status", 200))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state, seen, gate
    finally:
        gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def prepare(tmp_path, model, suffix, budget="1 EUR per day"):
    base, state, _, _ = model
    provider = state["provider"]
    (tmp_path / "nexo.toml").write_text(f'[ai.prices]\n"{provider}:mock" = ["100000 EUR", "100000 EUR"]\n')
    source = f'''ai:
    provider = "{provider}"
    model = "{provider}:mock"
    key = secret("NEXO_AI_KEY")
    url = "{base}"
    budget = {budget}
{suffix}
'''
    (tmp_path / "main.nexo").write_text(source)


def execute(tmp_path, backend):
    return run(backend, ["main.nexo"], tmp_path, env={"NEXO_AI_KEY": "local-test", "XDG_CONFIG_HOME": str(tmp_path / "config")})


def receipts(tmp_path):
    path = tmp_path / "config/nexo/usage.csv"
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "bedrock"])
def test_receipt_releases_unused_headroom(tmp_path, backend, model, provider):
    model[1]["provider"] = provider
    prepare(tmp_path, model, '''print(ask("one"))
print(ask("two"))
try:
    ask("three")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"ok\nok\nblocked\n"
    assert len(model[2]) == 2
    assert len(receipts(tmp_path)) == 2


@pytest.mark.parametrize("agent", [False, True])
def test_competing_calls_cannot_spend_the_same_headroom(tmp_path, backend, model, agent):
    model[3].clear()
    declaration = 'agent reviewer:\n    tools = []\n    max_steps = 1\n' if agent else ''
    call = 'reviewer.handle("work")' if agent else 'ask("work")'
    parameter = ', reviewer: any' if agent else ''
    argument = ', reviewer' if agent else ''
    prepare(tmp_path, model, f'''use nexo.sync
{declaration}
fn worker(done: channel[str]{parameter}):
    try:
        {call}
        done.send("ok")
    else e is AIError:
        done.send("blocked")

done = channel[str](size: 2)
spawn worker(done{argument})
spawn worker(done{argument})
print(done.receive())
print(done.receive())
''')
    env = dict(os.environ, PYTHONPATH=str(ROOT), NEXO_AI_KEY="local-test", XDG_CONFIG_HOME=str(tmp_path / "config"), NEXO_LEAKCHECK="1", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([*backend, "main.nexo"], cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    first = []
    ready = threading.Event()
    def reader():
        first.append(proc.stdout.readline())
        ready.set()
        first.append(proc.stdout.read())
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        assert ready.wait(30), "neither contender was refused while provider response was held"
        if first[0] != b"blocked\n":
            model[3].set()
            proc.wait(timeout=30)
            thread.join(timeout=5)
            pytest.fail(b"".join(first).decode())
        until = time.monotonic() + 10
        while not model[2] and time.monotonic() < until:
            time.sleep(0.01)
        assert len(model[2]) == 1
        model[3].set()
        proc.wait(timeout=30)
        thread.join(timeout=5)
        assert proc.returncode == 0, b"".join(first).decode()
        assert first == [b"blocked\n", b"ok\n"], first
        assert len(model[2]) == 1
    finally:
        model[3].set()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        thread.join(timeout=5)


@pytest.mark.parametrize("usage", [None, [True, 2], [-1, 2], ["3", 2], [3.0, 2], [9223372036854775808, 0]])
def test_unknown_usage_keeps_reservation(tmp_path, backend, model, usage):
    model[1]["usage"] = usage
    prepare(tmp_path, model, '''try:
    ask("first")
else e is AIError:
    print("unaccounted")
try:
    ask("second")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"unaccounted\nblocked\n"
    assert len(model[2]) == 1
    assert not receipts(tmp_path)


def test_uncertain_failure_is_not_retried_or_refunded(tmp_path, backend, model):
    model[1]["status"] = 503
    prepare(tmp_path, model, '''try:
    ask("first", retries: 3)
else e is HTTPError:
    print("failed")
try:
    ask("second")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"failed\nblocked\n"
    assert len(model[2]) == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "bedrock"])
def test_stream_settles_once_on_complete_consumption(tmp_path, backend, model, provider):
    model[1]["provider"] = provider
    prepare(tmp_path, model, '''answer = ask("stream", stream: true)
for chunk in answer:
    print(chunk)
for chunk in answer:
    assert chunk == "part"
print(ask("next"))
try:
    ask("over")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"part\nok\nblocked\n"
    assert len(model[2]) == 2
    assert len(receipts(tmp_path)) == 2


def test_abandoned_stream_is_not_refunded(tmp_path, backend, model):
    prepare(tmp_path, model, '''fn first_piece() -> str!:
    for chunk in ask("stream", stream: true)?:
        return chunk
    return "empty"
print(first_piece())
try:
    ask("next")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"part\nblocked\n"
    assert len(model[2]) == 1
    assert not receipts(tmp_path)


def test_usage_is_charged_even_if_answer_is_malformed(tmp_path, backend, model):
    model[1]["bad_first"] = True
    prepare(tmp_path, model, '''try:
    ask("bad")
else e is AIError:
    print("bad answer")
print(ask("next"))
try:
    ask("over")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"bad answer\nok\nblocked\n"
    assert len(model[2]) == 2
    assert len(receipts(tmp_path)) == 2


def test_failed_receipt_write_retains_known_cost(tmp_path, backend, model):
    model[1]["write_fail"] = True
    prepare(tmp_path, model, '''print(ask("one"))
print(ask("two"))
try:
    ask("over")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"ok\nok\nblocked\n"
    assert len(model[2]) == 2
    assert not receipts(tmp_path)


@pytest.mark.parametrize("history", ["broken\n", "{day},openai:mock,-1,2\n", "{day},openai:unknown,3,2\n", "not-a-date,openai:mock,3,2\n",
                                      "{day},openai:mock,3,2\u2028{day},openai:mock,3,2\n"])
def test_unaccountable_history_fails_before_request(tmp_path, backend, model, history):
    path = tmp_path / "config/nexo/usage.csv"
    path.parent.mkdir(parents=True)
    path.write_text(history.format(day=date.today().isoformat()))
    prepare(tmp_path, model, '''try:
    ask("first")
else e is AIError:
    print("history refused")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"history refused\n"
    assert not model[2]


def test_typed_repairs_are_admitted_individually(tmp_path, backend, model):
    model[1]["invalid_typed"] = True
    prepare(tmp_path, model, '''type Finding:
    score: 1..5
try:
    ask("repair", as: Finding, retries: 3)
else e is AIError:
    print("repair budget exhausted")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"repair budget exhausted\n"
    assert len(model[2]) == 2
    assert len(receipts(tmp_path)) == 2


def test_agent_turns_recheck_budget_before_provider(tmp_path, backend, model):
    prepare(tmp_path, model, '''@tool "Writes a marker"
fn report() -> str!:
    write("marker", "done")?
    return "done"
agent reviewer:
    tools = [report]
    max_steps = 3
print(reviewer.handle("first"))
try:
    reviewer.handle("over")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"ok\nblocked\n"
    assert len(model[2]) == 2
    assert len(receipts(tmp_path)) == 2
    assert (tmp_path / "marker").read_text() == "done"


def test_unknown_agent_usage_prevents_tool_dispatch(tmp_path, backend, model):
    model[1]["usage"] = None
    prepare(tmp_path, model, '''@tool "Must not run without accounted usage"
fn report() -> str!:
    write("marker", "bad")?
    return "done"
agent reviewer:
    tools = [report]
try:
    reviewer.handle("first")
else e is AIError:
    print("unaccounted")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"unaccounted\n"
    assert len(model[2]) == 1
    assert not (tmp_path / "marker").exists()


def test_explicit_zero_usage_is_reconciled(tmp_path, backend, model):
    model[1]["usage"] = [0, 0]
    prepare(tmp_path, model, 'print(ask("one"))\nprint(ask("two"))')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"ok\nok\n"
    assert len(model[2]) == 2
    assert all(row.endswith(',0,0') for row in receipts(tmp_path))


def test_reported_cost_above_hold_is_not_clamped(tmp_path, backend, model):
    model[1]["usage"] = [10, 5]
    prepare(tmp_path, model, '''print(ask("one"))
try:
    ask("over")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"ok\nblocked\n"
    assert len(model[2]) == 1
    assert receipts(tmp_path)[0].endswith(',10,5')


def test_shield_still_rejects_usage_persistence_before_admission(tmp_path, backend, model):
    prepare(tmp_path, model, 'print(ask("first"))')
    path = tmp_path / "main.nexo"
    path.write_text('policy:\n    allow env "NEXO_AI_KEY"\n    allow net "127.0.0.1"\n' + path.read_text())
    result = run(backend, ['security', 'run', 'main.nexo'], tmp_path,
                 env={'NEXO_AI_KEY': 'local-test', 'XDG_CONFIG_HOME': str(tmp_path / 'config')})
    assert result.returncode == 1, result.stdout.decode()
    assert not model[2]
    assert not (tmp_path / 'config').exists()


def test_duplicate_usage_fields_are_not_trusted(tmp_path, backend, model):
    model[1]["raw"] = '{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":3,"prompt_tokens":0,"completion_tokens":2}}'
    prepare(tmp_path, model, '''try:
    ask("first")
else e is AIError:
    print("unaccounted")
try:
    ask("next")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"unaccounted\nblocked\n"
    assert len(model[2]) == 1
    assert not receipts(tmp_path)


def test_stream_missing_final_usage_retains_hold(tmp_path, backend, model):
    model[1]['usage'] = None
    prepare(tmp_path, model, '''try:
    for chunk in ask("stream", stream: true):
        print(chunk)
else e is AIError:
    print("unaccounted")
try:
    ask("next")
else e is AIError:
    print("blocked")''')
    result = execute(tmp_path, backend)
    assert result.returncode == 0, result.stdout.decode()
    assert result.stdout == b"part\nunaccounted\nblocked\n"
    assert len(model[2]) == 1
    assert not receipts(tmp_path)
