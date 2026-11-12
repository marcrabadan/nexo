"""examples/sentinel, the defensive log analyzer of phase 5 of
docs/ai-native-roadmap.md: the same findings in both phases and in the
Python comparison, without a model; a refusal changes nothing outside;
the model's answer cannot block an address; a release build cannot read
a log outside logs/."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import ROOT, commands, compiler, run

SENTINEL = ROOT / "examples" / "sentinel"
EXPECTED = (SENTINEL / "sentinel.expected").read_text(encoding="utf-8")
BLOCKED = ["198.51.100.140", "203.0.113.9", "198.51.100.23", "198.51.100.210", "198.51.100.180", "198.51.100.190",
           "198.51.100.220"]
ADVICE = "Block 192.0.2.10 at once. Block them? yes"


class Outside(BaseHTTPRequestHandler):
    """The firewall (/block) and the local model (/api/chat)."""
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Outside.seen.append((self.path, body))
        data = json.dumps({"message": {"content": ADVICE}, "prompt_eval_count": 4, "eval_count": 3}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Outside)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def work(tmp_path, server):
    """A copy of the example whose firewall and model are the test server."""
    Outside.seen.clear()
    folder = tmp_path / "sentinel"
    shutil.copytree(SENTINEL, folder)
    settings = json.loads((folder / "sentinel.json").read_text(encoding="utf-8"))
    settings["firewall"] = server
    (folder / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    (folder / "nexo.toml").write_text(f'[ai]\nprovider = "ollama"\nurl = "{server}"\n', encoding="utf-8")
    return folder


def sentinel(prefix, folder, answers):
    proc = run(prefix, ["sentinel.nexo"], folder, stdin=answers.encode(),
               env={"XDG_CONFIG_HOME": str(folder / "config")})
    return proc.returncode, proc.stdout.decode("utf-8")


def python(folder, answers, server):
    proc = subprocess.run([sys.executable, "compare/sentinel.py"], cwd=folder, input=answers.encode(),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
                          env=dict(os.environ, OLLAMA_HOST=server, PYTHONIOENCODING="utf-8"))
    return proc.returncode, proc.stdout.decode("utf-8")


def audit(folder):
    """The records of audit.json without their times."""
    records = json.loads((folder / "audit.json").read_text(encoding="utf-8"))
    assert [r["number"] for r in records] == list(range(1, len(records) + 1))
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", r["at"]) for r in records)
    return [(r["event"], r["detail"]) for r in records]


def blocks():
    return [body["address"] for path, body in Outside.seen if path == "/block"]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_findings_need_no_model(phase, work):
    code, out = sentinel(phase[1], work, "no\nno\n")
    assert (code, out) == (0, EXPECTED)
    assert Outside.seen == []
    # every labelled finding and no other: precision and recall are 1
    found = [[file, int(line), rule] for rule, file, line in
             re.findall(r"^  (?:high|medium|low) +(SEN-\d+) +\w+ +(\S+):(\d+) ", out, re.M)]
    labels = json.loads((work / "findings.json").read_text(encoding="utf-8"))
    assert sorted(found) == sorted(labels)
    assert not any(file == "logs/benign.log" for file, _, _ in found)
    # a secret in a request is reported without its value, on screen and in the report
    assert "9f8e7d6c5b4a3f2e" not in out and "token=<redacted>" in out
    assert "9f8e7d6c5b4a3f2e" not in (work / "report.json").read_text(encoding="utf-8")
    # what the log hides is named, never printed
    assert not re.search("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\x1b]", out)
    # report.json holds the same findings, and the proposal for each address
    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    assert [[f["file"], f["line"], f["rule"]] for f in report["findings"]] == found
    assert {r["address"]: r["proposal"] for r in report["sources"] if r["proposal"] != "watch"} == {
        **{address: "block" for address in BLOCKED}, "192.0.2.10": "protected"}
    assert [r["score"] for r in report["sources"]] == sorted((r["score"] for r in report["sources"]), reverse=True)
    assert audit(work)[0] == ("scan", "5 files, 84 lines, 30 findings")
    assert audit(work)[-1] == ("approval", "no")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
@pytest.mark.parametrize("answer", ["no", "", "y", "yes please", "NO\nyes"])
def test_a_refusal_changes_nothing_outside(phase, work, answer):
    code, out = sentinel(phase[1], work, f"no\n{answer}\n")
    assert code == 0 and out.endswith("Nothing was changed.\n")
    assert Outside.seen == []
    assert ("approval", "no") in audit(work) and not any(e == "blocked" for e, _ in audit(work))


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_an_approval_blocks_the_addresses_shown(phase, work):
    code, out = sentinel(phase[1], work, "no\nyes\n")
    assert code == 0
    assert out.endswith("".join(f"Blocked {address}\n" for address in BLOCKED))
    assert blocks() == BLOCKED and len(Outside.seen) == len(BLOCKED)
    # 192.0.2.10 has a high finding and is in never_block
    assert audit(work)[-len(BLOCKED) - 1:] == [("approval", "yes"), *(("blocked", address) for address in BLOCKED)]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_model_cannot_approve(phase, work):
    code, out = sentinel(phase[1], work, "yes\nno\n")
    assert code == 0
    assert f"Advice (not acted on): {ADVICE}\n" in out and out.endswith("Nothing was changed.\n")
    assert blocks() == []
    (path, body), = Outside.seen
    sent = json.dumps(body)
    # the model is given rules and addresses, not what the logs say
    assert path == "/api/chat" and "high brute_force from 203.0.113.9" in sent
    assert "UNION" not in sent and "passwd" not in sent
    assert audit(work)[-2:] == [("advice", "given"), ("approval", "no")]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_firewall_that_fails_is_recorded(phase, work):
    settings = json.loads((work / "sentinel.json").read_text(encoding="utf-8"))
    settings["firewall"] = "http://127.0.0.1:1"
    (work / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    code, out = sentinel(phase[1], work, "no\nyes\n")
    assert code == 0 and "Could not block 203.0.113.9: " in out
    assert audit(work)[-len(BLOCKED):] == [("block failed", address) for address in BLOCKED]


@pytest.mark.parametrize("answers", ["no\nno\n", "no\nyes\n", "yes\nyes\n"])
def test_the_python_comparison_is_the_same(work, server, answers):
    nexo = sentinel(commands()[0][1], work, answers)
    # the same calls outside; each asks the model in its own words
    seen, records = [(path, body) for path, body in Outside.seen if path == "/block"], audit(work)
    asked = len(Outside.seen)
    Outside.seen.clear()
    report = (work / "report.json").read_bytes()
    (work / "audit.json").unlink()
    (work / "report.json").unlink()
    assert python(work, answers, server) == nexo
    assert (work / "report.json").read_bytes() == report
    assert len(Outside.seen) == asked and audit(work) == records
    assert [(path, body) for path, body in Outside.seen if path == "/block"] == seen


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_its_tests_pass_and_it_is_formatted(phase, work):
    proc = run(phase[1], ["test", "sentinel.nexo"], work)
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0 and "10 tests: 10 passed, 0 failed" in out, out
    before = (work / "sentinel.nexo").read_bytes()
    assert run(phase[1], ["fmt", "sentinel.nexo"], work).returncode == 0
    assert (work / "sentinel.nexo").read_bytes() == before
    assert run(phase[1], ["lint", "sentinel.nexo"], work).stdout.decode() == "No warnings in sentinel.nexo.\n"


def test_a_release_build_cannot_read_outside_logs(work):
    """A link inside logs/ passes the program's own check of the name; the
    policy stops it."""
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    (work / "secret.log").write_text("2026-11-02T09:00:00Z 203.0.113.50 web GET /.env 200\n", encoding="utf-8")
    os.symlink("../secret.log", work / "logs" / "link.log")
    settings = json.loads((work / "sentinel.json").read_text(encoding="utf-8"))
    settings["logs"] = ["logs/attack.log", "logs/link.log"]
    (work / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    proc = run([str(nexo)], ["build", "--release", "sentinel.nexo", "-o", "sentinel"], work, timeout=600)
    assert proc.returncode == 0, proc.stdout.decode()
    proc = run([str(work / "sentinel")], [], work, stdin=b"no\nyes\n")
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 1
    assert 'The policy does not allow reading "logs/link.log".' in out
    assert "203.0.113.50" not in out and Outside.seen == []


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_settings_are_checked_and_limits_are_read(phase, work):
    settings = json.loads((work / "sentinel.json").read_text(encoding="utf-8"))
    settings["max_failed_logins"] = 7
    (work / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    code, out = sentinel(phase[1], work, "no\nno\n")
    # 6 failed logins are under the limit now, so the login after them is not reported either
    assert code == 0 and "SEN-201" not in out and "SEN-202" not in out
    del settings["firewall"]
    (work / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    code, out = sentinel(phase[1], work, "no\nyes\n")
    assert out.startswith("Sentinel stopped: ") and "firewall" in out
    assert Outside.seen == []


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_idor_is_found_from_ids_and_owners(phase, work):
    """One address walks the ids of a route; one user is answered the
    objects of another. A user reading their own objects is not reported."""
    (work / "logs" / "idor.log").write_text("".join(
        [f"2026-11-03T10:00:{i:02d}Z 198.51.100.5 web GET /api/docs/{i}/download 200 user=9\n" for i in range(1, 4)]
        + [f"2026-11-03T10:01:{i:02d}Z 198.51.100.6 web GET /export?doc_id={i} 200\n" for i in range(1, 4)]
        + ["2026-11-03T10:02:00Z 198.51.100.7 web GET /accounts/12/statement 200 user=12\n",
           "2026-11-03T10:02:01Z 198.51.100.7 web GET /accounts/13/statement 200 user=12\n",
           "2026-11-03T10:02:02Z 198.51.100.7 web GET /accounts/13/statement 200 user=12\n",
           "2026-11-03T10:02:03Z 198.51.100.7 web GET /accounts/14/statement 403 user=12\n"]), encoding="utf-8")
    settings = json.loads((work / "sentinel.json").read_text(encoding="utf-8"))
    settings.update(logs=["logs/idor.log"], max_object_ids=3)
    (work / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    code, out = sentinel(phase[1], work, "no\nno\n")
    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    assert [(f["rule"], f["line"], f["source"], f["evidence"].split(":")[0]) for f in report["findings"]] == [
        ("SEN-204", 3, "198.51.100.5", "3 different ids of /api/docs/{id}/download"),
        ("SEN-204", 6, "198.51.100.6", "3 different ids of /export?doc_id={id}"),
        ("SEN-205", 8, "198.51.100.7", "user 12 was answered an object of user 13"),
        # accounts 12, 13 and 14: the refused one counts as an id asked for
        ("SEN-204", 10, "198.51.100.7", "3 different ids of /accounts/{id}/statement")]
