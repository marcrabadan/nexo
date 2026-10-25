"""nexo.infra (section 11, D118-D121): plan, apply, drift, destroy, cost and
import against the local emulator of the cloud APIs, in both phases.

tests/infra/NAME.nexo is planned from a copy of tests/infra and its output
must equal NAME.expected; a NAME.args file holds extra arguments."""
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import cloud_emulator as emu
from phases import ROOT, commands, run

CASES = ROOT / "tests" / "infra"
NAMES = sorted(p.stem for p in CASES.glob("*.nexo") if (CASES / f"{p.stem}.expected").exists())
PHASES = commands()


@pytest.fixture(scope="module")
def server():
    s = emu.start()
    yield s
    s.shutdown()


@pytest.fixture
def cloud(server):
    emu.STATE.__init__()
    return emu.environment(server)


def nexo(phase, args, cwd, env, stdin=b""):
    p = run(phase[1], args, cwd, stdin=stdin, env=dict({"API_KEY": "sk-123"}, **env), timeout=300)
    return p.returncode, p.stdout.decode("utf-8")


def copy(tmp_path, *names):
    for n in names:
        shutil.copy(CASES / n, tmp_path / n)
    return tmp_path


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
@pytest.mark.parametrize("name", NAMES)
def test_plan(phase, name, cloud, tmp_path):
    copy(tmp_path, f"{name}.nexo")
    extra = (CASES / f"{name}.args").read_text().split() if (CASES / f"{name}.args").exists() else []
    _, out = nexo(phase, ["plan", *extra, f"{name}.nexo"], tmp_path, cloud)
    assert out == (CASES / f"{name}.expected").read_text(encoding="utf-8")


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_four_clouds_from_one_file(phase, cloud, tmp_path):
    copy(tmp_path, "multi.nexo")
    code, out = nexo(phase, ["apply", "--yes", "multi.nexo"], tmp_path, cloud)
    assert code == 0, out
    for infra, n in (("shop_aws", 17), ("shop_azure", 15), ("shop_gcp", 13), ("shop_k8s", 13)):
        state = json.loads((tmp_path / ".nexo" / "state" / f"{infra.replace('_', '-')}-default.json").read_text())
        assert len(state["resources"]) == n
    assert out.count("Applied:") == 4
    assert "url = http://203.0.113.10\n" in out
    # the secret never reaches the state nor the output
    assert "sk-123" not in out
    assert all("sk-123" not in p.read_text() for p in (tmp_path / ".nexo" / "state").iterdir())
    code, out = nexo(phase, ["plan", "multi.nexo"], tmp_path, cloud)
    assert code == 0 and out.count("No changes.") == 4, out
    code, out = nexo(phase, ["drift", "multi.nexo"], tmp_path, cloud)
    assert out.count("None: the cloud matches the state.") == 4, out

    # a change made outside Nexo, and a resource deleted outside it
    deploy = next(k for k in emu.STATE.k8s if k.endswith("/deployments/shop-k8s-api"))
    emu.STATE.k8s[deploy]["spec"]["template"]["spec"]["containers"][0]["image"] = "shop-api:hacked"
    bucket = next(k for k in emu.STATE.gcp if k.endswith("/b/shop-gcp-photos"))
    del emu.STATE.gcp[bucket]
    code, out = nexo(phase, ["drift", "multi.nexo"], tmp_path, cloud)
    assert code == 0
    assert "  ~ Deployment shop-k8s-api: spec.template.spec.containers.0.image (shop-api:2.4 -> shop-api:hacked)\n" in out
    assert "  - storage.buckets shop-gcp-photos: no longer exists\n" in out

    code, out = nexo(phase, ["destroy", "multi.nexo"], tmp_path, cloud,
                     stdin=b"shop_aws\nshop_azure\nshop_gcp\nshop_k8s\n")
    assert code == 0, out
    assert "shop_k8s: 13 resources deleted." in out
    assert not emu.STATE.aws and not emu.STATE.azure and not emu.STATE.k8s


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_destroy_needs_the_name(phase, cloud, tmp_path):
    copy(tmp_path, "shop.nexo")
    assert nexo(phase, ["apply", "--yes", "shop.nexo"], tmp_path, cloud)[0] == 0
    code, out = nexo(phase, ["destroy", "shop.nexo"], tmp_path, cloud, stdin=b"shp\n")
    assert code == 1 and out.endswith("(shop): Nothing was changed.\n"), out
    assert len(emu.STATE.aws) == 22


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_import_adopts_what_exists(phase, cloud, tmp_path):
    copy(tmp_path, "shop.nexo")
    assert nexo(phase, ["apply", "--yes", "shop.nexo"], tmp_path, cloud)[0] == 0
    (tmp_path / ".nexo" / "state" / "shop-default.json").unlink()
    code, out = nexo(phase, ["import", "aws", "photos", "shop-photos", "shop.nexo"], tmp_path, cloud)
    assert (code, out) == (0, "Imported: AWS::S3::Bucket shop-photos (shop-photos).\n")
    code, out = nexo(phase, ["plan", "shop.nexo"], tmp_path, cloud)
    assert "  = storage photos\n" in out and "To create: 21, to change: 0, to delete: 0." in out, out
    code, out = nexo(phase, ["import", "aws", "photo", "x", "shop.nexo"], tmp_path, cloud)
    assert code == 1 and "photo" in out


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_state_in_s3(phase, cloud, tmp_path):
    source = (CASES / "shop.nexo").read_text().replace('    budget', '    state = s3("nexo-state")\n    budget', 1)
    (tmp_path / "shop.nexo").write_text(source)
    code, out = nexo(phase, ["apply", "--yes", "shop.nexo"], tmp_path, cloud)
    assert code == 0, out
    assert not (tmp_path / ".nexo" / "state").exists()
    keys = [k for k in emu.STATE.s3 if k.startswith("/nexo-state/")]
    assert keys == ["/nexo-state/nexo/shop-default.json"]
    assert "No changes." in nexo(phase, ["plan", "shop.nexo"], tmp_path, cloud)[1]


