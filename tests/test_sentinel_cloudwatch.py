"""examples/sentinel/cloudwatch.nexo: the events of CloudWatch log groups
copied into files that sentinel.nexo scans. AWS is a local server that
answers FilterLogEvents and checks the signature of each request with
its own implementation of Signature Version 4."""
import hashlib
import hmac
import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from phases import ROOT, commands, run

SENTINEL = ROOT / "examples" / "sentinel"

# what runs of the programs write in examples/sentinel; a copy for a test leaves them out
GENERATED = ("audit.json", "audit.key", "audit.pub", "audit-archive", "report.json", "state.json", "blocks.json",
             "sentinel.lock.db", "findings.ocsf.jsonl", "alerts-*.json", "*-report.json", "*-report.md",
             "cloudwatch-state.json", "cloudwatch-*.log")
NGINX = (SENTINEL / "logs" / "nginx.log").read_text(encoding="utf-8").splitlines()
KEYS = {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "AWS_SESSION_TOKEN": ""}


def signature_of(handler, body, secret):
    """The signature AWS would compute for the request as it arrived."""
    sent = handler.headers["Authorization"]
    credential = sent.split("Credential=")[1].split(",")[0]
    names = sent.split("SignedHeaders=")[1].split(",")[0]
    _, day, region, service, _ = credential.split("/")
    headers = "".join(f"{name}:{handler.headers[name].strip()}\n" for name in names.split(";"))
    canonical = f"POST\n/\n\n{headers}\n{names}\n{hashlib.sha256(body).hexdigest()}"
    moment = handler.headers["X-Amz-Date"]
    to_sign = f"AWS4-HMAC-SHA256\n{moment}\n{day}/{region}/{service}/aws4_request\n" \
              f"{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = f"AWS4{secret}".encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest(), names, (day, region, service, moment)


class Aws(BaseHTTPRequestHandler):
    seen: list = []
    signed: list = []
    start = 0  # when the first event happened, set by each test: a minute before it starts

    def log_message(self, *a):
        pass

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        expected, names, scope = signature_of(self, body, KEYS["AWS_SECRET_ACCESS_KEY"])
        if self.headers["Authorization"].split("Signature=")[1] != expected:
            return self.answer(403, {"__type": "InvalidSignatureException"})
        asked = json.loads(body)
        Aws.seen.append((self.headers["X-Amz-Target"], asked))
        Aws.signed.append((names, scope, self.headers.get("X-Amz-Security-Token")))
        events = [{"timestamp": Aws.start + i, "message": line + "\n", "logStreamName": "web/1"}
                  for i, line in enumerate(NGINX) if Aws.start + i >= asked["startTime"]]
        offset = int(asked.get("nextToken", 0))
        answer = {"events": events[offset:offset + 4]}
        if offset + 4 < len(events):
            answer["nextToken"] = str(offset + 4)
        self.answer(200, answer)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Aws)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def work(tmp_path, server):
    Aws.seen.clear()
    Aws.signed.clear()
    Aws.start = int(time.time() * 1000) - 60000
    folder = tmp_path / "sentinel"
    shutil.copytree(SENTINEL, folder, ignore=shutil.ignore_patterns(*GENERATED))
    configure(folder, endpoint=server)
    return folder


def configure(folder, **changes):
    settings = json.loads((folder / "cloudwatch.json").read_text(encoding="utf-8"))
    settings.update(changes)
    (folder / "cloudwatch.json").write_text(json.dumps(settings), encoding="utf-8")


