"""examples/sentinel/resources.nexo: the containers whose CPU or memory
grew or is near its limit, from Prometheus, with what they logged, from
Loki. The two services are a local server that answers their HTTP APIs."""
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from phases import ROOT, commands, run

SENTINEL = ROOT / "examples" / "sentinel"
EXPECTED = (SENTINEL / "resources.expected").read_text(encoding="utf-8")
MI = 1048576
API, WORKER, CART, IMPORTER, TINY, BAD = (("shop", "api-7d9f8-x2k4p", "api"), ("shop", "worker-5c6b7-q8z1m", "worker"),
                                         ("shop", "cart-66d4c-h7n2v", "cart"), ("data", "importer-0", "importer"),
                                         ("shop", "tiny-1", "tiny"), ("shop", 'x"} or {job="', "api"))
# cores, and bytes
DATA = {
    "cpu": {API: 0.95, WORKER: 0.2, CART: 0.1, IMPORTER: 0.6, TINY: 0.03, BAD: 0.5},
    "cpu_before": {API: 0.4, WORKER: 0.19, CART: 0.1, IMPORTER: 0.1, TINY: 0.01},
    "cpu_limit": {API: 1, IMPORTER: 2},
    "throttled": {API: 38.2, CART: 3},
    "memory": {API: 480 * MI, WORKER: 900 * MI, CART: 100 * MI, IMPORTER: 210 * MI, TINY: 20 * MI},
    "memory_before": {API: 470 * MI, WORKER: 300 * MI, CART: 100 * MI, IMPORTER: 200 * MI, TINY: 8 * MI},
    "memory_limit": {API: 512 * MI, WORKER: 2048 * MI, CART: 256 * MI},
}
LOGS = {"api-7d9f8-x2k4p": ["WARN request timeout after 30s\x1b[2J", "ERROR connection pool exhausted"],
        "worker-5c6b7-q8z1m": ["ERROR cache holds 1200000 entries"]}


def series(query):
    before = "_before" if " offset " in query else ""
    if "cfs_throttled" in query:
        return "throttled"
    if "container_cpu_usage_seconds_total" in query:
        return "cpu" + before
    if "container_memory_working_set_bytes" in query:
        return "memory" + before
    if 'resource="cpu"' in query:
        return "cpu_limit"
    if 'resource="memory"' in query:
        return "memory_limit"


class Monitoring(BaseHTTPRequestHandler):
    seen: list = []
    without: str = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        Monitoring.seen.append((url.path, query))
        code = 200
        if url.path == "/api/v1/query":
            name = series(query["query"])
            if name is None or name == Monitoring.without:
                code, body = 422, {"status": "error"}
            else:
                result = [{"metric": {"namespace": n, "pod": p, "container": c}, "value": [1762768800, str(v)]}
                          for (n, p, c), v in DATA[name].items()
                          if 'namespace="' not in query["query"] or f'namespace="{n}"' in query["query"]]
                body = {"status": "success", "data": {"resultType": "vector", "result": result}}
        else:
            pod = query["query"].split('pod="')[1].split('"')[0]
            values = [[str(1762768800000000000 - i), line] for i, line in enumerate(LOGS.get(pod, []))]
            body = {"status": "success", "data": {"resultType": "streams",
                                                  "result": [{"stream": {"pod": pod}, "values": values}]}}
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
    Monitoring.without = ""
    folder = tmp_path / "sentinel"
    shutil.copytree(SENTINEL, folder)
    configure(folder, prometheus=server, loki=server)
    return folder


def configure(folder, **changes):
    settings = json.loads((folder / "resources.json").read_text(encoding="utf-8"))
    settings.update(changes)
    (folder / "resources.json").write_text(json.dumps(settings), encoding="utf-8")


def resources(prefix, folder):
    proc = run(prefix, ["resources.nexo"], folder)
    return proc.returncode, proc.stdout.decode("utf-8")


def findings(folder):
    report = json.loads((folder / "resources-report.json").read_text(encoding="utf-8"))
    return [(f["pod"], f["rule"], f["detail"]) for f in report["findings"]]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_growth_and_limits_are_found(phase, work):
    code, out = resources(phase[1], work)
    assert (code, out) == (0, EXPECTED)
    assert findings(work) == [
        ("api-7d9f8-x2k4p", "RES-101", "480Mi of 512Mi (93%)"),
        ("api-7d9f8-x2k4p", "RES-201", "950m of 1000m (95%)"),
        ("api-7d9f8-x2k4p", "RES-202", "400m -> 950m (+137%)"),
        ("api-7d9f8-x2k4p", "RES-203", "held back in 38% of its periods"),
        ("worker-5c6b7-q8z1m", "RES-102", "300Mi -> 900Mi (+200%)"),
        ("importer-0", "RES-202", "100m -> 600m (+500%)")]
    report = json.loads((work / "resources-report.json").read_text(encoding="utf-8"))
    assert report["containers"] == 5
    assert [(e["container"], e["lines"]) for e in report["logs"]] == [
        ("shop/api-7d9f8-x2k4p/api", 2), ("shop/worker-5c6b7-q8z1m/worker", 1), ("data/importer-0/importer", 0)]
    # Loki is asked only about the containers with a finding, and never with a name that changes the query
    asked = [q["query"] for path, q in Monitoring.seen if path.startswith("/loki")]
    assert len(asked) == 3 and not any("job=" in q for q in asked)
    assert asked[0].startswith('{namespace="shop", pod="api-7d9f8-x2k4p", container="api"} |~ "')
    assert "\x1b" not in out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_limits_of_the_settings(phase, work):
    configure(work, increase_percent=300, near_limit_percent=94, throttled_percent=40, namespace="shop", window="15m")
    code, out = resources(phase[1], work)
    assert code == 0 and "the last 15m against the 15m before" in out
    assert [(pod, rule) for pod, rule, _ in findings(work)] == [("api-7d9f8-x2k4p", "RES-201")]
    queries = [q["query"] for path, q in Monitoring.seen if path == "/api/v1/query"]
    assert queries[1] == ('sum by (namespace, pod, container) (rate(container_cpu_usage_seconds_total'
                          '{namespace="shop", container!=""}[15m] offset 15m))')


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_without_a_before_nothing_is_called_growth(phase, work):
    Monitoring.without = "memory_before"
    code, out = resources(phase[1], work)
    assert code == 0 and out.startswith("Without the memory of before: ")
    assert "RES-102" not in [rule for _, rule, _ in findings(work)]
    assert "RES-101" in [rule for _, rule, _ in findings(work)]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
@pytest.mark.parametrize("change", [{"window": "1h] offset 1h) or vector(1"}, {"namespace": 'a"}'},
                                    {"increase_percent": 0}, {"lines": "x"}])
def test_wrong_settings_send_nothing(phase, work, change):
    configure(work, **change)
    code, out = resources(phase[1], work)
    assert out.startswith("Sentinel stopped: ") and Monitoring.seen == []


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_its_tests_pass_and_it_is_formatted(phase, work):
    proc = run(phase[1], ["test", "resources.nexo"], work)
    assert proc.returncode == 0 and "3 tests: 3 passed, 0 failed" in proc.stdout.decode("utf-8")
    for name in ("resources.nexo", "monitoring.nexo"):
        before = (work / name).read_bytes()
        assert run(phase[1], ["fmt", name], work).returncode == 0
        assert (work / name).read_bytes() == before
        assert run(phase[1], ["lint", name], work).stdout.decode() == f"No warnings in {name}.\n"
