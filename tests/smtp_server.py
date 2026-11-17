"""A mail server for the tests of nexo.notify (D209), written from RFC 5321
on its own (it shares no code with nexo/stdlib/notify.py): it greets,
answers EHLO with several lines, checks AUTH PLAIN when it has users,
refuses the recipients it was told to, and keeps each message handed
over.

    server = start(users={"shop": "pw"}, refuse=["gone@mail.com"])
    server.port, server.messages  # [{"from", "to", "auth", "data"}]
"""
from __future__ import annotations

import base64
import socketserver
import threading


class Server:
    def __init__(self, users, refuse):
        self.users = dict(users or {})
        self.refuse = list(refuse or [])
        self.messages: list = []


def start(users=None, refuse=None) -> Server:
    state = Server(users, refuse)

    class Handler(socketserver.StreamRequestHandler):
        def say(self, text: str) -> None:
            self.wfile.write((text + "\r\n").encode())
            self.wfile.flush()

        def handle(self):
            self.say("220 mail.test ESMTP")
            sender, to, auth = None, None, None
            while True:
                raw = self.rfile.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                word = line.split(" ")[0].upper()
                if word == "EHLO":
                    self.say("250-mail.test greets " + line[5:])
                    self.say("250-SIZE 1000000")
                    self.say("250 AUTH PLAIN")
                elif word == "AUTH":
                    parts = line.split(" ")
                    try:
                        _, user, password = base64.b64decode(parts[2]).decode().split("\0")
                    except (IndexError, ValueError):
                        self.say("501 5.5.2 cannot decode")
                        continue
                    if parts[1].upper() == "PLAIN" and state.users.get(user) == password:
                        auth = user
                        self.say("235 2.7.0 accepted")
                    else:
                        self.say("535 5.7.8 the password is wrong")
                elif word == "MAIL":
                    if state.users and auth is None:
                        self.say("530 5.7.0 authentication required")
                        continue
                    sender = line[line.index("<") + 1:line.index(">")]
                    self.say("250 2.1.0 ok")
                elif word == "RCPT":
                    address = line[line.index("<") + 1:line.index(">")]
                    if address in state.refuse:
                        self.say("550 5.1.1 no such user here")
                    else:
                        to = address
                        self.say("250 2.1.5 ok")
                elif word == "DATA":
                    if sender is None or to is None:
                        self.say("503 5.5.1 MAIL and RCPT first")
                        continue
                    self.say("354 end with a line with a dot")
                    data = b""
                    while not data.endswith(b"\r\n.\r\n"):
                        chunk = self.rfile.readline()
                        if not chunk:
                            return
                        data += chunk
                    state.messages.append({"from": sender, "to": to, "auth": auth, "data": data[:-5].decode("ascii")})
                    self.say("250 2.0.0 kept")
                elif word == "QUIT":
                    self.say("221 2.0.0 bye")
                    return
                else:
                    self.say("502 5.5.1 not here")

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    state.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return state
