"""The exit criterion of phase 5 (section 25): "an app deploys to AWS, Azure,
GCP and k8s from the same file". examples/deploy/shop.nexo holds the app,
its tests, four infra blocks and the pipeline that builds and deploys it;
each phase runs that pipeline against the emulator of the four APIs
(tests/cloud_emulator.py)."""
import base64
import json
import shutil

import pytest

import cloud_emulator as emu
from phases import ROOT, commands, run

APP = ROOT / "examples" / "deploy"
CLOUDS = {"shop_aws": "aws", "shop_azure": "azure", "shop_gcp": "gcp", "shop_k8s": "k8s"}


@pytest.fixture
def cloud():
    server = emu.start()
    emu.STATE.__init__()
    yield dict(emu.environment(server), API_KEY="sk-live-1", NEXO_CONTAINER_TOOL="echo")
    server.shutdown()


def nexo(phase, args, cwd, env, stdin=b""):
    p = run(phase[1], args, cwd, stdin=stdin, env=env, timeout=600)
    return p.returncode, p.stdout.decode("utf-8")


def held(target: str) -> str:
    """What the emulator holds for one cloud, as text."""
    store = getattr(emu.STATE, target)
    text = json.dumps(list(store.values()))
    if target == "gcp":
        # Secret Manager keeps the versions apart
        text += json.dumps([e["body"] for e in emu.STATE.log if e["path"].endswith(":addVersion")])
    return text


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_one_file_deploys_to_four_clouds(phase, cloud, tmp_path):
    shutil.copy(APP / "shop.nexo", tmp_path / "shop.nexo")
    code, out = nexo(phase, ["ci", "run", "--yes"], tmp_path, cloud)
    assert code == 0, out
    assert out.startswith("== step checks\n✓ shipping grows with the quantity\n")
    assert "build -t shop:1.1.0 .\n" in out
    assert out.endswith("Pipeline main: 3 steps passed, version 1.1.0.\n")
    # the four targets, in the order their references need
    order = [out.index(f"Plan de {name} ") for name in ("shop_aws", "shop_azure", "shop_gcp", "shop_k8s")]
    assert order == sorted(order)
    for target in CLOUDS.values():
        text = held(target)
        assert "shop:1.1.0" in text and "shop:1.0" not in text, target
        # the secret reaches each cloud, and never the state
        assert "sk-live-1" in text or base64.b64encode(b"sk-live-1").decode() in text, target
    for infra in CLOUDS:
        state = (tmp_path / ".nexo" / "state" / f"{infra.replace('_', '-')}-prod.json").read_text()
        assert "sk-live-1" not in state
    # the app on k8s reads the database on GCP
    deploy = next(v for k, v in emu.STATE.k8s.items() if k.endswith("/deployments/shop-k8s-api"))
    env = {e["name"]: e.get("value") for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DB"].startswith("postgres://") and env["NEXO_STAGE"] == "prod"

    code, out = nexo(phase, ["drift", "--stage", "prod", "shop.nexo"], tmp_path, cloud)
    assert out.count("None: the cloud matches the state.") == 4, out
    # destroy keeps the order of the file, but shop_k8s goes before the
    # database it reads on GCP
    names = b"shop_aws\nshop_azure\nshop_k8s\nshop_gcp\n"
    code, out = nexo(phase, ["destroy", "--stage", "prod", "shop.nexo"], tmp_path, cloud, stdin=names)
    assert code == 0, out
    assert not emu.STATE.aws and not emu.STATE.azure and not emu.STATE.k8s


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_exported_workflow(phase, tmp_path):
    shutil.copy(APP / "shop.nexo", tmp_path / "shop.nexo")
    code, out = nexo(phase, ["ci", "export", "github"], tmp_path, {})
    assert (code, out) == (0, "Wrote .github/workflows/main.yml.\n")
    text = (tmp_path / ".github" / "workflows" / "main.yml").read_text()
    for step in ("checks", "build", "deploy"):
        assert f"nexo ci run main shop.nexo --step {step} --yes" in text
    assert "    environment: deploy\n" in text and "API_KEY: ${{ secrets.API_KEY }}" in text
