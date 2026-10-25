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
    assert "cannot be decrypted with this key" in proc.stdout.decode()


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
    assert "The table customer has @private fields and the database has no key" in out


REPORTED = APP + """
type Contact:
    phone: str @personal

infra shop:
    cloud = "aws"
    region = "eu-west-1"
    identity api_id:
        can read database customers
    network net
    database customers
"""

REPORT = """Personal data of app.nexo:

Contact: not stored in a database
  phone  personal
  kept: (not declared)
  purpose: (not declared)
  read by: the program

Customer: table customer in shop.db
  name         personal
  email        personal
  national_id  sensitive, encrypted at rest
  kept: 5 years after last_activity
  purpose: billing and shipping
  read by: the program, api_id (infra shop, database customers)
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_report_and_lint(phase, tmp_path):
    (tmp_path / "app.nexo").write_text(REPORTED, encoding="utf-8")
    proc = run(phase[1], ["privacy", "report"], tmp_path)
    assert proc.stdout.decode() == REPORT
    assert not (tmp_path / "shop.db").exists()
    lint = run(phase[1], ["lint", "app.nexo"], tmp_path).stdout.decode()
    assert lint.count('The type "Contact" has personal data') == 2
    assert "Avisos: 2." in lint


def test_release_build_needs_keep_and_purpose(tmp_path):
    nexo = compiler()
    if nexo is None:
        pytest.skip("the compiler is not available")
    (tmp_path / "app.nexo").write_text('type Contact:\n    phone: str @personal\n    purpose = "calls"\nprint(1)\n',
                                       encoding="utf-8")
    proc = run([str(nexo)], ["build", "--release", "app.nexo"], tmp_path)
    assert proc.returncode == 1
    assert 'The type "Contact" has personal data without keep.' in proc.stdout.decode()


UNIQUE = """use nexo.db

db = connect("shop.db", key: secret("DB_KEY"))

@table
type Customer:
    id: id
    name: str @personal
    email: str @personal @unique
    code: int @private @unique
    since: date
    orders: many Order

    keep = 5 years after since
    purpose = "billing"

@table
type Order:
    id: id
    customer: ref Customer
    total: int

ana = db.Customer.add(name: "Ana", email: "ana@x.com", code: 11, since: today)
luis = db.Customer.add(name: "Luis", email: "luis@x.com", code: 22, since: today)
db.Order.add(customer: ana, total: 30)
db.Order.add(customer: luis, total: 40)
privacy.erase(ana)
privacy.erase(luis)
print(db.sql("SELECT name, email FROM customer"))
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_erasing_keeps_unique_fields_unique(phase, tmp_path):
    # erased rows that others point to stay, anonymised: a @unique field
    # takes a value made from the row's id, not one shared blank value
    (tmp_path / "app.nexo").write_text(UNIQUE, encoding="utf-8")
    out = run(phase[1], ["app.nexo"], tmp_path, env={"DB_KEY": "k"}).stdout.decode()
    assert out == '[{"name": "", "email": "erased-1"}, {"name": "", "email": "erased-2"}]\n'
