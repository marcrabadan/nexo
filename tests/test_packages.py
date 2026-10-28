"""Packages (section 13, D174): `nexo publish`, `nexo add`, nexo.lock and
`use` of a package, in both phases with the same output and files."""
import base64
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from phases import commands, run

PHASES = commands()
IDS = [c[0] for c in PHASES]

SHOW = '''use money_format.round

## Cents as a text with two decimals.
pub fn show(cents: int) -> str:
    return "{round.whole(cents)}.{round.part(cents)}"
'''
ROUND = '''pub fn whole(cents: int) -> int:
    return cents // 100

pub fn part(cents: int) -> str:
    p = cents % 100
    if p < 10:
        return "0{p}"
    return "{p}"
'''


class Env:
    def __init__(self, phase, tmp: Path):
        self.phase = phase
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.env = {"NEXO_REGISTRY": str(self.registry), "XDG_CONFIG_HOME": str(tmp / "config")}

    def nexo(self, args, cwd, **env):
        out = run(self.phase[1], args, cwd, env={**self.env, **env})
        return out.returncode, out.stdout.decode().replace(str(self.tmp), "TMP")

    def package(self, name, version, files, deps=None, permissions=None, config="config"):
        folder = self.tmp / "src" / f"{name}-{version}"
        folder.mkdir(parents=True)
        toml = f'[project]\nname = "{name}"\nversion = "{version}"\nnexo = "0.1"\n'
        if deps:
            toml += "\n[dependencies]\n" + "".join(f'{d} = "{r}"\n' for d, r in deps.items())
        if permissions:
            toml += "\n[permissions]\n" + permissions
        (folder / "nexo.toml").write_text(toml)
        for rel, text in files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(text)
        code, out = self.nexo(["publish"], folder, XDG_CONFIG_HOME=str(self.tmp / config))
        assert code == 0, out
        assert out.startswith(f"Published {name} {version} in TMP/registry, signed with the key ")
        return folder

    def project(self, main="", deps=None):
        root = self.tmp / "shop"
        root.mkdir()
        toml = '[project]\nname = "shop"\nversion = "0.1.0"\nnexo = "0.1"\n'
        if deps:
            toml += "\n[dependencies]\n" + "".join(f'{d} = "{r}"\n' for d, r in deps.items())
        (root / "nexo.toml").write_text(toml)
        (root / "main.nexo").write_text(main)
        return root


def lib(version="", extra=""):
    return {"lib.nexo": f"pub fn version() -> str:\n    return \"{version}\"\n{extra}"}


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_publish_add_and_use_a_package(phase, tmp_path):
    e = Env(phase, tmp_path)
    e.package("money-format", "1.2.0", {"lib.nexo": SHOW, "round.nexo": ROUND, "README.md": "# money\n"})
    root = e.project("use money_format\nuse money_format.round\nprint(money_format.show(1234))\nprint(round.whole(5))\n")
    code, out = e.nexo(["add", "money-format"], root)
    assert (code, out) == (0, "  money-format 1.2.0\nAdded money-format 1.2.0 to nexo.toml and nexo.lock.\n")
    assert (root / "nexo.toml").read_text().endswith('\n[dependencies]\nmoney-format = "^1.2"\n')
    lock = (root / "nexo.lock").read_text()
    assert lock.startswith("# nexo.lock: the exact packages of this project, written by nexo add. Do not edit.\n\n"
                           '[[package]]\nname = "money-format"\nversion = "1.2.0"\nsha256 = "')
    assert lock.endswith('"\ndependencies = []\n')
    installed = root / ".nexo" / "packages" / "money-format"
    assert sorted(p.name for p in installed.iterdir()) == ["README.md", "lib.nexo", "nexo.toml", "round.nexo"]
    assert e.nexo(["main.nexo"], root) == (0, "12.34\n0\n")
    # a clone of the project: `nexo add` installs exactly what nexo.lock says
    import shutil
    shutil.rmtree(root / ".nexo")
    code, out = e.nexo(["main.nexo"], root)
    assert code == 1 and out.endswith("The package money-format is not installed. "
                                      "Run nexo add to install the packages of nexo.lock.\n")
    assert e.nexo(["add"], root) == (0, "  money-format 1.2.0\nInstalled the packages of nexo.lock (1).\n")
    assert (root / "nexo.lock").read_text() == lock
    assert e.nexo(["main.nexo"], root) == (0, "12.34\n0\n")
    # a published version never changes
    code, out = e.nexo(["publish"], tmp_path / "src" / "money-format-1.2.0")
    assert (code, out) == (1, "Error: The registry has money-format 1.2.0 already. "
                              "Raise the version in nexo.toml: a published version never changes.\n")


