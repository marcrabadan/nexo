"""The program policy (section 9, D142): sandbox escape tests in both
phases, and the release rules of the compiler. A program built with
`nexo build --release` must stop at every access its policy does not
allow, however the access is reached; development mode warns instead."""
import http.server
import os
import threading

import pytest

from phases import commands, compiler, run

ESCAPES = """use lib.files

policy:
    allow read "data/"
    allow write "data/"
    allow net "127.0.0.1"
    deny shell

for path in ["data/ok.txt", "data/link.txt", "data/up/secret.txt", "data/../secret.txt", "{ROOT}/secret.txt"]:
    print(read(path))
print(files.grab())
"""


def tree(tmp_path, program):
    (tmp_path / "data").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "secret.txt").write_text("top\n")
    (tmp_path / "data" / "ok.txt").write_text("ok\n")
    os.symlink("../secret.txt", tmp_path / "data" / "link.txt")
    os.symlink("..", tmp_path / "data" / "up")
    (tmp_path / "lib" / "files.nexo").write_text('pub fn grab() -> str!:\n    return read("secret.txt")?\n')
    (tmp_path / "app.nexo").write_text(program.replace("{ROOT}", str(tmp_path)))


def release(tmp_path):
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    proc = run([str(nexo)], ["build", "--release", "app.nexo", "-o", "app"], tmp_path, timeout=600)
    assert proc.returncode == 0, proc.stdout.decode()
    return run([str(tmp_path / "app")], [], tmp_path)


@pytest.mark.parametrize("name,prefix", commands(), ids=[n for n, _ in commands()])
def test_development_mode_warns_about_every_way_out(tmp_path, name, prefix):
    tree(tmp_path, ESCAPES)
    out = run(prefix, ["app.nexo"], tmp_path).stdout.decode()
    # each way out is allowed and reported once, with the rule to copy
    for shown in ("data/link.txt", "data/up/secret.txt", "secret.txt", f"{tmp_path}/secret.txt"):
        assert f'Add allow read "{shown}" to the policy block.' in out
    assert out.count("Warning in") == 4
    assert out.count("top\n") == 5


@pytest.mark.parametrize("path", ["data/link.txt", "data/up/secret.txt", "data/../secret.txt", "{ROOT}/secret.txt"])
def test_release_build_stops_every_way_out(tmp_path, path):
    program = ESCAPES.replace('["data/ok.txt", "data/link.txt", "data/up/secret.txt", "data/../secret.txt", "{ROOT}/secret.txt"]',
                              f'["data/ok.txt", "{path}"]')
    tree(tmp_path, program)
    proc = release(tmp_path)
    out = proc.stdout.decode()
    assert proc.returncode == 1
    assert out.startswith("ok\n")
    assert "top\n" not in out
    assert "The policy does not allow reading" in out


def test_release_build_stops_a_module(tmp_path):
    tree(tmp_path, ESCAPES.replace(', "data/link.txt", "data/up/secret.txt", "data/../secret.txt", "{ROOT}/secret.txt"', ""))
    proc = release(tmp_path)
    out = proc.stdout.decode()
    assert proc.returncode == 1 and "top\n" not in out
    assert "Error in lib/files.nexo, line 2:" in out
    assert 'The policy does not allow reading "secret.txt".' in out


def test_release_build_stops_writes_env_shell_and_python(tmp_path):
    for body, expected in [
        ('write("../out.txt", "x")', 'The policy does not allow writing "../out.txt".'),
        ('move_file("data/ok.txt", "elsewhere")', 'The policy does not allow writing "elsewhere".'),
        ('use nexo.db\ndb = connect("other.db")', 'The policy does not allow reading "other.db".'),
        ('use nexo.db\ndb = connect(":memory:", key: secret("HOME"))', "The policy does not allow reading the environment variable HOME."),
        ('print(run("echo hi"))', 'The policy denies running the shell command "echo hi" (deny shell).'),
        ("use python json", "The policy does not allow importing the Python module json."),
    ]:
        for f in tmp_path.iterdir():
            if f.is_dir() and not f.is_symlink():
                for g in f.iterdir():
                    g.unlink()
                f.rmdir()
            else:
                f.unlink()
        tree(tmp_path, 'policy:\n    allow read "data/"\n    allow write "data/"\n    deny shell\n\n' + body + "\n")
        proc = release(tmp_path)
        out = proc.stdout.decode()
        assert proc.returncode == 1, out
        assert expected in out
        assert not (tmp_path / "out.txt").exists() and not (tmp_path.parent / "out.txt").exists()
        assert (tmp_path / "data" / "ok.txt").exists() and not (tmp_path / "other.db").exists()


class Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/away":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/inside")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"reached")

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_port
    s.shutdown()


def test_release_build_checks_every_redirect(tmp_path, server):
    tree(tmp_path, f'policy:\n    allow net "127.0.0.1"\n\n'
                   f'print(http.get("http://127.0.0.1:{server}/inside").text)\n'
                   f'print(http.get("http://127.0.0.1:{server}/away", retries: 0).text)\n')
    proc = release(tmp_path)
    out = proc.stdout.decode()
    assert proc.returncode == 1
    assert out.startswith("reached\n") and out.count("reached") == 1
    assert "The policy does not allow connecting to localhost." in out


@pytest.mark.parametrize("name,prefix", commands(), ids=[n for n, _ in commands()])
def test_redirects_are_reported_in_development_mode(tmp_path, server, name, prefix):
    tree(tmp_path, f'policy:\n    allow net "127.0.0.1"\n\nprint(http.get("http://127.0.0.1:{server}/away").text)\n')
    out = run(prefix, ["app.nexo"], tmp_path).stdout.decode()
    assert "Add allow net \"localhost\" to the policy block." in out
    assert out.endswith("reached\n")


def test_release_build_needs_a_policy(tmp_path):
    nexo = compiler()
    if nexo is None:
        pytest.skip("needs cargo and clang")
    (tmp_path / "app.nexo").write_text('print("hi")\n')
    proc = run([str(nexo)], ["build", "--release", "app.nexo"], tmp_path)
    assert proc.returncode == 1
    assert proc.stdout.decode() == ("Error: A release build needs a policy block: it is what the program is "
                                    "allowed to do. Add one; nexo policy suggest proposes it from a test run.\n")
    # a development build does not
    assert run([str(nexo)], ["build", "app.nexo"], tmp_path).returncode == 0


def test_release_build_runs_what_the_policy_allows(tmp_path):
    tree(tmp_path, 'policy:\n    allow read "data/ok.txt"\n    allow write "data/"\n\n'
                   'write("data/new.txt", read("data/ok.txt"))\nprint(read("data/new.txt"))\n')
    proc = release(tmp_path)
    assert proc.returncode == 1
    assert 'The policy does not allow reading "data/new.txt".' in proc.stdout.decode()
    tree_ok = tmp_path / "ok"
    tree_ok.mkdir()
    tree(tree_ok, 'policy:\n    allow read "data/"\n    allow write "data/"\n\n'
                  'write("data/new.txt", read("data/ok.txt"))\nprint(read("data/new.txt"))\n')
    proc = release(tree_ok)
    assert proc.returncode == 0
    assert proc.stdout.decode() == "ok\n\n"


@pytest.mark.parametrize("name,prefix", commands(), ids=[n for n, _ in commands()])
def test_suggest_then_release(tmp_path, name, prefix):
    """The block nexo policy suggest prints is enough for a release build."""
    program = ('fn copy() -> str!:\n    text = read("data/ok.txt")?\n    write("data/copy.txt", text)?\n    return text\n\n'
               'test "copies":\n    assert copy() == "ok\\n"\n\nprint(copy())\n')
    tree(tmp_path, program)
    proc = run(prefix, ["policy", "suggest", "app.nexo"], tmp_path)
    out = proc.stdout.decode()
    assert proc.returncode == 0, out
    assert out == 'policy:\n    allow read "data/ok.txt"\n    allow write "data/copy.txt"\n'
    if name == "compiler":
        (tmp_path / "app.nexo").write_text(out + "\n" + program)
        proc = release(tmp_path)
        assert proc.returncode == 0, proc.stdout.decode()
        assert proc.stdout.decode() == "ok\n\n"
