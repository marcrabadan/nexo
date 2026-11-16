"""nexo.notify (section 15, D209): `send_email(to, text, subject:)`,
`slack(channel, text)` and `notify(to, text)`, which picks one of the two
by what `to` looks like. Mail is handed over to the SMTP server of the
`[notify]` table of the project's nexo.toml, spoken here with no library
(RFC 5321, AUTH PLAIN, a text/plain message in base64); Slack takes the
message through its Web API with the token of an app. Under `nexo test`
nothing is sent: each message is a line of the test's output."""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import time as _time
from pathlib import Path

from .. import errors
from ..errors import NexoError
from ..types import Builtin, failure

SLACK = "https://slack.com/api"
DAY_NAMES = ("Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed")  # 1970-01-01 was a Thursday
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
NOT_IN_ADDRESS = '<>(),;:"[]\\'


# ------------------------------------------------------------ settings

def settings(call) -> dict:
    """The `[notify]` table of the nexo.toml of the program's folder or the
    nearest one above: lines `name = "text"`."""
    from .policy import enforcing
    if enforcing():
        return {}
    start = Path(call.span.file).resolve().parent if call.span and call.span.file else Path.cwd()
    for d in [start, *start.parents]:
        path = d / "nexo.toml"
        if path.is_file():
            return read_table(call, path)
    return {}


def read_table(call, path: Path) -> dict:
    from .ai import toml_value
    table: dict = {}
    inside = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return table
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            inside = line.split("#")[0].strip().strip("[]").strip() == "notify"
            continue
        if not inside:
            continue
        key, sep, value = line.partition("=")
        parsed = toml_value(value.strip())
        if not sep or not key.strip() or not isinstance(parsed, str):
            raise NexoError("ai_toml", call.span, path=str(path), line=n)
        table[key.strip()] = parsed
    return table


def secret_of(call, name: str) -> str:
    """The value of the variable a setting names (a key is never written
    in a file, D106)."""
    from .crypto import Secret
    return Secret(name).reveal(call)


def invalid(call, detail: str, **args):
    return failure("InvalidValue", call.span, detail=errors.msg("detail." + detail, **args))


def text_arg(call, fn: str, name: str, value) -> str:
    if not isinstance(value, str):
        from .core import expects
        raise expects(call, fn, "str", value)
    return value


def is_address(text: str) -> bool:
    from .core import EMAIL_RE
    return bool(EMAIL_RE.match(text)) and all(33 <= ord(c) < 127 and c not in NOT_IN_ADDRESS for c in text)


# ---------------------------------------------------------------- mail

def mail_date(seconds: int) -> str:
    """Mon, 16 Nov 2026 10:00:00 +0000"""
    days, rest = divmod(seconds, 86400)
    t = _time.gmtime(seconds)
    return (f"{DAY_NAMES[days % 7]}, {t.tm_mday:02d} {MONTH_NAMES[t.tm_mon - 1]} {t.tm_year} "
            f"{rest // 3600:02d}:{rest // 60 % 60:02d}:{rest % 60:02d} +0000")


def header_text(text: str) -> str:
    """A header's value: as it is when it is plain ASCII, else as encoded
    words of at most 39 bytes each (RFC 2047), so a line stays under 76."""
    if all(32 <= ord(c) < 127 for c in text):
        return text
    words, chunk = [], b""
    for c in text:
        piece = c.encode("utf-8")
        if len(chunk) + len(piece) > 39:
            words.append(chunk)
            chunk = b""
        chunk += piece
    words.append(chunk)
    return "\r\n ".join("=?utf-8?B?" + base64.b64encode(w).decode() + "?=" for w in words)


def message(sender: str, to: str, subject: str, text: str, seconds: int, ident: str) -> bytes:
    body = base64.b64encode(text.encode("utf-8")).decode()
    lines = [f"From: {sender}", f"To: {to}", f"Subject: {header_text(subject)}", f"Date: {mail_date(seconds)}",
             f"Message-ID: <{ident}@{sender.rsplit('@', 1)[-1]}>", "MIME-Version: 1.0",
             "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: base64", ""]
    lines += [body[i:i + 76] for i in range(0, len(body), 76)]
    return ("\r\n".join(lines) + "\r\n.\r\n").encode("ascii")


class Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def local_host(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def server_of(url: str):
    """(tls, user, host, port) of smtps://user@host:port, or None."""
    scheme, sep, rest = url.partition("://")
    if not sep or scheme not in ("smtp", "smtps") or "/" in rest or not rest:
        return None
    user, _, hostport = rest.rpartition("@")
    host, _, port = hostport.partition(":")
    if not host or (port and not port.isdigit()) or ":" in user:
        return None
    tls = scheme == "smtps"
    return tls, user, host.lower(), int(port) if port else (465 if tls else 25)


def deliver(tls: bool, user: str, password: str, host: str, port: int, sender: str, to: str, data: bytes) -> None:
    """One message, handed over to the server (RFC 5321)."""
    if not tls and not local_host(host):
        raise Refused(f"{host} is reached with smtps:// only")
    try:
        sock = socket.create_connection((host, port), timeout=30.0)
    except OSError as exc:
        raise Refused(f"cannot reach {host}:{port}: {exc.strerror or 'timed out'}") from None
    try:
        if tls:
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError:
                raise Refused(f"TLS with {host}: the certificate is not trusted") from None
            except (ssl.SSLError, OSError):
                raise Refused(f"TLS with {host}: the TLS handshake failed") from None
        buf = b""

        def reply() -> tuple:
            nonlocal buf
            while True:
                while b"\n" not in buf:
                    try:
                        chunk = sock.recv(4096)
                    except OSError:
                        raise Refused("connection lost") from None
                    if not chunk:
                        raise Refused("the server closed the connection")
                    buf += chunk
                line, _, buf = buf.partition(b"\n")
                text = line.decode("utf-8", "replace").rstrip("\r")
                if len(text) < 4 or text[3] != "-":
                    return text[:3], text

        def say(line: bytes, *good: str) -> None:
            if line:
                try:
                    sock.sendall(line)
                except OSError:
                    raise Refused("connection lost") from None
            code, text = reply()
            if code not in good:
                raise Refused(f"it answered {text}")

        say(b"", "220")
        say(f"EHLO {sender.rsplit('@', 1)[-1]}\r\n".encode(), "250")
        if user:
            token = base64.b64encode(f"\0{user}\0{password}".encode("utf-8"))
            say(b"AUTH PLAIN " + token + b"\r\n", "235")
        say(f"MAIL FROM:<{sender}>\r\n".encode(), "250")
        say(f"RCPT TO:<{to}>\r\n".encode(), "250", "251")
        say(b"DATA\r\n", "354")
        say(data, "250")
        try:
            sock.sendall(b"QUIT\r\n")
        except OSError:
            pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def b_send_email(call, to, text, subject=None):
    to = text_arg(call, "send_email", "to", to)
    text = text_arg(call, "send_email", "text", text)
    if subject is None:
        subject = text.split("\n")[0].strip()
    subject = " ".join(text_arg(call, "send_email", "subject", subject).split())
    if not is_address(to):
        raise invalid(call, "notify_address", to=to)
    from .stream import in_memory
    if in_memory():
        print(errors.msg("info.notify_email", to=to, subject=subject))
        return None
    table = settings(call)
    url, sender = table.get("smtp", ""), table.get("from", "")
    if not url or not sender:
        raise failure("NetworkError", call.span, key="NetworkError.mail_setup", to=to)
    server = server_of(url)
    if server is None:
        raise invalid(call, "notify_smtp", url=url)
    if not is_address(sender):
        raise invalid(call, "notify_address", to=sender)
    tls, user, host, port = server
    from .policy import check
    check(call, "net", host)
    password = secret_of(call, table["key"]) if user and table.get("key") else ""
    now = _time.time()
    ident = f"{int(now * 1000)}.{os.urandom(4).hex()}"
    try:
        deliver(tls, user, password, host, port, sender, to, message(sender, to, subject, text, int(now), ident))
    except Refused as exc:
        raise failure("NetworkError", call.span, key="NetworkError.mail", server=host, to=to, reason=exc.reason) from None
    return None


# --------------------------------------------------------------- slack

def b_slack(call, channel, text):
    channel = text_arg(call, "slack", "channel", channel)
    text = text_arg(call, "slack", "text", text)
    if not channel.strip() or any(c.isspace() for c in channel):
        raise invalid(call, "notify_channel", channel=channel)
    from .stream import in_memory
    if in_memory():
        print(errors.msg("info.notify_slack", channel=channel, text=text))
        return None
    name = settings(call).get("slack", "")
    if not name:
        raise failure("NetworkError", call.span, key="NetworkError.slack_setup", channel=channel)
    token = secret_of(call, name)
    from .http import send
    base = os.environ.get("NEXO_SLACK_URL") or SLACK
    body = json.dumps({"channel": channel, "text": text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    r = send(call, "POST", base.rstrip("/") + "/chat.postMessage", body, "application/json; charset=utf-8",
             {"Authorization": f"Bearer {token}"}, 30.0, 1)
    try:
        answer = json.loads(r.text())
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        reason = answer.get("error") if isinstance(answer, dict) and isinstance(answer.get("error"), str) else "no answer"
        raise failure("NetworkError", call.span, key="NetworkError.slack", channel=channel, reason=reason)
    return None


def b_notify(call, to, text):
    """An address gets a mail, a #channel a message of Slack."""
    to = text_arg(call, "notify", "to", to)
    text = text_arg(call, "notify", "text", text)
    if to.startswith("#"):
        return b_slack(call, to, text)
    if is_address(to):
        return b_send_email(call, to, text)
    raise invalid(call, "notify_to", to=to)


def exports() -> dict:
    return {
        "send_email": Builtin("send_email", b_send_email),
        "slack": Builtin("slack", b_slack),
        "notify": Builtin("notify", b_notify),
    }