def test_both_phases_publish_and_lock_the_same_bytes(tmp_path):
    if len(PHASES) < 2:
        pytest.skip("needs the compiler")
    outputs = []
    for phase in PHASES:
        e = Env(phase, tmp_path / phase[0])
        e.env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
        e.package("money-format", "1.2.0", {"lib.nexo": SHOW, "round.nexo": ROUND}, config="../../config")
        root = e.project()
        e.nexo(["add", "money-format"], root)
        files = sorted(p for p in e.registry.rglob("*") if p.is_file())
        outputs.append([(p.relative_to(e.registry), p.read_bytes()) for p in files]
                       + [(root / "nexo.lock").read_bytes(), (root / "nexo.toml").read_bytes()])
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_one_version_of_each_package_the_highest_every_requirement_accepts(phase, tmp_path):
    e = Env(phase, tmp_path)
    for v in ("1.0.0", "1.2.0", "2.0.0"):
        e.package("base", v, lib(v))
    e.package("tax", "1.0.0", {"lib.nexo": "use base\npub fn rate() -> str:\n    return base.version()\n"},
              deps={"base": "^1.0"})
    root = e.project("use tax\nuse base\nprint(tax.rate())\nprint(base.version())\n", deps={"base": "^1.0"})
    code, out = e.nexo(["add", "tax"], root)
    assert (code, out) == (0, "  base 1.2.0\n  tax 1.0.0\nAdded tax 1.0.0 to nexo.toml and nexo.lock.\n")
    assert 'name = "tax"\nversion = "1.0.0"' in (root / "nexo.lock").read_text()
    assert 'dependencies = ["base ^1.0"]' in (root / "nexo.lock").read_text()
    assert e.nexo(["main.nexo"], root) == (0, "1.2.0\n1.2.0\n")
    # a newer compatible version does not move the lock
    e.package("base", "1.3.0", lib("1.3.0"))
    assert e.nexo(["add"], root)[1].startswith("  base 1.2.0\n")
    # the project asks for base 2, tax for base 1: one version cannot be both
    (root / "nexo.toml").write_text((root / "nexo.toml").read_text().replace('base = "^1.0"', 'base = "^2.0"'))
    code, out = e.nexo(["add"], root)
    assert (code, out) == (1, "Error: nexo.toml needs base ^2.0 and tax 1.0.0 needs base ^1.0, and no version is "
                              "both. A program has one version of each package: update one of them so both "
                              "accept the same version.\n")
    (root / "nexo.toml").write_text((root / "nexo.toml").read_text().replace('base = "^2.0"', 'base = "2"'))
    code, out = e.nexo(["add"], root)
    assert (code, out) == (1, 'Error: "2" is not a version requirement. Write ^MAJOR.MINOR, such as ^2.1: that '
                              "version or a later compatible one.\n")


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_a_dependency_of_a_dependency_is_not_used_by_name(phase, tmp_path):
    e = Env(phase, tmp_path)
    e.package("base", "1.0.0", lib("1.0.0"))
    e.package("tax", "1.0.0", {"lib.nexo": "use helpers\npub fn rate() -> int:\n    return 1\n"})
    root = e.project("use base\nprint(base.version())\n")
    e.nexo(["add", "tax"], root)
    code, out = e.nexo(["main.nexo"], root)
    assert code == 1 and "There is no module base: there is no file base.nexo." in out
    (root / "main.nexo").write_text("use tax\nprint(tax.rate())\n")
    code, out = e.nexo(["main.nexo"], root)
    assert code == 1 and out.endswith(
        "A file of the package tax uses helpers, which is neither a module of the package nor one of its "
        "dependencies. Write use helpers as the name of one of its dependencies, listed in the [dependencies] "
        "of its nexo.toml, or name its own modules after the package.\n")


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_a_changed_archive_or_signature_is_never_installed(phase, tmp_path):
    e = Env(phase, tmp_path)
    e.package("base", "1.0.0", lib("1.0.0"))
    archive = e.registry / "base" / "base-1.0.0.json"
    good = archive.read_bytes()
    archive.write_bytes(good.replace(b'1.0.0\\"', b'6.6.6\\"'))
    root = e.project()
    code, out = e.nexo(["add", "base"], root)
    assert (code, out) == (1, "Error: The package base 1.0.0 does not have a valid signature. Nothing was "
                              "installed: a package without a valid signature is never installed. Tell the "
                              "registry.\n")
    assert not (root / ".nexo").exists() and not (root / "nexo.lock").exists()
    assert "dependencies" not in (root / "nexo.toml").read_text()
    archive.write_bytes(good)
    assert e.nexo(["add", "base"], root)[0] == 0
    # published again with other files: not the one in nexo.lock
    index = e.registry / "base" / "index.json"
    index.write_text(index.read_text().replace('"version":"1.0.0"', '"version":"0.0.1"'))
    (tmp_path / "src" / "base-1.0.0" / "lib.nexo").write_text("pub fn version() -> str:\n    return \"x\"\n")
    e.nexo(["publish"], tmp_path / "src" / "base-1.0.0")
    code, out = e.nexo(["add"], root)
    assert (code, out) == (1, "Error: The package base 1.0.0 is not the one in nexo.lock: its sha256 changed. "
                              "Nothing was installed. If it was published again on purpose, remove its entry "
                              "from nexo.lock and run nexo add base.\n")


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_a_new_version_signed_by_another_key_is_not_installed(phase, tmp_path):
    e = Env(phase, tmp_path)
    e.package("base", "1.0.0", lib("1.0.0"))
    root = e.project()
    assert e.nexo(["add", "base"], root)[0] == 0
    e.package("base", "1.1.0", lib("1.1.0"), config="other")
    (root / "nexo.toml").write_text((root / "nexo.toml").read_text().replace("^1.0", "^1.1"))
    code, out = e.nexo(["add"], root)
    assert (code, out) == (1, "Error: The package base 1.1.0 is signed by another key than the one in nexo.lock. "
                              "Nothing was installed. Check with its author that the key changed, then remove "
                              "its entry from nexo.lock and run nexo add base.\n")
    key = json.loads((e.registry / "base" / "index.json").read_text())["versions"][1]["key"]
    assert len(base64.b64decode(key)) == 32


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_permissions_beyond_the_policy_reject_the_package(phase, tmp_path):
    e = Env(phase, tmp_path)
    e.package("pay", "1.0.0", lib("1.0.0"), permissions='net = ["api.stripe.com"]\nshell = true\n')
    root = e.project('policy:\n    allow net "*.other.com"\n\nprint(1)\n')
    code, out = e.nexo(["add", "pay"], root)
    assert (code, out) == (1, 'Error: The package pay needs connecting to api.stripe.com, and the policy of '
                              'main.nexo does not allow it. Add allow net "api.stripe.com" to that policy if the '
                              "package should have it, or choose another package.\n")
    (root / "main.nexo").write_text('policy:\n    allow net "*.stripe.com"\n\nprint(1)\n')
    code, out = e.nexo(["add", "pay"], root)
    assert (code, out) == (1, "Error: The package pay needs running shell commands, and the policy of main.nexo "
                              "does not allow it. Add allow shell to that policy if the package should have it, "
                              "or choose another package.\n")
    (root / "main.nexo").write_text('policy:\n    allow net "*.stripe.com"\n    allow shell\n\nprint(1)\n')
    code, out = e.nexo(["add", "pay"], root)
    assert (code, out) == (0, "  pay 1.0.0\n    needs connecting to api.stripe.com\n    needs running shell "
                              "commands\nAdded pay 1.0.0 to nexo.toml and nexo.lock.\n")


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_a_critical_advisory_blocks_the_install(phase, tmp_path):
    e = Env(phase, tmp_path)
    e.package("base", "1.0.0", lib("1.0.0"))
    index = e.registry / "base" / "index.json"
    doc = json.loads(index.read_text())
    doc["versions"][0]["advisories"] = [{"id": "NXA-1", "severity": "critical", "summary": "runs any code"},
                                        {"id": "NXA-2", "severity": "low", "summary": "slow"}]
    index.write_text(json.dumps(doc))
    root = e.project()
    code, out = e.nexo(["add", "base"], root)
    assert (code, out) == (1, "Error: The package base 1.0.0 has a critical vulnerability: NXA-1, runs any code. "
                              "Choose another version, or install it anyway with nexo add base --accept-risk.\n")
    code, out = e.nexo(["add", "base", "--accept-risk"], root)
    assert (code, out) == (0, "Warning: base 1.0.0 has the vulnerability NXA-1 (critical): runs any code.\n"
                              "Warning: base 1.0.0 has the vulnerability NXA-2 (low): slow.\n"
                              "  base 1.0.0\nAdded base 1.0.0 to nexo.toml and nexo.lock.\n")


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_a_registry_on_the_network(phase, tmp_path):
    e = Env(phase, tmp_path)
    e.package("base", "1.0.0", lib("1.0.0"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(e.registry))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        root = e.project("use base\nprint(base.version())\n")
        assert e.nexo(["add", "base"], root, NEXO_REGISTRY=url)[0] == 0
        assert e.nexo(["main.nexo"], root) == (0, "1.0.0\n")
        code, out = e.nexo(["add", "nothing"], root, NEXO_REGISTRY=url)
        assert (code, out) == (1, f"Error: There is no package nothing in the registry {url}. Check the name: "
                                  "packages are written as in nexo.toml, such as money-format.\n")
        code, out = e.nexo(["add", "base"], root, NEXO_REGISTRY="http://example.com")
        assert (code, out) == (1, "Error: The registry http://example.com cannot be read: HTTPS is needed for any "
                                  "host but this machine. Check the network, or the folder or address in "
                                  "NEXO_REGISTRY.\n")
        code, out = e.nexo(["publish"], tmp_path / "src" / "base-1.0.0", NEXO_REGISTRY=url)
        assert (code, out) == (1, f"Error: nexo publish writes to the folder of a registry, and {url} is an "
                                  "address. Set NEXO_REGISTRY to the folder of the registry.\n")
    finally:
        server.shutdown()


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_add_outside_a_project_and_bad_names(phase, tmp_path):
    e = Env(phase, tmp_path)
    assert e.nexo(["add", "base"], tmp_path) == (1, "Error: There is no nexo.toml in this folder or above it. "
                                                    "Run it inside a project; nexo new name creates one.\n")
    root = e.project()
    code, out = e.nexo(["add", "Money_Format"], root)
    assert (code, out) == (1, 'Error: "Money_Format" is not a valid package name. Use lowercase letters and '
                              "digits, words joined by -, starting with a letter, such as money-format.\n")
    assert e.nexo(["add", "a", "b"], root)[0] == 2


def test_a_release_build_lists_its_packages_in_the_sbom(tmp_path):
    compiled = [p for p in PHASES if p[0] == "compiler"]
    if not compiled:
        pytest.skip("needs the compiler")
    e = Env(compiled[0], tmp_path)
    e.package("money-format", "1.2.0", {"lib.nexo": SHOW, "round.nexo": ROUND})
    root = e.project('policy:\n    allow read "data"\n\nuse money_format\nprint(money_format.show(705))\n')
    e.nexo(["add", "money-format"], root)
    assert e.nexo(["build", "--release", "main.nexo", "-o", "app"], root)[0] == 0
    assert run([str(root / "app")], [], root).stdout == b"7.05\n"
    sbom = json.loads((root / "app.sbom.json").read_text())
    lock = (root / "nexo.lock").read_text()
    sha = lock.split('sha256 = "')[1].split('"')[0]
    package = next(c for c in sbom["components"] if c.get("purl") == "pkg:nexo/money-format@1.2.0")
    assert package["hashes"] == [{"alg": "SHA-256", "content": sha}]
    files = {c["name"] for c in sbom["components"] if c["type"] == "file"}
    assert {".nexo/packages/money-format/lib.nexo", ".nexo/packages/money-format/round.nexo"} <= files
