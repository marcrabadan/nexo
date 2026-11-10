"""examples/sentinel/restarts.nexo: the containers Prometheus says
restarted, explained with their lines in Loki. The two services are a
local server that answers their HTTP APIs."""
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from phases import ROOT, commands, run

SENTINEL = ROOT / "examples" / "sentinel"
EXPECTED = (SENTINEL / "restarts.expected").read_text(encoding="utf-8")

RESTARTS = [("shop", "api-7d9f8-x2k4p", "api", "3.02"), ("shop", "worker-5c6b7-q8z1m", "worker", "1"),
            ("shop", "cart-66d4c-h7n2v", "cart", "2"), ("data", "importer-0", "importer", "4.4"),
            ("data", "cache-0", "redis", "1"), ("shop", "mail-9b8a7-k3j5d", "mail", "1"),
            ("shop", 'x"} or {job="', "api", "1")]
REASONS = {"api-7d9f8-x2k4p": "OOMKilled", "worker-5c6b7-q8z1m": "Error", "importer-0": "Error",
           "cache-0": "Completed", "mail-9b8a7-k3j5d": "Error"}
CODES = {"api-7d9f8-x2k4p": "137", "worker-5c6b7-q8z1m": "2", "cart-66d4c-h7n2v": "1", "importer-0": "1",
         "cache-0": "0", "mail-9b8a7-k3j5d": "137"}
WAITING = {"importer-0": "CrashLoopBackOff"}
MEMORY = {"api-7d9f8-x2k4p": "99.6", "worker-5c6b7-q8z1m": "41.2", "mail-9b8a7-k3j5d": "93.5"}
# the newest line first, as Loki gives them with direction=backward
LOGS = {
    "api-7d9f8-x2k4p": ["GET /products 200 12ms", "listening on :8080"],
    "worker-5c6b7-q8z1m": ["goroutine 1 [running]:", "panic: runtime error: index out of range [3] with length 3",
                           "processing job 8812"],
    "cart-66d4c-h7n2v": ["exiting", "dial tcp 10.0.3.7:5432: connect: connection refused", "connecting to postgres"],
    "importer-0": ["fatal: missing required environment variable IMPORT_BUCKET", "starting importer"],
    "cache-0": ["Redis is now ready to exit, bye bye...", "Received SIGTERM scheduling shutdown..."],
    "mail-9b8a7-k3j5d": ["queue is empty\x1b[2J‮", "started"],
}


class Monitoring(BaseHTTPRequestHandler):
    """Prometheus (/api/v1/query) and Loki (/loki/api/v1/query_range)."""
    seen: list = []
    auth: list = []
    loki_down = False
    without: str = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        Monitoring.seen.append((url.path, query, self.headers.get("X-Scope-OrgID")))
        Monitoring.auth.append(self.headers.get("Authorization"))
        code = 200
        if url.path == "/api/v1/query":
            def keep(namespace):
                return 'namespace="' not in query["query"] or f'namespace="{namespace}"' in query["query"]
            def samples(values, label=None):
                return [{"metric": {"namespace": n, "pod": p, "container": c, **({"reason": values[p]} if label else {})},
                         "value": [1762768800, "1" if label else values[p]]}
                        for n, p, c, _ in RESTARTS if p in values and keep(n)]
            asked = query["query"]
            if "restarts_total" in asked:
                result = samples({p: v for _, p, _, v in RESTARTS})
            elif "last_terminated_reason" in asked:
                result = samples(REASONS, "reason")
            elif "last_terminated_exitcode" in asked:
                result = samples(CODES)
            elif "waiting_reason" in asked:
                result = samples(WAITING, "reason")
            elif asked.startswith("100 * max by") and "container_memory_working_set_bytes" in asked:
                result = samples(MEMORY)
            else:
                result = None
            if result is None or (Monitoring.without and Monitoring.without in asked):
                code, body = 422, {"status": "error", "error": "unknown metric"}
            else:
                body = {"status": "success", "data": {"resultType": "vector", "result": result}}
        elif url.path == "/loki/api/v1/query_range" and not Monitoring.loki_down:
            pod = query["query"].split('pod="')[1].split('"')[0]
            values = [[str(1762768800000000000 - i), line] for i, line in enumerate(LOGS.get(pod, []))]
            body = {"status": "success", "data": {"resultType": "streams",
                                                  "result": [{"stream": {"pod": pod}, "values": values}]}}
        else:
            code, body = 503, {"status": "error"}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Monitoring)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def work(tmp_path, server):
    Monitoring.seen.clear()
    Monitoring.auth.clear()
    Monitoring.loki_down = False
    Monitoring.without = ""
    folder = tmp_path / "sentinel"
    shutil.copytree(SENTINEL, folder)
    configure(folder, prometheus=server, loki=server)
    return folder


def configure(folder, **changes):
    settings = json.loads((folder / "restarts.json").read_text(encoding="utf-8"))
    settings.update(changes)
    (folder / "restarts.json").write_text(json.dumps(settings), encoding="utf-8")


