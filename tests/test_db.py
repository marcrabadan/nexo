"""nexo.db (D100, D101) in both phases: the SQL each phase sends is the
same, and `nexo db diff` and `nexo db migrate` give the same files."""
import shutil
import sqlite3
from pathlib import Path

import pytest

from phases import commands, run

CONFORMANCE = Path(__file__).resolve().parent / "conformance"


def sql_log(phase, tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    shutil.copy(CONFORMANCE / "s06_db.nexo", work)
    proc = run(phase[1], ["s06_db.nexo"], work, env={"NEXO_SQL_LOG": "1"})
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
