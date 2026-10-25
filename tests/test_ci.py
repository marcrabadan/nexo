"""nexo.ci (section 12, D122, D123): `nexo ci run` and `nexo ci export` in
both phases. tests/ci/app.nexo is exported to each service and must equal
tests/ci/TARGET.expected."""
import json
import shutil
import subprocess

import pytest

import cloud_emulator as emu
from phases import ROOT, commands, run

CASES = ROOT / "tests" / "ci"
PHASES = commands()
EXPORTS = {"github": ".github/workflows/main.yml", "gitlab": ".gitlab-ci.yml", "azure": "azure-pipelines.yml",
           "jenkins": "Jenkinsfile"}


def nexo(phase, args, cwd, stdin=b"", env=None):
    p = run(phase[1], args, cwd, stdin=stdin, env=env, timeout=300)
    return p.returncode, p.stdout.decode("utf-8")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """tests/ci/app.nexo in a git repository: v1.2.0 and then a feat."""
    shutil.copy(CASES / "app.nexo", tmp_path / "app.nexo")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "ci")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fix: first")
    git(tmp_path, "tag", "v1.2.0")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: typo")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feat(api): orders")
    return tmp_path


KEY = {"DEPLOY_KEY": "k"}

RUN_OK = """== paso build
hola ci
compilando
== paso dev
shop:1.3.0 dev
== paso prod
¿Aprobar el paso prod? Lo aprueban ana@company.com, luis@company.com. Escribe sí para seguir: prod shop:1.3.0
== after success
v1.3.0 en producción
Pipeline main: 3 pasos correctos, versión 1.3.0.
"""


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_run_with_an_approval(phase, repo):
    code, out = nexo(phase, ["ci", "run"], repo, stdin=b"si\n", env=KEY)
    assert (code, out) == (0, RUN_OK)
    state = json.loads((repo / ".nexo" / "ci" / "main.json").read_text())
    assert state["vars"]["image"] == "shop:1.3.0"


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_a_refused_approval_fails_the_step(phase, repo):
    code, out = nexo(phase, ["ci", "run", "main", "app.nexo"], repo, stdin=b"no\n", env=KEY)
    assert code == 1
    assert "El paso prod no se ha aprobado." in out
    assert out.endswith("Pipeline main: el paso prod ha fallado.\n== after failure\nfallo en prod\n")


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_steps_one_by_one(phase, repo):
    # as the exported jobs run them: the state travels in .nexo/ci
    code, out = nexo(phase, ["ci", "run", "--step", "dev", "--yes"], repo, env=KEY)
    assert code == 1 and "image no tiene valor en este paso" in out
    assert nexo(phase, ["ci", "run", "--step", "build", "--yes"], repo, env=KEY)[0] == 0
    code, out = nexo(phase, ["ci", "run", "--step", "dev", "--yes"], repo, env=KEY)
    assert (code, out) == (0, "== paso dev\nshop:1.3.0 dev\n")
    code, out = nexo(phase, ["ci", "run", "--step", "prod", "--yes"], repo, env=KEY)
    assert "Paso prod aprobado con --yes (lo aprueban ana@company.com, luis@company.com).\n" in out
    code, out = nexo(phase, ["ci", "run", "--after", "success"], repo, env=KEY)
    assert (code, out) == (0, "== after success\nv1.3.0 en producción\n")


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_a_failing_command(phase, repo):
    path = repo / "app.nexo"
    path.write_text(path.read_text().replace('run("echo compilando")', 'run("echo roto; exit 2")'))
    code, out = nexo(phase, ["ci", "run", "--yes"], repo, env=KEY)
    assert code == 1
    assert 'El comando "echo roto; exit 2" ha terminado con el código 2.' in out
    assert out.endswith("== after failure\nfallo en build\n")
    state = json.loads((repo / ".nexo" / "ci" / "main.json").read_text())
    assert state["failed"] == "build"
    code, out = nexo(phase, ["ci", "run", "--after", "failure"], repo, env=KEY)
    assert out == "== after failure\nfallo en build\n"


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_missing_secret_and_names(phase, repo):
    code, out = nexo(phase, ["ci", "run"], repo)
    assert code == 1 and "Faltan los secretos DEPLOY_KEY del pipeline main." in out
    code, out = nexo(phase, ["ci", "run", "mian"], repo, env=KEY)
    assert code == 1 and '¿Quisiste decir "main"?' in out


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_version_auto_needs_git(phase, tmp_path):
    shutil.copy(CASES / "app.nexo", tmp_path / "app.nexo")
    code, out = nexo(phase, ["ci", "run"], tmp_path, env=dict(KEY, GIT_CEILING_DIRECTORIES=str(tmp_path.parent)))
    assert code == 1 and "version = auto necesita un repositorio git." in out


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
@pytest.mark.parametrize("target", list(EXPORTS))
def test_export(phase, target, tmp_path):
    shutil.copy(CASES / "app.nexo", tmp_path / "app.nexo")
    code, out = nexo(phase, ["ci", "export", target], tmp_path)
    assert (code, out) == (0, f"Escrito {EXPORTS[target]}.\n")
    assert (tmp_path / EXPORTS[target]).read_text() == (CASES / f"{target}.expected").read_text()


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_tests_in_a_step(phase, tmp_path):
    (tmp_path / "app.nexo").write_text('test "ok":\n    assert 1 == 1\n\npipeline main:\n'
                                       '    step checks:\n        test()\n')
    code, out = nexo(phase, ["ci", "run"], tmp_path)
    assert code == 0 and "✓ ok" in out, out
    (tmp_path / "bad.nexo").write_text('test "broken":\n    assert 1 == 2\n')
    code, out = nexo(phase, ["ci", "run", "app.nexo"], tmp_path)
    assert code == 1 and "Hay tests que fallan." in out, out


@pytest.mark.parametrize("phase", PHASES, ids=lambda p: p[0])
def test_deploy_from_a_pipeline(phase, tmp_path):
    server = emu.start()
    emu.STATE.__init__()
    try:
        shutil.copy(CASES / "deploy.nexo", tmp_path / "deploy.nexo")
        env = dict(emu.environment(server), NEXO_CONTAINER_TOOL="echo")
        code, out = nexo(phase, ["ci", "run"], tmp_path, env=env)
        assert code == 0, out
        assert out.startswith("== paso build\nbuild -t shop-api:1.4.0 .\n== paso review\nComentario para la revisión:\n"
                              "Plan de web (k8s, etapa prod):\n")
        assert out.endswith("Aplicado: 3 creados, 0 cambiados, 0 borrados.\n"
                            "Pipeline release: 3 pasos correctos, versión 1.4.0.\n")
        comment = (tmp_path / ".nexo" / "ci" / "comment.md").read_text()
        assert comment.startswith("Plan de web (k8s, etapa prod):\n") and "Crear: 3" in comment
        deploy = next(v for k, v in emu.STATE.k8s.items() if k.endswith("/deployments/web-api"))
        assert deploy["spec"]["template"]["spec"]["containers"][0]["image"] == "shop-api:1.4.0"
        assert deploy["spec"]["replicas"] == 2
    finally:
        server.shutdown()
