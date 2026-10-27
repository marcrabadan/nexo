"""nexo.db (D100, D101) in both phases: the SQL each phase sends is the
same, and `nexo db diff` and `nexo db migrate` give the same files."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from phases import commands, run

CONFORMANCE = Path(__file__).resolve().parent / "conformance"


def sql_log(phase, tmp_path, name="s06_db.nexo"):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    shutil.copy(CONFORMANCE / name, work)
    proc = run(phase[1], [name], work, env={"NEXO_SQL_LOG": "1"})
    return [line for line in proc.stdout.decode().splitlines() if line.startswith("sql: ")]


def test_both_phases_send_the_same_sql(tmp_path):
    phases = commands()
    if len(phases) < 2:
        pytest.skip("the compiler is not available")
    logs = [sql_log(p, tmp_path / p[0]) for p in phases]
    assert logs[0] == logs[1]
    # the pipeline of section 6 runs in the database
    assert ('sql: SELECT "id", "customer_id", "amount", "date" FROM "order" WHERE ("amount" > ?) '
            'ORDER BY "date" DESC, "id" LIMIT 20 [100]') in logs[0]


def test_group_and_pick_run_in_sql(tmp_path):
    phases = commands()
    logs = [sql_log(p, tmp_path / p[0], "s06_db_group.nexo") for p in phases]
    if len(logs) == 2:
        assert logs[0] == logs[1]
    log = logs[0]
    assert ('sql: SELECT "region", COUNT(*), SUM("units"), MIN("units"), MAX("units") FROM "sale" '
            'WHERE ("units" > ?) GROUP BY "region" ORDER BY MIN("id") [1]') in log
    assert 'sql: SELECT "region", "units" FROM "sale" ORDER BY "id" LIMIT 2 []' in log
    # a sum that may overflow reads the rows and adds them in memory
    assert log[-1] == 'sql: SELECT "id", "region", "units", "price", "kind", "note", "extra" FROM "sale" ORDER BY "id" []'


APP = """use nexo.db
db = connect("shop.db")
@table
type Customer:
    id: id
    name: str
    email: str @unique
print("this does not run")
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_diff_and_migrate(phase, tmp_path):
    (tmp_path / "app.nexo").write_text(APP, encoding="utf-8")
    out = []

    def nexo(*args):
        proc = run(phase[1], list(args), tmp_path)
        out.append(proc.stdout.decode())
        return proc.returncode

    assert nexo("db", "diff", "app.nexo") == 0
    assert nexo("db", "migrate", "app.nexo") == 0
    assert nexo("db", "diff", "app.nexo") == 0
    with sqlite3.connect(tmp_path / "shop.db") as c:
        c.execute("INSERT INTO customer (name, email) VALUES ('Ana', 'a@x')")
    (tmp_path / "app.nexo").write_text(APP.replace("    email: str @unique\n", "    phone: str?\n"), encoding="utf-8")
    assert nexo("db", "diff", "app.nexo") == 0
    assert nexo("db", "migrate", "app.nexo") == 1
    assert nexo("db", "migrate", "app.nexo", "--allow-drop") == 0
    assert out[0].startswith("Migration written to ./migrations/0001.sql:\n")
    assert "this does not run" not in "".join(out)
    assert out[2] == "shop.db already matches the types of app.nexo.\n"
    assert 'CREATE TABLE "customer_new"' in out[3]
    assert "drops columns or tables" in out[4]
    with sqlite3.connect(tmp_path / "shop.db") as c:
        assert list(c.execute("SELECT * FROM customer")) == [(1, "Ana", None)]
        assert [r[0] for r in c.execute("SELECT name FROM nexo_migrations")] == ["0001.sql", "0002.sql"]


def test_relations_run_in_sql(tmp_path):
    phases = commands()
    logs = [sql_log(p, tmp_path / p[0], "s06_db_relations.nexo") for p in phases]
    if len(logs) == 2:
        assert logs[0] == logs[1]
    log = logs[0]
    assert ('sql: SELECT "id", "customer_id", "amount" FROM "order" WHERE ("customer_id" IN '
            '(SELECT "id" FROM "customer" WHERE "name" = ?)) ORDER BY "id" [\'Ana\']') in log
    # the customers of the five orders, and their bosses (among them): one
    # statement, not one per order
    i = log.index('sql: SELECT "id", "customer_id", "amount" FROM "order" ORDER BY "id" []')
    assert log[i + 1] == ('sql: SELECT "id", "name", "vip", "boss_id" FROM "customer" WHERE ("id" IN (?, ?, ?)) '
                          'ORDER BY "id" [1, 2, 3]')
    assert log[i + 2].startswith("sql: SELECT COUNT(*)")
