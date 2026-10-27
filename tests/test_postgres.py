"""nexo.db on PostgreSQL (D163) in both phases, against a server started
for the tests with TLS (skipped when PostgreSQL is not installed): the
programs of the db conformance cases give the same output as on SQLite,
both phases send the same SQL, and privacy, transactions, migrations and
TLS checks behave alike."""
import datetime
import glob
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from phases import commands, run

CONFORMANCE = Path(__file__).resolve().parent / "conformance"
PASSWORD = "nexo-test-pass"
CASES = ["s06_db", "s06_db_field", "s06_db_rollback", "s06_db_sql", "s06_db_group", "s06_db_relations"]


def binaries():
    found = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))
    if found:
        return Path(found[-1]).parent
    initdb = shutil.which("initdb")
    return Path(initdb).parent if initdb else None


def certificates(folder: Path) -> None:
    """A certificate authority and a certificate for localhost signed by it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nexo test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (folder / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                          serialization.PrivateFormat.TraditionalOpenSSL,
                                                          serialization.NoEncryption()))
    os.chmod(folder / "server.key", 0o600)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """(port, CA file, psql runner) of a server with TLS and SCRAM."""
    bin_dir = binaries()
    if bin_dir is None:
        pytest.skip("PostgreSQL is not installed")
    # initdb refuses to run as root: the postgres user runs the server then
    as_user = []
    if os.geteuid() == 0:
        if shutil.which("runuser") is None:
            pytest.skip("cannot run PostgreSQL as root")
        as_user = ["runuser", "-u", "postgres", "--"]
    folder = Path(tempfile.mkdtemp(prefix="nexo-pg-"))
    certificates(folder)
    (folder / "pw").write_text(PASSWORD)
    if as_user:
        shutil.chown(folder, "postgres")
        for f in folder.iterdir():
            shutil.chown(f, "postgres")
        os.chmod(folder / "ca.pem", 0o644)
    port = free_port()
    data = folder / "data"
    subprocess.run(as_user + [str(bin_dir / "initdb"), "-D", str(data), "-U", "nexo", f"--pwfile={folder / 'pw'}",
                              "-A", "scram-sha-256"], check=True, capture_output=True, timeout=120)
    options = (f"-p {port} -k {folder} -c listen_addresses=127.0.0.1 -c ssl=on "
               f"-c ssl_cert_file={folder / 'server.crt'} -c ssl_key_file={folder / 'server.key'}")
    subprocess.run(as_user + [str(bin_dir / "pg_ctl"), "-D", str(data), "-o", options, "-l", str(folder / "log"),
                              "-w", "start"], check=True, capture_output=True, timeout=120)

    def psql(sql: str, database: str = "postgres") -> str:
        env = dict(os.environ, PGPASSWORD=PASSWORD, PGSSLMODE="require")
        return subprocess.run(["psql", "-qAt", "-h", "127.0.0.1", "-p", str(port), "-U", "nexo", "-d", database,
                               "-c", sql], env=env, check=True, capture_output=True, text=True).stdout

    try:
        yield port, folder / "ca.pem", psql
    finally:
        subprocess.run(as_user + [str(bin_dir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True, timeout=120)
        shutil.rmtree(folder, ignore_errors=True)


def fresh(server, name: str) -> str:
    """A new, empty database; its address."""
    port, _, psql = server
    psql(f'DROP DATABASE IF EXISTS "{name}"')
    psql(f'CREATE DATABASE "{name}"')
    return f"postgres://nexo@localhost:{port}/{name}"


def env_of(server) -> dict:
    return {"PGPASS": PASSWORD, "SSL_CERT_FILE": str(server[1])}


def on_server(source: str, url: str) -> str:
    """A program with its connect("...") pointed at the server."""
    import re
    return re.sub(r'connect\("[^"]*"\)', f'connect("{url}", key: secret("PGPASS"))', source)


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_conformance_cases_on_postgres(server, phase, name, tmp_path):
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
    assert ('sql: SELECT "id", "customer_id", "amount", "date" FROM "order" WHERE ("amount" > $1) '
            'ORDER BY "date" DESC NULLS LAST, "id" LIMIT 20 [100]') in log
    assert any(line.startswith('sql: INSERT INTO "customer" ("name", "email") VALUES ($1, $2) RETURNING "id"')
               for line in log)


TRANSACTION = """use nexo.db

db = connect("x.db")

@table
type Item:
    id: id
    name: str @unique

db.Item.add(name: "a")
db.transaction:
    try:
        db.Item.add(name: "a")
    else e:
        print(e.message)
    db.Item.add(name: "b")
