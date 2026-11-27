"""examples/sentinel/incidents.nexo: an attack on a service followed, in
the same window, by a restart or by CPU or memory trouble of that service
is an incident. report.json comes from a run of sentinel.nexo on the
corpus; the reports of restarts and resources are written here, around
the times of its attacks."""
import json
import shutil

import pytest

from phases import ROOT, commands, run

SENTINEL = ROOT / "examples" / "sentinel"


def restart(pod, container, severity, name, namespace="shop"):
    return {"severity": severity, "namespace": namespace, "pod": pod, "container": container, "restarts": 3,
            "state": "running", "reason": "OOMKilled", "exit_code": "137", "memory": "99%", "cause": "RST-101",
            "name": name, "proof": "metrics", "evidence": "", "context": []}


def pressure(pod, container, severity, name, detail):
    return {"rule": "RES-101", "name": name, "severity": severity, "namespace": "shop", "pod": pod,
            "container": container, "detail": detail}


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "sentinel"
    shutil.copytree(SENTINEL, folder)
    proc = run(commands()[0][1], ["sentinel.nexo"], folder, stdin=b"no\nno\n")
    assert proc.returncode == 0, proc.stdout.decode()
    return folder


def write(folder, name, value):
    (folder / name).write_text(json.dumps(value), encoding="utf-8")


def incidents(prefix, folder):
    proc = run(prefix, ["incidents.nexo"], folder)
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, out
    return out, json.loads((folder / "incidents-report.json").read_text(encoding="utf-8"))["incidents"]


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_an_attack_then_a_restart_is_an_incident(phase, work):
    write(work, "restarts-report.json", {"at": "2026-11-02T09:40:00.000Z", "window": "1h", "restarts": [
        restart("api-7d9f8-x2k4p", "api", "high", "out_of_memory"),
        restart("cart-66d4c-h7n2v", "cart", "high", "crash"),
        restart("api-7d9f8-q1w2e", "api", "low", "stopped_by_platform")]})
    write(work, "resources-report.json", {"at": "2026-11-02T10:45:00.000Z", "window": "30m", "findings": [
        pressure("worker-5c6b7-q8z1m", "worker", "high", "memory_near_limit", "1900Mi of 2048Mi (92%)"),
        pressure("api-7d9f8-x2k4p", "api", "medium", "cpu_increase", "100m -> 600m (+500%)")]})
    out, found = incidents(phase[1], work)
    assert [(i["service"], i["severity"]) for i in found] == [("shop-api", "critical"), ("shop-worker", "critical")]
    api, worker = found
    # every sure and high attack on the logs of the service, in order; only its own pods after them
    assert api["first_attack"] == "2026-11-02T09:14:06Z"
    assert api["sources"][:3] == ["203.0.113.9", "198.51.100.23", "192.0.2.10"]
    assert api["impacts"] == ["shop/api-7d9f8-x2k4p/api restarted 3 times: out_of_memory"]
    assert worker["impacts"] == ["shop/worker-5c6b7-q8z1m/worker: memory_near_limit, 1900Mi of 2048Mi (92%)"]
    assert any("SEN-108 jndi_injection at logs/nginx.log:5" in line for line in worker["attacks"])
    assert "INCIDENT critical - shop-api, attacked from 203.0.113.9, 198.51.100.23" in out
    assert "  After them:\n    shop/api-7d9f8-x2k4p/api restarted 3 times: out_of_memory\n" in out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_trouble_long_after_the_attacks_is_not_an_incident(phase, work):
    write(work, "restarts-report.json", {"at": "2026-11-03T12:00:00.000Z", "window": "1h", "restarts": [
        restart("api-7d9f8-x2k4p", "api", "critical", "crash")]})
    out, found = incidents(phase[1], work)
    assert found == [] and "No incidents" in out
    assert "Not read, as they are not there yet: resources-report.json" in out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_an_attack_alone_or_trouble_alone_is_not_an_incident(phase, work):
    write(work, "restarts-report.json", {"at": "2026-11-02T09:40:00.000Z", "window": "1h", "restarts": [
        restart("cart-66d4c-h7n2v", "cart", "critical", "crash")]})
    out, found = incidents(phase[1], work)
    assert found == []
    (work / "report.json").unlink()
    write(work, "restarts-report.json", {"at": "2026-11-02T09:40:00.000Z", "window": "1h", "restarts": [
        restart("api-7d9f8-x2k4p", "api", "critical", "crash")]})
    out, found = incidents(phase[1], work)
    assert found == [] and "Not read, as they are not there yet: report.json, resources-report.json" in out


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_its_tests_pass_and_it_is_formatted(phase, work):
    proc = run(phase[1], ["test", "incidents.nexo"], work)
    assert proc.returncode == 0 and "1 test: 1 passed, 0 failed" in proc.stdout.decode("utf-8")
    before = (work / "incidents.nexo").read_bytes()
    assert before.isascii()
    assert run(phase[1], ["fmt", "incidents.nexo"], work).returncode == 0
    assert (work / "incidents.nexo").read_bytes() == before
    assert run(phase[1], ["lint", "incidents.nexo"], work).stdout.decode() == "No warnings in incidents.nexo.\n"