def restarts(prefix, folder, env=None):
    proc = run(prefix, ["restarts.nexo"], folder, env=env)
    return proc.returncode, proc.stdout.decode("utf-8")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_each_restart_has_its_cause(phase, work):
    code, out = restarts(phase[1], work)
    assert (code, out) == (0, EXPECTED)
    report = json.loads((work / "restarts-report.json").read_text(encoding="utf-8"))
    assert [(r["severity"], r["pod"], r["restarts"], r["name"], r["proof"]) for r in report["restarts"]] == [
        ("critical", "importer-0", 4, "configuration", "logs"),
        ("high", "api-7d9f8-x2k4p", 3, "out_of_memory", "metrics"),
        ("high", "worker-5c6b7-q8z1m", 1, "crash", "logs"),
        ("high", "mail-9b8a7-k3j5d", 1, "out_of_memory", "metrics"),
        ("medium", "cart-66d4c-h7n2v", 2, "dependency", "logs"),
        ("low", "cache-0", 1, "stopped_by_platform", "logs")]
    worker = report["restarts"][2]
    assert (worker["exit_code"], worker["memory"], worker["state"]) == ("2", "41%", "running")
    # the lines around the evidence, the oldest first
    assert worker["context"] == ["processing job 8812", "panic: runtime error: index out of range [3] with length 3",
                                 "goroutine 1 [running]:"]
    # a name that would change the query is never sent to Loki
    asked = [q["query"] for path, q, _ in Monitoring.seen if path.startswith("/loki")]
    assert len(asked) == 6 and not any("job=" in q for q in asked)
    assert asked[0] == '{namespace="shop", pod="api-7d9f8-x2k4p", container="api"}'
    assert all(q["since"] == "1h" and q["limit"] == "200" and q["direction"] == "backward"
               for path, q, _ in Monitoring.seen if path.startswith("/loki"))
    assert "\x1b" not in out and "\u202e" not in out
    assert set(Monitoring.auth) == {None}
    page = (work / "restarts-report.md").read_text(encoding="utf-8")
    assert page.startswith("# Restarts of the last 1h\n\n6 containers restarted: 1 critical, 3 high, 1 medium, 1 low.\n")
    assert "| critical | `data/importer-0/importer` | 4 | CrashLoopBackOff | 1 | - | RST-106 configuration | logs |" in page
    assert "```\nprocessing job 8812\npanic: runtime error: index out of range [3] with length 3\n" in page
    assert "\x1b" not in page and "\u202e" not in page


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_authorization_goes_to_both(phase, work):
    configure(work, auth=True)
    code, out = restarts(phase[1], work, env={"SENTINEL_AUTH": "Bearer t0ken"})
    assert code == 0 and "t0ken" not in out
    assert set(Monitoring.auth) == {"Bearer t0ken"} and len(Monitoring.auth) == 11
    Monitoring.seen.clear()
    code, out = restarts(phase[1], work)
    assert out.startswith("Sentinel stopped: ") and "SENTINEL_AUTH" in out and Monitoring.seen == []


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_without_the_memory_metrics_the_rest_stands(phase, work):
    Monitoring.without = "container_memory_working_set_bytes"
    code, out = restarts(phase[1], work)
    assert code == 0 and out.startswith("Without the memory: ")
    report = json.loads((work / "restarts-report.json").read_text(encoding="utf-8"))
    assert {r["memory"] for r in report["restarts"]} == {"-"}
    # exit code 137 without its memory is a stop, not a lack of memory
    mail = [r for r in report["restarts"] if r["pod"].startswith("mail")][0]
    assert (mail["name"], mail["severity"]) == ("stopped_by_platform", "low")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_namespace_window_and_tenant(phase, work):
    configure(work, namespace="data", window="30m", tenant="team-a")
    code, out = restarts(phase[1], work)
    assert code == 0 and "restarts of the last 30m" in out and "2 containers restarted" in out
    queries = [q["query"] for path, q, _ in Monitoring.seen if path == "/api/v1/query"]
    assert queries[0] == 'increase(kube_pod_container_status_restarts_total{namespace="data"}[30m]) > 0'
    assert 'container_memory_working_set_bytes{namespace="data", container!=""}[30m]' in queries[4]
    assert {tenant for _, _, tenant in Monitoring.seen} == {"team-a"}


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_without_loki_the_metrics_still_explain(phase, work):
    Monitoring.loki_down = True
    code, out = restarts(phase[1], work)
    assert code == 0 and out.count("No logs of ") == 6
    report = json.loads((work / "restarts-report.json").read_text(encoding="utf-8"))
    assert {r["pod"]: r["name"] for r in report["restarts"]} == {
        "importer-0": "unknown", "api-7d9f8-x2k4p": "out_of_memory", "mail-9b8a7-k3j5d": "out_of_memory",
        "worker-5c6b7-q8z1m": "unknown", "cart-66d4c-h7n2v": "unknown", "cache-0": "unknown"}


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
@pytest.mark.parametrize("change", [{"window": "1h]) or vector(1"}, {"namespace": 'a"}'}, {"lines": "many"},
                                    {"lines": 0}])
def test_wrong_settings_send_nothing(phase, work, change):
    configure(work, **change)
    code, out = restarts(phase[1], work)
    assert out.startswith("Sentinel stopped: ") and Monitoring.seen == []
    assert not (work / "restarts-report.json").exists()


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_its_tests_pass_and_it_is_formatted(phase, work):
    proc = run(phase[1], ["test", "restarts.nexo"], work)
    assert proc.returncode == 0 and "6 tests: 6 passed, 0 failed" in proc.stdout.decode("utf-8")
    before = (work / "restarts.nexo").read_bytes()
    assert run(phase[1], ["fmt", "restarts.nexo"], work).returncode == 0
    assert (work / "restarts.nexo").read_bytes() == before
    assert run(phase[1], ["lint", "restarts.nexo"], work).stdout.decode() == "No warnings in restarts.nexo.\n"
