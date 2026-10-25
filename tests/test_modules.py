"""Modules of the program's own files (section 13, D124) in both phases:
the root of a project, the tests of a module, `nexo check` and `nexo lint`.
The programs themselves are conformance cases (tests/conformance/modules)."""
import pytest

from phases import commands, run

PAYMENTS = """pub type Status:
    Paid(amount: int)
    Pending


pub fn charge(amount: int) -> Status:
    if amount == 0:
        return Pending
    return Paid(amount + taxes.due(amount))


test "charge":
    assert charge(0) == Pending
"""

TAXES = """pub fn due(amount: int) -> int:
    return amount // 10
"""

MAIN = """use shop.payments

match payments.charge(10):
    payments.Paid(n): print("paid {n}")
    payments.Pending: print("pending")
"""


def project(tmp_path, toml=True):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "payments.nexo").write_text("use shop.taxes\n\n" + PAYMENTS, encoding="utf-8")
    (tmp_path / "shop" / "taxes.nexo").write_text(TAXES, encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.nexo").write_text(MAIN, encoding="utf-8")
    if toml:
        (tmp_path / "nexo.toml").write_text("", encoding="utf-8")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_root_is_the_folder_of_nexo_toml(phase, tmp_path):
    project(tmp_path)
    proc = run(phase[1], ["app/main.nexo"], tmp_path)
    assert (proc.returncode, proc.stdout.decode()) == (0, "paid 11\n")
    # from any folder of the project
    proc = run(phase[1], ["main.nexo"], tmp_path / "app")
    assert (proc.returncode, proc.stdout.decode()) == (0, "paid 11\n")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_without_nexo_toml_the_root_is_the_folder_of_the_program(phase, tmp_path):
    project(tmp_path, toml=False)
    proc = run(phase[1], ["app/main.nexo"], tmp_path)
    assert proc.returncode == 1
    assert proc.stdout.decode() == (
        "Error en app/main.nexo, línea 1:\n"
        "  use shop.payments\n"
        "  ^^^^^^^^^^^^^^^^^\n"
        "No existe el módulo shop.payments: no hay ningún archivo app/shop/payments.nexo. Crea el archivo o "
        "corrige el nombre; el nombre de un módulo es su ruta (shop/payments.nexo es shop.payments).\n"
    )


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_module_runs_its_own_tests(phase, tmp_path):
    project(tmp_path)
    proc = run(phase[1], ["test", "shop/payments.nexo"], tmp_path)
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout.decode() == "✓ charge\n1 prueba: 1 correcta, 0 fallidas\n"
    # and the program that uses it does not run them
    with (tmp_path / "app" / "main.nexo").open("a", encoding="utf-8") as f:
        f.write('\n\ntest "main":\n    assert payments.charge(0) == payments.Pending\n')
    proc = run(phase[1], ["test", "app/main.nexo"], tmp_path)
    assert proc.stdout.decode() == "paid 11\n✓ main\n1 prueba: 1 correcta, 0 fallidas\n"


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_check_and_lint_read_the_modules(phase, tmp_path):
    project(tmp_path)
    proc = run(phase[1], ["check", "app/main.nexo"], tmp_path)
    assert (proc.returncode, proc.stdout.decode()) == (0, "No hay errores en app/main.nexo.\n")
    proc = run(phase[1], ["lint", "app/main.nexo"], tmp_path)
    assert (proc.returncode, proc.stdout.decode()) == (0, "No hay avisos en app/main.nexo.\n")
    (tmp_path / "app" / "main.nexo").write_text(MAIN.replace("payments.Pending", "payments.Waiting"), encoding="utf-8")
    proc = run(phase[1], ["check", "app/main.nexo"], tmp_path)
    assert proc.returncode == 1
    assert proc.stdout.decode() == (
        "Error en app/main.nexo, línea 5:\n"
        '  payments.Waiting: print("pending")\n'
        "  ^^^^^^^^^^^^^^^^\n"
        'El módulo shop.payments no tiene "Waiting". Revisa el nombre; un módulo solo exporta sus funciones y '
        "tipos pub.\n"
    )


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_the_repl_remembers_the_modules_of_the_session(phase, tmp_path):
    project(tmp_path)
    proc = run(phase[1], [], tmp_path, stdin=b"use shop.taxes\ntaxes.due(50)\ntaxes.nope(1)\ntaxes.due(10)\n")
    out = proc.stdout.decode().split("\n", 1)[1]
    assert out == (
        ">>> >>> 5\n"
        ">>> Error en <repl>, línea 1:\n"
        "  taxes.nope(1)\n"
        "  ^^^^^^^^^^\n"
        'El módulo shop.taxes no tiene "nope". Revisa el nombre; un módulo solo exporta sus funciones y tipos pub.\n'
        ">>> 1\n"
        ">>> \n"
    )
