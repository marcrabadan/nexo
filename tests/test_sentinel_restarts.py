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
    loki_down = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        Monitoring.seen.append((url.path, query, self.headers.get("X-Scope-OrgID")))
        code = 200
        if url.path == "/api/v1/query":
            def keep(namespace):
                return 'namespace="' not in query["query"] or f'namespace="{namespace}"' in query["query"]
            if "restarts_total" in query["query"]:
                result = [{"metric": {"namespace": n, "pod": p, "container": c}, "value": [1762768800, v]}
                          for n, p, c, v in RESTARTS if keep(n)]
            else:
                result = [{"metric": {"namespace": n, "pod": p, "container": c, "reason": REASONS[p]},
                           "value": [1762768800, "1"]} for n, p, c, _ in RESTARTS if p in REASONS and keep(n)]
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
    Monitoring.loki_down = False
    folder = tmp_path / "sentinel"
    shutil.copytree(SENTINEL, folder)
    configure(folder, prometheus=server, loki=server)
    return folder


def configure(folder, **changes):
    settings = json.loads((folder / "restarts.json").read_text(encoding="utf-8"))
    settings.update(changes)
    (folder / "restarts.json").write_text(json.dumps(settings), encoding="utf-8")


def restarts(prefix, folder):
    proc = run(prefix, ["restarts.nexo"], folder)
    return proc.returncode, proc.stdout.decode("utf-8")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_each_restart_has_its_cause(phase, work):
    code, out = restarts(phase[1], work)
    assert (code, out) == (0, EXPECTED)
    report = json.loads((work / "restarts-report.json").read_text(encoding="utf-8"))
    assert [(r["pod"], r["restarts"], r["name"], r["proof"]) for r in report["restarts"]] == [
        ("api-7d9f8-x2k4p", 3, "out_of_memory", "metrics"), ("worker-5c6b7-q8z1m", 1, "crash", "logs"),
        ("cart-66d4c-h7n2v", 2, "dependency", "logs"), ("importer-0", 4, "configuration", "logs"),
        ("cache-0", 1, "stopped_by_platform", "logs"), ("mail-9b8a7-k3j5d", 1, "unknown", "none")]
    # a name that would change the query is never sent to Loki
    asked = [q["query"] for path, q, _ in Monitoring.seen if path.startswith("/loki")]
    assert len(asked) == 6 and not any("job=" in q for q in asked)
    assert asked[0] == '{namespace="shop", pod="api-7d9f8-x2k4p", container="api"}'
    assert all(q["since"] == "1h" and q["limit"] == "200" and q["direction"] == "backward"
               for path, q, _ in Monitoring.seen if path.startswith("/loki"))
    assert "\x1b" not in out and "‮" not in out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_namespace_window_and_tenant(phase, work):
    configure(work, namespace="data", window="30m", tenant="team-a")
    code, out = restarts(phase[1], work)
    assert code == 0 and "restarts of the last 30m" in out and "Containers that restarted: 2" in out
    first = Monitoring.seen[0]
    assert first[1]["query"] == 'increase(kube_pod_container_status_restarts_total{namespace="data"}[30m]) > 0'
    assert [tenant for path, _, tenant in Monitoring.seen if path.startswith("/loki")] == ["team-a", "team-a"]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_without_loki_the_metrics_still_explain(phase, work):
    Monitoring.loki_down = True
    code, out = restarts(phase[1], work)
    assert code == 0 and out.count("No logs of ") == 6
    report = json.loads((work / "restarts-report.json").read_text(encoding="utf-8"))
    assert [r["name"] for r in report["restarts"]] == ["out_of_memory"] + ["unknown"] * 5


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
@pytest.mark.parametrize("change", [{"window": "1h]) or vector(1"}, {"namespace": 'a"}'}, {"lines": "many"}])
def test_wrong_settings_send_nothing(phase, work, change):
    configure(work, **change)
    code, out = restarts(phase[1], work)
    assert out.startswith("Sentinel stopped: ") and Monitoring.seen == []
    assert not (work / "restarts-report.json").exists()


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_its_tests_pass_and_it_is_formatted(phase, work):
    proc = run(phase[1], ["test", "restarts.nexo"], work)
    assert proc.returncode == 0 and "4 tests: 4 passed, 0 failed" in proc.stdout.decode("utf-8")
    before = (work / "restarts.nexo").read_bytes()
    assert run(phase[1], ["fmt", "restarts.nexo"], work).returncode == 0
    assert (work / "restarts.nexo").read_bytes() == before
    assert run(phase[1], ["lint", "restarts.nexo"], work).stdout.decode() == "No warnings in restarts.nexo.\n"
