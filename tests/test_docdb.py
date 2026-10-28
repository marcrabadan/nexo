"""nexo.db on document stores (D171-D172) in both phases, against the test
servers tests/mongo_server.py and tests/es_server.py: the programs of the
db conformance cases give the same output as on SQLite, both phases send
the same commands, and transactions, privacy, passwords, db.sql and
`nexo db diff` behave alike."""
import re
from pathlib import Path

import pytest

import es_server
import mongo_server
from phases import commands, run
from test_postgres import CASES, TRANSACTION

CONFORMANCE = Path(__file__).resolve().parent / "conformance"
PASSWORD = "nexo-test-pass"
STORES = ["mongo", "elastic"]
RAW_SQL = 'rows = db.sql("SELECT id, amount FROM order WHERE amount > {minimum} ORDER BY amount")\nprint(rows)\n'


@pytest.fixture(scope="module")
def servers():
    return {"mongo": mongo_server.start({"shop": PASSWORD}), "elastic": es_server.start({"elastic": PASSWORD})}


def url_of(servers, store: str, name: str) -> str:
    s = servers[store]
    if store == "mongo":
        return f"mongodb://shop@localhost:{s.port}/{name}"
    return f"elasticsearch://elastic@localhost:{s.port}/{name.lower()}"


def on_store(source: str, url: str) -> str:
    return re.sub(r'connect\("[^"]*"\)', f'connect("{url}", key: secret("DBPASS"))', source)


def program(name: str) -> str:
    """A conformance case without db.sql, which needs an SQL database."""
    return (CONFORMANCE / f"{name}.nexo").read_text().replace(RAW_SQL, "")


def local_output(phase, source: str, folder: Path) -> str:
    """The output of a program on SQLite, in memory."""
    folder.mkdir()
    name = "app.nexo"
    (folder / name).write_text(re.sub(r'connect\("[^"]*"(, key: [^)]*\))?\)', 'connect(":memory:")', source))
    return run(phase[1], [name], folder).stdout.decode()


@pytest.mark.parametrize("name", [c for c in CASES if c != "s06_db_sql"])
@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_conformance_cases_on_document_stores(servers, phase, store, name, tmp_path):
    source = program(name)
    expected = local_output(phase, source, tmp_path / "local")
    (tmp_path / "app.nexo").write_text(on_store(source, url_of(servers, store, f"{phase[0]}_{name}")))
    assert run(phase[1], ["app.nexo"], tmp_path, env={"DBPASS": PASSWORD}).stdout.decode() == expected


@pytest.mark.parametrize("store", STORES)
def test_both_phases_send_the_same_commands(servers, store, tmp_path):
    phases = commands()
    if len(phases) < 2:
        pytest.skip("the compiler is not available")
    logs = []
    for p in phases:
        work = tmp_path / p[0]
        work.mkdir()
        log = []
        for name in [c for c in CASES if c != "s06_db_sql"]:
            (work / f"{name}.nexo").write_text(on_store(program(name), url_of(servers, store, f"same_{p[0]}_{name}")))
            proc = run(p[1], [f"{name}.nexo"], work, env={"DBPASS": PASSWORD, "NEXO_SQL_LOG": "1"})
            # (the ids of the server's cursors are its own)
            log += [re.sub(r'"getMore":\d+', '"getMore":N', line.replace(f"same_{p[0]}_", "same_"))
                    for line in proc.stdout.decode().splitlines() if line.startswith(f"{store}: ")]
        logs.append(log)
    assert logs[0] == logs[1]
    if store == "mongo":
        assert ('mongo: {"find":"order","filter":{"amount":{"$gt":100}},"sort":{"date":-1,"_id":1},"limit":20,'
                '"$db":"same_s06_db"}') in logs[0]
    else:
        assert any(line.startswith('elastic: POST /same_s06_db-order/_search {"query":{"range":{"amount":{"gt":100}}},'
                                   '"sort":[{"date":{"order":"desc","missing":"_last"}},{"id":{"order":"asc","missing":"_first"}}]')
                   for line in logs[0])


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_transaction_is_undone_as_on_sqlite(servers, phase, store, tmp_path):
    expected = local_output(phase, TRANSACTION, tmp_path / "local")
    (tmp_path / "app.nexo").write_text(on_store(TRANSACTION, url_of(servers, store, f"{phase[0]}_tx")))
    assert run(phase[1], ["app.nexo"], tmp_path, env={"DBPASS": PASSWORD}).stdout.decode() == expected


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_privacy_on_document_stores(servers, phase, store, tmp_path):
    import test_privacy
    app = test_privacy.APP.replace('connect("shop.db", key: secret("DB_KEY"))',
                                   f'connect("{url_of(servers, store, phase[0] + "_privacy")}", key: secret("DB_KEY"))')
    # (without its last line, db.sql, which needs an SQL database)
    app = app.replace('print(db.sql("SELECT name FROM customer"))\n', "")
    (tmp_path / "app.nexo").write_text(app)
    proc = run(phase[1], ["app.nexo"], tmp_path, env={"DB_KEY": PASSWORD})
    assert proc.stdout.decode() == test_privacy.OUT.replace('[{"name": "Ana"}]\n', "")


ERRORS = """use nexo.db
try:
    db = connect("{url}", key: secret("DBPASS"))
    print(db.Item.count())
else e:
    print(e.message)
db = connect("{url}", key: secret("GOOD"))
@table
type Item:
    id: id
    name: str
print(db.sql("SELECT 1"))
"""


@pytest.mark.parametrize("store", STORES)
def test_errors_are_the_same_in_both_phases(servers, store, tmp_path):
    (tmp_path / "e.nexo").write_text(ERRORS.replace("{url}", url_of(servers, store, "errors")))
    outs = [run(p[1], ["e.nexo"], tmp_path, env={"DBPASS": "wrong", "GOOD": PASSWORD}).stdout.decode()
            for p in commands()]
    assert all(o == outs[0] for o in outs)
    first, rest = outs[0].split("\n", 1)
    assert first.startswith("Database error in ")
    assert "is not an SQL database: db.sql runs on SQLite, PostgreSQL and MySQL." in rest


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_db_diff_says_there_is_no_schema(servers, phase, store, tmp_path):
    url = url_of(servers, store, "diff")
    (tmp_path / "app.nexo").write_text(f'use nexo.db\ndb = connect("{url}")\n@table\ntype Item:\n    id: id\n')
    out = run(phase[1], ["db", "diff", "app.nexo"], tmp_path).stdout.decode()
    assert out == f"{url} keeps no schema to migrate: its tables and their unique keys are made when a program " \
                  "first uses them.\n"


MANY = """use nexo.db
db = connect("{url}", key: secret("DBPASS"))
@table
type Row:
    id: id
    n: int
for i in range(1, 1206):
    db.Row.add(n: i % 7)
rows = db.Row |> where n > 2 |> sort by n desc
print(len(rows), db.Row.count(), (rows |> take 3 |> map it.id), (rows |> map it.id |> sum))
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_more_rows_than_a_page(servers, phase, tmp_path):
    # Elasticsearch gives 1000 hits a page; MongoDB's test server a few a
    # batch: both read the rest
    for store in STORES:
        work = tmp_path / store
        expected = local_output(phase, MANY, tmp_path / f"local_{store}")
        work.mkdir()
        (work / "app.nexo").write_text(MANY.replace("{url}", url_of(servers, store, f"{phase[0]}_many")))
        assert run(phase[1], ["app.nexo"], work, env={"DBPASS": PASSWORD}).stdout.decode() == expected