def collect(prefix, folder, **keys):
    proc = run(prefix, ["cloudwatch.nexo"], folder, env={**KEYS, **keys})
    return proc.returncode, proc.stdout.decode("utf-8")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_events_become_a_file_sentinel_scans(phase, work, server):
    code, out = collect(phase[1], work)
    assert (code, out) == (0, "NEXO SENTINEL - CloudWatch logs of eu-west-1\n"
                              "  web             9 new lines in logs/cloudwatch-web.log\n")
    assert (work / "logs" / "cloudwatch-web.log").read_text(encoding="utf-8").splitlines() == NGINX
    # three pages, each signed for the service and the region, with the token of the page before
    assert [target for target, _ in Aws.seen] == ["Logs_20140328.FilterLogEvents"] * 3
    assert [asked.get("nextToken") for _, asked in Aws.seen] == [None, "4", "8"]
    first = Aws.seen[0][1]
    assert first["logGroupName"] == "/ecs/web/nginx" and first["limit"] == 1000 and "filterPattern" not in first
    assert abs(first["startTime"] - (time.time() * 1000 - 900000)) < 120000
    names, (day, region, service, moment), token = Aws.signed[0]
    assert names == "content-type;host;x-amz-date;x-amz-target" and token is None
    assert (region, service) == ("eu-west-1", "logs") and moment.startswith(day) and len(moment) == 16
    assert json.loads((work / "cloudwatch-state.json").read_text(encoding="utf-8")) == {"web": Aws.start + 8}
    # nothing new: the next run asks from the last event on and leaves the file as it is
    code, out = collect(phase[1], work)
    assert "  web             0 new lines" in out and Aws.seen[-1][1]["startTime"] == Aws.start + 9
    assert (work / "logs" / "cloudwatch-web.log").read_text(encoding="utf-8").splitlines() == NGINX
    # sentinel.nexo finds in that file what it finds in nginx.log
    settings = json.loads((work / "sentinel.json").read_text(encoding="utf-8"))
    settings.update(logs=["logs/cloudwatch-web.log"], suppress=[])
    (work / "sentinel.json").write_text(json.dumps(settings), encoding="utf-8")
    proc = run(phase[1], ["sentinel.nexo"], work, stdin=b"no\nno\n")
    assert proc.returncode == 0
    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    assert [(f["line"], f["rule"]) for f in report["findings"]] == [
        (4, "SEN-107"), (5, "SEN-108"), (6, "SEN-205"), (7, "SEN-109"), (9, "SEN-106")]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_session_token_and_a_filter_are_sent(phase, work):
    configure(work, groups=[{"name": "web", "group": "/ecs/web/nginx", "filter": '"GET"'}])
    code, out = collect(phase[1], work, AWS_SESSION_TOKEN="t0ken/with+signs==")
    assert code == 0 and "9 new lines" in out and "t0ken" not in out
    names, _, token = Aws.signed[0]
    assert names == "content-type;host;x-amz-date;x-amz-security-token;x-amz-target"
    assert token == "t0ken/with+signs==" and Aws.seen[0][1]["filterPattern"] == '"GET"'


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_wrong_keys_and_missing_keys_read_nothing(phase, work):
    code, out = collect(phase[1], work, AWS_SECRET_ACCESS_KEY="another")
    assert code == 0 and "  web             not read: " in out and "403" in out
    assert Aws.seen == [] and not (work / "logs" / "cloudwatch-web.log").exists()
    code, out = collect(phase[1], work, AWS_ACCESS_KEY_ID="")
    assert "not read: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are not set" in out
    assert "another" not in out and KEYS["AWS_SECRET_ACCESS_KEY"] not in out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_pages_are_limited_and_the_rest_waits(phase, work):
    configure(work, max_pages=1, groups=[{"name": "web", "group": "/ecs/web/nginx"},
                                         {"name": "Not Plain", "group": "/x"}])
    code, out = collect(phase[1], work)
    assert "  web             4 new lines in logs/cloudwatch-web.log (more remain" in out
    assert "Left out a group without a plain name or a log group: Not Plain" in out
    code, out = collect(phase[1], work)
    assert (work / "logs" / "cloudwatch-web.log").read_text(encoding="utf-8").splitlines() == NGINX[:8]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
@pytest.mark.parametrize("change", [{"region": "eu west"}, {"window": "soon"}, {"max_pages": 0},
                                    {"endpoint": "logs.example.com"}])
def test_wrong_settings(phase, work, change):
    configure(work, **change)
    code, out = collect(phase[1], work)
    assert Aws.seen == [] and ("Sentinel stopped: " in out or "not read: endpoint" in out)


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_its_tests_pass_and_it_is_formatted(phase, work):
    proc = run(phase[1], ["test", "cloudwatch.nexo"], work, env=KEYS)
    assert proc.returncode == 0 and "3 tests: 3 passed, 0 failed" in proc.stdout.decode("utf-8")
    before = (work / "cloudwatch.nexo").read_bytes()
    assert before.isascii()
    assert run(phase[1], ["fmt", "cloudwatch.nexo"], work).returncode == 0
    assert (work / "cloudwatch.nexo").read_bytes() == before
    assert run(phase[1], ["lint", "cloudwatch.nexo"], work).stdout.decode() == "No warnings in cloudwatch.nexo.\n"
