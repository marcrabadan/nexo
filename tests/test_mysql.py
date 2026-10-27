"""nexo.db on MySQL (D170) in both phases, against a server started for the
tests with TLS (skipped when MySQL is not installed): the programs of the
db conformance cases give the same output as on SQLite, both phases send
the same SQL, and privacy, transactions, migrations and TLS checks behave
alike."""
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from phases import commands, run
from test_postgres import CASES, CONFORMANCE, SCHEMA, TRANSACTION, certificates, free_port

PASSWORD = "nexo-test-pass"


@pytest.fixture(scope="module")
def server():
    """(port, CA file, mysql runner) of a server with TLS."""
    mysqld = shutil.which("mysqld") or ("/usr/sbin/mysqld" if os.path.exists("/usr/sbin/mysqld") else None)
    if mysqld is None or shutil.which("mysql") is None:
        pytest.skip("MySQL is not installed")
    # mysqld refuses to run as root: the mysql user runs the server then
    as_user = []
    if os.geteuid() == 0:
        if shutil.which("runuser") is None:
            pytest.skip("cannot run MySQL as root")
        as_user = ["runuser", "-u", "mysql", "--"]
    folder = Path(tempfile.mkdtemp(prefix="nexo-mysql-"))
    certificates(folder)
    if as_user:
        shutil.chown(folder, "mysql")
        for f in folder.iterdir():
            shutil.chown(f, "mysql")
        os.chmod(folder / "ca.pem", 0o644)
    port = free_port()
    data = folder / "data"
    subprocess.run(as_user + [mysqld, "--no-defaults", "--initialize-insecure", f"--datadir={data}"],
                   check=True, capture_output=True, timeout=300)
    proc = subprocess.Popen(as_user + [
        mysqld, "--no-defaults", f"--datadir={data}", f"--port={port}", "--bind-address=127.0.0.1",
        f"--socket={folder / 'sock'}", "--mysqlx=OFF", "--secure-file-priv=", f"--log-error={folder / 'err.log'}",
        f"--pid-file={folder / 'pid'}", f"--ssl-ca={folder / 'ca.pem'}", f"--ssl-cert={folder / 'server.crt'}",
        f"--ssl-key={folder / 'server.key'}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def mysql(sql: str, user: str = "nexo", database: str | None = None) -> str:
        args = ["mysql", "-N", "-B", "-h", "127.0.0.1", "-P", str(port), "-u", user]
        if database:
            args.append(database)
        env = dict(os.environ, MYSQL_PWD=PASSWORD if user == "nexo" else "")
        return subprocess.run(args + ["-e", sql], env=env, check=True, capture_output=True, text=True).stdout

    try:
        end = time.time() + 120
        while True:
            try:
                mysql(f"CREATE USER 'nexo'@'%' IDENTIFIED BY '{PASSWORD}'; GRANT ALL ON *.* TO 'nexo'@'%'",
                      user="root")
                break
            except subprocess.CalledProcessError:
                if time.time() > end or proc.poll() is not None:
                    raise
                time.sleep(0.5)
        yield port, folder / "ca.pem", mysql
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(folder, ignore_errors=True)


def fresh(server, name: str) -> str:
    """A new, empty database; its address."""
    port, _, mysql = server
    mysql(f"DROP DATABASE IF EXISTS `{name}`; CREATE DATABASE `{name}`")
    return f"mysql://nexo@localhost:{port}/{name}"


def env_of(server) -> dict:
    return {"MYPASS": PASSWORD, "SSL_CERT_FILE": str(server[1])}


def on_server(source: str, url: str) -> str:
    """A program with its connect("...") pointed at the server."""
    return re.sub(r'connect\("[^"]*"\)', f'connect("{url}", key: secret("MYPASS"))', source)


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_conformance_cases_on_mysql(server, phase, name, tmp_path):
    url = fresh(server, f"{phase[0]}_{name}")
    (tmp_path / f"{name}.nexo").write_text(on_server((CONFORMANCE / f"{name}.nexo").read_text(), url))
    proc = run(phase[1], [f"{name}.nexo"], tmp_path, env=env_of(server))
    assert proc.stdout.decode() == (CONFORMANCE / f"{name}.expected").read_text()


def test_both_phases_send_the_same_sql(server, tmp_path):
    phases = commands()
    if len(phases) < 2:
        pytest.skip("the compiler is not available")
    logs = []
    for p in phases:
        work = tmp_path / p[0]
        work.mkdir()
        log = []
        for name in CASES:
            url = fresh(server, f"same_sql_{name}")
            (work / f"{name}.nexo").write_text(on_server((CONFORMANCE / f"{name}.nexo").read_text(), url))
            proc = run(p[1], [f"{name}.nexo"], work, env=dict(env_of(server), NEXO_SQL_LOG="1"))
            log += [line for line in proc.stdout.decode().splitlines() if line.startswith("sql: ")]
        logs.append(log)
    assert logs[0] == logs[1]
    log = logs[0]
    assert ('sql: SELECT "id", "customer_id", "amount", "date" FROM "order" WHERE ("amount" > ?) '
            'ORDER BY "date" DESC, "id" LIMIT 20 [100]') in log
    assert any('CONSTRAINT "fk_order_customer_id" FOREIGN KEY ("customer_id") REFERENCES "customer" ("id")' in line
               for line in log)


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_failed_statement_leaves_the_transaction_as_in_sqlite(server, phase, tmp_path):
    (tmp_path / "local.nexo").write_text(TRANSACTION)
    expected = run(phase[1], ["local.nexo"], tmp_path).stdout.decode()
    url = fresh(server, f"{phase[0]}_tx")
    (tmp_path / "server.nexo").write_text(on_server(TRANSACTION, url))
    assert run(phase[1], ["server.nexo"], tmp_path, env=env_of(server)).stdout.decode() == expected


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_privacy_on_mysql(server, phase, tmp_path):
    import test_privacy
    url = fresh(server, f"{phase[0]}_privacy")
    app = test_privacy.APP.replace('connect("shop.db", key: secret("DB_KEY"))', f'connect("{url}", key: secret("DB_KEY"))')
    (tmp_path / "app.nexo").write_text(app)
    proc = run(phase[1], ["app.nexo"], tmp_path, env={"DB_KEY": PASSWORD, "SSL_CERT_FILE": str(server[1])})
    assert proc.stdout.decode() == test_privacy.OUT
    # @private columns are encrypted at rest
    stored = server[2]("SELECT national_id FROM customer", database=url.rsplit("/", 1)[1])
    assert stored.startswith("nexo:") and "123A" not in stored


REFS = SCHEMA.replace("print(", """@table
type Order:
    id: id
    customer: ref Customer
    amount: int
print(""")


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_diff_and_migrate_on_mysql(server, phase, tmp_path):
    url = fresh(server, f"{phase[0]}_migrate")
    app = tmp_path / "app.nexo"
    app.write_text(on_server(REFS, url))
    env = env_of(server)

    def db(*args):
        return run(phase[1], ["db", *args, "app.nexo"], tmp_path, env=env).stdout.decode()

    out = db("diff")
    assert ('CREATE TABLE "customer" ("id" BIGINT AUTO_INCREMENT PRIMARY KEY, '
            '"name" LONGTEXT COLLATE utf8mb4_0900_bin NOT NULL, '
            '"email" VARCHAR(768) COLLATE utf8mb4_0900_bin NOT NULL UNIQUE);') in out
    assert ('CREATE TABLE "order" ("id" BIGINT AUTO_INCREMENT PRIMARY KEY, "customer_id" BIGINT NOT NULL, '
            '"amount" BIGINT NOT NULL, CONSTRAINT "fk_order_customer_id" FOREIGN KEY ("customer_id") '
            'REFERENCES "customer" ("id"));') in out
    assert "Applied" in db("migrate")
    assert "already matches" in db("diff")
    app.write_text(app.read_text().replace("    email: str @unique", "    age: int?")
                   .replace("    customer: ref Customer\n", ""))
    out = db("diff")
    assert 'ALTER TABLE "customer" ADD COLUMN "age" BIGINT;' in out
    assert 'ALTER TABLE "customer" DROP COLUMN "email";' in out
    assert ('ALTER TABLE "order" DROP FOREIGN KEY "fk_order_customer_id";\n'
            'ALTER TABLE "order" DROP COLUMN "customer_id";') in out
    assert "--allow-drop" in db("migrate")
    assert "Applied" in db("migrate", "--allow-drop")
    assert "already matches" in db("diff")
    columns = server[2]("SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer' "
                        "ORDER BY ordinal_position", database=url.rsplit("/", 1)[1])
    assert columns.split() == ["id", "name", "age"]


FAILURES = """use nexo.db
for url in ["mysql://nexo@localhost:{port}/mysql", "mysql://nexo@127.0.0.1:{port}/mysql", "mysql://nexo:pw@localhost/shop", "mysql://nexo@localhost:{port}"]:
    try:
        db = connect(url, key: secret("MYPASS"))
        print(db.sql("SELECT 1 AS one, 'a' || 'b' AS ab"))
    else e:
        print(e.message)
"""


def test_tls_and_addresses_are_checked_alike(server, tmp_path):
    port = server[0]
    (tmp_path / "f.nexo").write_text(FAILURES.replace("{port}", str(port)))
    outs = []
    for p in commands():
        # without the certificate authority: refused; with it, only the name
        # in the certificate (localhost) is trusted
        a = run(p[1], ["f.nexo"], tmp_path, env={"MYPASS": PASSWORD}).stdout.decode()
        b = run(p[1], ["f.nexo"], tmp_path, env=env_of(server)).stdout.decode()
        outs.append((a, b))
    a, b = outs[0]
    assert a.splitlines()[0].endswith("TLS with localhost: the certificate is not trusted")
    assert b.splitlines() == [
        '[{"one": 1, "ab": "ab"}]',
        f"Database error in mysql://nexo@127.0.0.1:{port}/mysql: TLS with 127.0.0.1: the certificate is not trusted",
        "Database error in mysql://nexo:pw@localhost/shop: the address holds a password; give it with key: instead",
        f"Database error in mysql://nexo@localhost:{port}: the address names no database: "
        "mysql://user@host/database",
    ]
    assert all(o == outs[0] for o in outs)


WRONG = """use nexo.db
try:
    db = connect("mysql://nexo@localhost:{port}/mysql", key: secret("MYPASS"))
else e:
    print(e.message)
"""


def test_a_wrong_password_is_the_servers_answer(server, tmp_path):
    (tmp_path / "w.nexo").write_text(WRONG.replace("{port}", str(server[0])))
    outs = [run(p[1], ["w.nexo"], tmp_path, env=dict(env_of(server), MYPASS="wrong")).stdout.decode()
            for p in commands()]
    assert "Access denied for user 'nexo'" in outs[0]
    assert all(o == outs[0] for o in outs)
