"""nexo.notify (section 15, D209) in both phases: mail handed over to a
mail server written for the tests (tests/smtp_server.py), messages of
Slack to a local stand-in for its Web API, `notify` picking one of the
two, and what each says when it is not set up or is refused."""
import base64
import json
import threading
from email import message_from_string
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import smtp_server
from phases import commands, run

PHASES = commands()


def slack_api(answers):
    """A stand-in for https://slack.com/api: it keeps what it is sent and
    answers what it was told to, in order (then ok)."""
    seen: list = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"path": self.path, "auth": self.headers["authorization"], "body": body})
            out = json.dumps(answers.pop(0) if answers else {"ok": True}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], seen


def project(folder, smtp, extra=""):
    (folder / "nexo.toml").write_text(f'''[notify]
smtp = "{smtp}"   # where the mail is handed over
from = "shop@example.org"
key = "MAIL_PASS"
slack = "SLACK_TOKEN"
{extra}''')


PROGRAM = '''use nexo.notify

send_email("ana@mail.com", "Thank you for your purchase!")
send_email("luis@mail.com", "Hola Luis,\\ntu pedido está en camino. LONG", subject: "Pedido  nº 7: en camino, llegará mañana por la mañana")
notify("ana@mail.com", "The route changed\\nAS64666 announces 198.51.100.0/24")
notify("#network", "The route changed")
slack("#deploys", "v1.2.0 is in production ✓")
for to in ["gone@mail.com", "nobody", "#closed"]:
    try:
        notify(to, "hello")
        print("sent to {to}")
    else e:
        print("{e.kind}: {e.message}")
print("done")
'''.replace("LONG", "Gracias. " * 20)


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_mail_and_slack(phase, tmp_path):
    mail = smtp_server.start(users={"shop": "pw"}, refuse=["gone@mail.com"])
    port, seen = slack_api([{"ok": True}, {"ok": True}, {"ok": False, "error": "channel_not_found"}])
    project(tmp_path, f"smtp://shop@localhost:{mail.port}")
    (tmp_path / "p.nexo").write_text(PROGRAM)
    env = {"MAIL_PASS": "pw", "SLACK_TOKEN": "xoxb-test", "NEXO_SLACK_URL": f"http://127.0.0.1:{port}"}
    r = run(phase[1], ["p.nexo"], tmp_path, env=env)
    assert r.stdout.decode().splitlines() == [
        "NetworkError: localhost did not take the mail for gone@mail.com: it answered 550 5.1.1 no such user here",
        'InvalidValue: "nobody" is neither an email address nor a #channel of Slack',
        "NetworkError: Slack did not take the message for #closed: channel_not_found",
        "done"]
    assert r.returncode == 0
    assert [(m["from"], m["to"], m["auth"]) for m in mail.messages] == [
        ("shop@example.org", "ana@mail.com", "shop"), ("shop@example.org", "luis@mail.com", "shop"),
        ("shop@example.org", "ana@mail.com", "shop")]
    first, second, third = [message_from_string(m["data"]) for m in mail.messages]
    assert first["From"] == "shop@example.org" and first["To"] == "ana@mail.com"
    assert first["Subject"] == "Thank you for your purchase!"
    assert first["Content-Type"] == "text/plain; charset=utf-8" and first["MIME-Version"] == "1.0"
    assert first["Date"].endswith("+0000") and first["Message-ID"].endswith("@example.org>")
    assert base64.b64decode(first.get_payload()).decode() == "Thank you for your purchase!"
    # a subject that is not ASCII goes in encoded words, a long text in lines of 76
    assert str(make_header(decode_header(second["Subject"]))) == "Pedido nº 7: en camino, llegará mañana por la mañana"
    assert all(len(line) <= 76 for line in mail.messages[1]["data"].split("\r\n"))
    assert base64.b64decode(second.get_payload()).decode() == "Hola Luis,\ntu pedido está en camino. " + "Gracias. " * 20
    assert third["Subject"] == "The route changed"
    assert [(s["path"], s["auth"], s["body"]) for s in seen] == [
        ("/chat.postMessage", "Bearer xoxb-test", {"channel": "#network", "text": "The route changed"}),
        ("/chat.postMessage", "Bearer xoxb-test", {"channel": "#deploys", "text": "v1.2.0 is in production ✓"}),
        ("/chat.postMessage", "Bearer xoxb-test", {"channel": "#closed", "text": "hello"})]


FAILING = '''for to in ["ana@mail.com", "#network"]:
    try:
        notify(to, "hello")
        print("sent to {to}")
    else e:
        print("{e.kind}: {e.message}")
'''


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_a_wrong_password_and_a_missing_token(phase, tmp_path):
    mail = smtp_server.start(users={"shop": "pw"})
    project(tmp_path, f"smtp://shop@localhost:{mail.port}")
    (tmp_path / "p.nexo").write_text(FAILING)
    r = run(phase[1], ["p.nexo"], tmp_path, env={"MAIL_PASS": "wrong"})
    assert r.stdout.decode().splitlines()[-2:] == [
        "NetworkError: localhost did not take the mail for ana@mail.com: it answered 535 5.7.8 the password is wrong",
        'NotFound: The secret "SLACK_TOKEN" is not set']
    assert mail.messages == []


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_without_settings_and_with_wrong_ones(phase, tmp_path):
    (tmp_path / "p.nexo").write_text(FAILING)
    r = run(phase[1], ["p.nexo"], tmp_path)
    assert r.stdout.decode().splitlines() == [
        "NetworkError: There is no mail server to hand the mail for ana@mail.com over to",
        "NetworkError: There is no token to send #network a message with"]
    # a server on another machine is reached with TLS only, and nobody listens on port 1
    for smtp, said in [("smtp://mail.example.org", "mail.example.org is reached with smtps:// only"),
                       ("smtp://localhost:1", "cannot reach localhost:1: Connection refused")]:
        project(tmp_path, smtp)
        r = run(phase[1], ["p.nexo"], tmp_path, env={"SLACK_TOKEN": "x", "NEXO_SLACK_URL": "http://127.0.0.1:1"})
        host = smtp.split("//")[1].split(":")[0]
        assert r.stdout.decode().splitlines()[0] == f"NetworkError: {host} did not take the mail for ana@mail.com: {said}"
    project(tmp_path, "mail.example.org")
    r = run(phase[1], ["p.nexo"], tmp_path, env={"SLACK_TOKEN": "x", "NEXO_SLACK_URL": "http://127.0.0.1:1"})
    assert r.stdout.decode().splitlines()[0] == (
        'InvalidValue: "mail.example.org" is not a mail server: write smtps://user@host, or smtp://localhost:25 on this machine')


@pytest.mark.parametrize("phase", PHASES, ids=lambda c: c[0])
def test_the_policy_checks_the_mail_server(phase, tmp_path):
    mail = smtp_server.start()
    project(tmp_path, f"smtp://localhost:{mail.port}")
    (tmp_path / "p.nexo").write_text('policy:\n    deny net "localhost"\n\nsend_email("ana@mail.com", "hello")\n')
    r = run(phase[1], ["p.nexo"], tmp_path)
    assert r.returncode == 1 and "policy" in r.stdout.decode()
    assert mail.messages == []