@pytest.mark.skipif(len(PHASES) < 2, reason="needs cargo and clang")
def test_phases_share_the_state(cloud, tmp_path):
    interpreter, compiler = PHASES
    copy(tmp_path, "shop.nexo")
    assert nexo(interpreter, ["apply", "--yes", "shop.nexo"], tmp_path, cloud)[0] == 0
    assert "No changes." in nexo(compiler, ["plan", "shop.nexo"], tmp_path, cloud)[1]
    path = tmp_path / "shop.nexo"
    path.write_text(path.read_text().replace("size = medium", "size = small", 1))
    code, out = nexo(compiler, ["apply", "--yes", "shop.nexo"], tmp_path, cloud)
    assert code == 0 and "Applied: 0 created, 1 changed, 0 deleted." in out, out
    assert "No changes." in nexo(interpreter, ["plan", "shop.nexo"], tmp_path, cloud)[1]
    code, out = nexo(interpreter, ["drift", "shop.nexo"], tmp_path, cloud)
    assert "None: the cloud matches the state." in out


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_cost(phase, cloud, tmp_path):
    copy(tmp_path, "shop.nexo")
    code, out = nexo(phase, ["cost", "--stage", "prod", "shop.nexo"], tmp_path, cloud)
    assert code == 0 and out.startswith("Cost of shop (aws, stage prod):\n"), out
    assert out.endswith(" EUR per month.\n")


