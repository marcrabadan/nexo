"""Privacy (section 23, D116) in both phases: @private columns encrypted at
rest, keep periods, privacy.export, erase and restrict, the lint rule and
`nexo privacy report`."""
import sqlite3

import pytest

from phases import commands, compiler, run

APP = """use nexo.db

db = connect("shop.db", key: secret("DB_KEY"))

@table
type Customer:
    id: id
    name: str @personal
    email: str @personal
    national_id: str @private
    last_activity: date
    orders: many Order

    keep = 5 years after last_activity
    purpose = "billing and shipping"

@table
type Order:
    id: id
    customer: ref Customer
    total: int

ana = db.Customer.add(name: "Ana", email: "ana@x.com", national_id: "123A", last_activity: date(2026, 1, 2))
db.Customer.add(name: "Old", email: "old@x.com", national_id: "999Z", last_activity: date(2001, 1, 1))
db.Order.add(customer: ana, total: 30)
print(db.Customer.count())
print(db.Customer.find(national_id: "123A").name)
print(db.Customer |> where national_id == "123A" |> map name)
path = privacy.export(ana)
print(path)
print(read(path))
privacy.restrict(ana)
print(db.Customer.count())
luis = db.Customer.add(name: "Luis", email: "l@x.com", national_id: "55", last_activity: date(2026, 1, 2))
privacy.erase(luis)
print(db.Customer.count())
# a new connection applies the keep period: Old is erased
again = connect("shop.db", key: secret("DB_KEY"))
print(again.Customer.count())
print(db.sql("SELECT name FROM customer"))
"""

OUT = """2
Ana
["Ana"]
customer-1.json
{
  "id": 1,
  "name": "Ana",
  "email": "ana@x.com",
  "national_id": "123A",
  "last_activity": "2026-01-02",
  "orders": [
    {
      "id": 1,
      "customer": 1,
      "total": 30
    }
  ]
}

1
1
0
[{"name": "Ana"}]
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_private_fields_keep_and_rights(phase, tmp_path):
    (tmp_path / "app.nexo").write_text(APP, encoding="utf-8")
    proc = run(phase[1], ["app.nexo"], tmp_path, env={"DB_KEY": "key of the tests"})
    assert proc.stdout.decode() == OUT
    with sqlite3.connect(tmp_path / "shop.db") as c:
        stored = [r[0] for r in c.execute("SELECT national_id FROM customer")]
        assert len(stored) == 1 and stored[0].startswith("nexo:") and "123A" not in stored[0]
        assert list(c.execute("SELECT * FROM nexo_privacy")) == [("customer", 1, "restricted")]
    # another key cannot read them
    with sqlite3.connect(tmp_path / "shop.db") as c:
        c.execute("DELETE FROM nexo_privacy")
    reader = APP.split("ana = ")[0] + "print(db.Customer.all())\n"
    (tmp_path / "reader.nexo").write_text(reader, encoding="utf-8")
    proc = run(phase[1], ["reader.nexo"], tmp_path, env={"DB_KEY": "another key"})
    assert "no se pueden descifrar con esta clave" in proc.stdout.decode()


def test_both_phases_send_the_same_sql(tmp_path):
    phases = commands()
    if len(phases) < 2:
        pytest.skip("the compiler is not available")
    logs = []
    for name, prefix in phases:
        work = tmp_path / name
        work.mkdir()
        (work / "app.nexo").write_text(APP, encoding="utf-8")
        proc = run(prefix, ["app.nexo"], work, env={"DB_KEY": "k", "NEXO_SQL_LOG": "1"})
        logs.append([line for line in proc.stdout.decode().splitlines() if line.startswith("sql: ")])
    assert logs[0] == logs[1]
    assert any("NOT IN (SELECT \"row\" FROM \"nexo_privacy\"" in line for line in logs[0])


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_private_field_needs_a_key(phase, tmp_path):
    (tmp_path / "app.nexo").write_text(APP.replace(', key: secret("DB_KEY")', ""), encoding="utf-8")
    out = run(phase[1], ["app.nexo"], tmp_path).stdout.decode()
    assert "La tabla customer tiene campos @private y la base de datos no tiene clave" in out


REPORTED = APP + """
type Contact:
    phone: str @personal
"""

REPORT = """Datos personales de app.nexo:

Contact: no se guarda en una base de datos
  phone  personal
  se guarda: (sin declarar)
  finalidad: (sin declarar)
  lo leen: el programa

Customer: tabla customer en shop.db
  name         personal
  email        personal
  national_id  sensible, cifrado en reposo
  se guarda: 5 years after last_activity
  finalidad: billing and shipping
  lo leen: el programa
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_report_and_lint(phase, tmp_path):
    (tmp_path / "app.nexo").write_text(REPORTED, encoding="utf-8")
    proc = run(phase[1], ["privacy", "report"], tmp_path)
    assert proc.stdout.decode() == REPORT
    assert not (tmp_path / "shop.db").exists()
    lint = run(phase[1], ["lint", "app.nexo"], tmp_path).stdout.decode()
    assert lint.count('El type "Contact" tiene datos personales') == 2
    assert "Avisos: 2." in lint


def test_release_build_needs_keep_and_purpose(tmp_path):
    nexo = compiler()
    if nexo is None:
        pytest.skip("the compiler is not available")
    (tmp_path / "app.nexo").write_text('type Contact:\n    phone: str @personal\n    purpose = "calls"\nprint(1)\n',
                                       encoding="utf-8")
    proc = run([str(nexo)], ["build", "--release", "app.nexo"], tmp_path)
    assert proc.returncode == 1
    assert 'El type "Contact" tiene datos personales sin keep.' in proc.stdout.decode()
