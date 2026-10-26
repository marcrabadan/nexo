"""`nexo new` (D148) and `nexo docs` (D147), in both phases with the same output."""
import pytest

from phases import commands, run

PHASES = commands()
IDS = [c[0] for c in PHASES]

DOCUMENTED = '''## Adds two numbers.
## Both are ints.
fn add(a: int, b: int) -> int:
    return a + b

## A point of the plane.
pub type Point:
    x: int
    y: int
    fn norm(self) -> int:
        return self.x * self.x + self.y * self.y

fn bare():
    pass
'''


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_new_makes_a_project_that_runs_and_passes_its_test(phase, tmp_path):
    out = run(phase[1], ["new", "shop"], tmp_path)
    assert out.returncode == 0
    assert out.stdout.decode() == ('Created the project "shop": shop/nexo.toml and shop/src/main.nexo. '
                                   "Run it with nexo shop/src/main.nexo and its tests with nexo test shop.\n")
    assert (tmp_path / "shop" / "nexo.toml").read_text() == (
        '[project]\nname = "shop"\nversion = "0.1.0"\nnexo = "0.1"\n')
    assert run(phase[1], ["shop/src/main.nexo"], tmp_path).stdout.decode() == "Hello, world!\n"
    tests = run(phase[1], ["test", "shop"], tmp_path)
    assert tests.returncode == 0 and tests.stdout.decode().endswith("1 test: 1 passed, 0 failed\n")
    # formatted and lint-clean
    assert run(phase[1], ["lint", "shop"], tmp_path).stdout.decode() == "No warnings in shop.\n"
    before = (tmp_path / "shop" / "src" / "main.nexo").read_text()
    run(phase[1], ["fmt", "shop"], tmp_path)
    assert (tmp_path / "shop" / "src" / "main.nexo").read_text() == before


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_new_refuses_a_bad_name_or_an_existing_folder(phase, tmp_path):
    (tmp_path / "shop").mkdir()
    out = run(phase[1], ["new", "shop"], tmp_path)
    assert out.returncode == 1
    assert out.stdout.decode() == 'Error: "shop" exists already. Choose another name, or remove it first.\n'
    out = run(phase[1], ["new", "../up"], tmp_path)
    assert out.returncode == 1 and "is not a valid project name" in out.stdout.decode()
    assert not (tmp_path.parent / "up").exists()
    assert run(phase[1], ["new"], tmp_path).returncode == 2


@pytest.mark.parametrize("phase", PHASES, ids=IDS)
def test_docs_writes_markdown_from_the_comments(phase, tmp_path):
    (tmp_path / "m.nexo").write_text(DOCUMENTED)
    (tmp_path / "plain.nexo").write_text("print(1)\n")
    out = run(phase[1], ["docs"], tmp_path)
    assert out.returncode == 0
    assert out.stdout.decode() == (
        "# m.nexo\n\n"
        "## `fn add(a: int, b: int) -> int`\n\nAdds two numbers.\nBoth are ints.\n\n"
        "## `pub type Point`\n\nA point of the plane.\n\n- `x: int`\n- `y: int`\n- `fn norm(self) -> int`\n\n"
        "## `fn bare()`\n")
    out = run(phase[1], ["docs", "plain.nexo"], tmp_path)
    assert out.stdout.decode() == "Nothing to document in plain.nexo: no functions, types or traits.\n"


def test_both_phases_document_alike(tmp_path):
    (tmp_path / "m.nexo").write_text(DOCUMENTED)
    outs = {run(p[1], ["docs", "m.nexo"], tmp_path).stdout for p in PHASES}
    assert len(outs) == 1