print(db.Item |> map name)
try:
    db.transaction:
        db.Item.add(name: "c")
        db.Item.add(name: "a")
else e:
    print(e.kind)
print(db.Item |> sort by name desc |> map name, db.Item.count())
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_a_failed_statement_leaves_the_transaction_as_in_sqlite(server, phase, tmp_path):
    (tmp_path / "local.nexo").write_text(TRANSACTION)
    expected = run(phase[1], ["local.nexo"], tmp_path).stdout.decode()
    assert expected == 'There is already a Item with name = "a"\n["a", "b"]\nDatabaseError\n["b", "a"] 2\n'
    url = fresh(server, f"{phase[0]}_tx")
    (tmp_path / "server.nexo").write_text(on_server(TRANSACTION, url))
    assert run(phase[1], ["server.nexo"], tmp_path, env=env_of(server)).stdout.decode() == expected


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_privacy_on_postgres(server, phase, tmp_path):
    import test_privacy
    url = fresh(server, f"{phase[0]}_privacy")
    app = test_privacy.APP.replace('connect("shop.db", key: secret("DB_KEY"))', f'connect("{url}", key: secret("DB_KEY"))')
    (tmp_path / "app.nexo").write_text(app)
    proc = run(phase[1], ["app.nexo"], tmp_path, env={"DB_KEY": PASSWORD, "SSL_CERT_FILE": str(server[1])})
    assert proc.stdout.decode() == test_privacy.OUT
    # @private columns are encrypted at rest
    stored = server[2]("SELECT national_id FROM customer", url.rsplit("/", 1)[1])
    assert stored.startswith("nexo:") and "123A" not in stored


SCHEMA = """use nexo.db
db = connect("x.db")
@table
type Customer:
    id: id
    name: str
    email: str @unique
print("this does not run")
"""


@pytest.mark.parametrize("phase", commands(), ids=lambda c: c[0])
def test_diff_and_migrate_on_postgres(server, phase, tmp_path):
    url = fresh(server, f"{phase[0]}_migrate")
    app = tmp_path / "app.nexo"
    app.write_text(on_server(SCHEMA, url))
    env = env_of(server)

    def db(*args):
        return run(phase[1], ["db", *args, "app.nexo"], tmp_path, env=env).stdout.decode()

    assert ('CREATE TABLE "customer" ("id" BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, '
            '"name" TEXT COLLATE "C" NOT NULL, "email" TEXT COLLATE "C" NOT NULL UNIQUE);') in db("diff")
    assert "Applied" in db("migrate")
    assert "already matches" in db("diff")
    app.write_text(app.read_text().replace("    email: str @unique", "    age: int?"))
    out = db("diff")
    assert 'ALTER TABLE "customer" ADD COLUMN "age" BIGINT;' in out
    assert 'ALTER TABLE "customer" DROP COLUMN "email";' in out
    assert "--allow-drop" in db("migrate")
    assert "Applied" in db("migrate", "--allow-drop")
    columns = server[2]("SELECT column_name FROM information_schema.columns WHERE table_name = 'customer' "
                        "ORDER BY ordinal_position", url.rsplit("/", 1)[1])
    assert columns.split() == ["id", "name", "age"]


FAILURES = """use nexo.db
for url in ["postgres://nexo@localhost:{port}/postgres", "postgres://nexo@127.0.0.1:{port}/postgres", "postgres://nexo:pw@localhost/shop"]:
    try:
        db = connect(url, key: secret("PGPASS"))
        print(db.sql("SELECT 1 AS one"))
    else e:
        print(e.message)
"""


def test_tls_is_checked_alike(server, tmp_path):
    port = server[0]
    (tmp_path / "f.nexo").write_text(FAILURES.replace("{port}", str(port)))
    outs = []
    for p in commands():
        # without the certificate authority: refused; with it, only the name
        # in the certificate (localhost) is trusted
        a = run(p[1], ["f.nexo"], tmp_path, env={"PGPASS": PASSWORD}).stdout.decode()
        b = run(p[1], ["f.nexo"], tmp_path, env=env_of(server)).stdout.decode()
        outs.append((a, b))
    a, b = outs[0]
    assert a.splitlines()[0].endswith("TLS with localhost: the certificate is not trusted")
    assert b.splitlines() == [
        '[{"one": 1}]',
        f"Database error in postgres://nexo@127.0.0.1:{port}/postgres: TLS with 127.0.0.1: the certificate is not trusted",
        "Database error in postgres://nexo:pw@localhost/shop: the address holds a password; give it with key: instead",
    ]
    assert all(o == outs[0] for o in outs)