def test_no_infra_block(tmp_path):
    (tmp_path / "app.nexo").write_text('print("hola")\n')
    for phase in PHASES:
        code, out = nexo(phase, ["plan", "app.nexo"], tmp_path, {})
        assert code == 1 and "app.nexo has no infra block." in out, (phase[0], out)


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_an_infra_uses_values_of_another(phase, cloud, tmp_path):
    copy(tmp_path, "peers.nexo")
    code, out = nexo(phase, ["apply", "--yes", "peers.nexo"], tmp_path, cloud)
    assert code == 0, out
    # data is applied first although it is declared after web
    assert out.index("Plan for data") < out.index("Plan for web")
    assert "db = 10.20.0.3\n" in out
    deploy = next(v for k, v in emu.STATE.k8s.items() if k.endswith("/deployments/web-api"))
    env = deploy["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "DB", "value": "postgres://10.20.0.3/customers"} in env
    code, out = nexo(phase, ["destroy", "peers.nexo"], tmp_path, cloud, stdin=b"web\ndata\n")
    assert code == 0 and out.index("of web will") < out.index("of data will"), out


def certificates(d):
    """A certificate authority and a certificate for 127.0.0.1 it signs."""
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "2",
            "-subj", "/CN=nexo test ca")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr",
            "-subj", "/CN=127.0.0.1")
    (d / "san.ext").write_text("subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "server.crt", "-days", "2", "-extfile", "san.ext")
    return d / "ca.crt", d / "server.crt", d / "server.key"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_a_cluster_with_its_own_certificate_authority(phase, tmp_path):
    certs = tmp_path / "certs"
    certs.mkdir()
    ca, cert, key = certificates(certs)
    server = emu.start(tls=(str(cert), str(key)))
    emu.STATE.__init__()
    try:
        url = f"https://127.0.0.1:{server.server_address[1]}"
        (tmp_path / "app.nexo").write_text('infra web:\n    cloud = "k8s"\n    namespace = "web"\n'
                                           '    app api:\n        image = "shop-api:1.0"\n')
        env = {"NEXO_K8S_SERVER": url, "NEXO_K8S_TOKEN": emu.K8S_TOKEN, "NO_PROXY": "127.0.0.1",
               "no_proxy": "127.0.0.1"}
        # without its authority the certificate is refused, and nothing is sent
        code, out = nexo(phase, ["apply", "--yes", "app.nexo"], tmp_path, env)
        assert code == 1 and "There is no connection to" in out, out
        assert not emu.STATE.k8s
        code, out = nexo(phase, ["apply", "--yes", "app.nexo"], tmp_path, dict(env, NEXO_K8S_CA=str(tmp_path / "x.pem")))
        assert code == 1 and "The certificate authority of the cluster cannot be read" in out, out
        code, out = nexo(phase, ["apply", "--yes", "app.nexo"], tmp_path, dict(env, NEXO_K8S_CA=str(ca)))
        assert code == 0 and "Applied: 3 created" in out, out
    finally:
        server.shutdown()


GCP_SECRET = """infra web:
    cloud = "gcp"
    region = "europe-west1"
    app api:
        image = "shop-api:1.0"
        env:
            API_KEY = secret("API_KEY")
"""


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_a_changed_secret_is_a_change(phase, cloud, tmp_path):
    # Secret Manager keeps the value beside the body: its hash still counts
    (tmp_path / "app.nexo").write_text(GCP_SECRET)
    first = dict(cloud, API_KEY="first-key")
    assert nexo(phase, ["apply", "--yes", "app.nexo"], tmp_path, first)[0] == 0
    assert "No changes." in nexo(phase, ["plan", "app.nexo"], tmp_path, first)[1]
    second = dict(cloud, API_KEY="second-key")
    code, out = nexo(phase, ["apply", "--yes", "app.nexo"], tmp_path, second)
    assert code == 0 and "      ~ secretmanager.secrets web-api-api-key: value\n" in out, out
    versions = [e["body"] for e in emu.STATE.log if e["path"].endswith(":addVersion")]
    assert len(versions) == 2 and base64.b64encode(b"second-key").decode() in versions[1]
    assert "No changes." in nexo(phase, ["plan", "app.nexo"], tmp_path, second)[1]


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_a_state_changed_while_confirming_is_not_overwritten(phase, cloud, tmp_path):
    (tmp_path / "app.nexo").write_text('infra web:\n    cloud = "k8s"\n    namespace = "web"\n'
                                       '    app api:\n        image = "shop-api:1.0"\n')
    env = dict(os.environ, PYTHONPATH=str(ROOT), **cloud, API_KEY="sk-123")
    waiting = subprocess.Popen([*phase[1], "apply", "app.nexo"], cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    seen = b""
    while b"Type yes" not in seen:
        chunk = os.read(waiting.stdout.fileno(), 4096)
        assert chunk, seen
        seen += chunk
    # another apply runs while the first one asks
    assert nexo(phase, ["apply", "--yes", "app.nexo"], tmp_path, cloud)[0] == 0
    out, _ = waiting.communicate(b"yes\n", timeout=300)
    assert waiting.returncode == 1
    assert "Another command changed the state .nexo/state/web-default.json since this one read it." in out.decode()
