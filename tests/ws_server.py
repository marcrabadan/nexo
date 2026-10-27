"""A WebSocket server for the tests of nexo.stream (D169), written from RFC
6455 on its own (it shares no code with nexo/stdlib/websocket.py): it
answers the handshake, sends each connection the messages it was given
(split in two frames when long, with a ping first), and keeps the text
messages a client sends.

    server = start(["hello", "world"])
    server.port, server.received
"""
from __future__ import annotations

import base64
import hashlib
import socketserver
import struct
import threading


class Server:
    def __init__(self, outgoing):
        self.outgoing = list(outgoing)
        self.received: list = []


def start(outgoing) -> Server:
    state = Server(outgoing)

    class Handler(socketserver.BaseRequestHandler):
        def recv(self, n):
            out = b""
            while len(out) < n:
                chunk = self.request.recv(n - len(out))
                if not chunk:
                    raise ConnectionError
                out += chunk
            return out

        def frame(self, opcode, payload, fin=True):
            n = len(payload)
            head = bytes([(0x80 if fin else 0) | opcode])
            head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n)
            self.request.sendall(head + payload)

        def handle(self):
            request = b""
            while b"\r\n\r\n" not in request:
                request += self.request.recv(4096)
            lines = request.decode().split("\r\n")
            headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:]) if k}
            if headers.get("upgrade", "").lower() != "websocket":
                self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            accept = base64.b64encode(hashlib.sha1(
                (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.request.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            self.frame(9, b"are you there")
            for text in state.outgoing:
                data = text.encode()
                if len(data) > 4:
                    self.frame(1, data[:3], fin=False)
                    self.frame(0, data[3:])
                else:
                    self.frame(1, data)
            try:
                while True:
                    b0, b1 = self.recv(2)
                    n = b1 & 0x7F
                    if n == 126:
                        n = struct.unpack(">H", self.recv(2))[0]
                    elif n == 127:
                        n = struct.unpack(">Q", self.recv(8))[0]
                    assert b1 & 0x80, "a client masks its frames"
                    mask = self.recv(4)
                    payload = bytes(x ^ mask[i % 4] for i, x in enumerate(self.recv(n)))
                    opcode = b0 & 0x0F
                    if opcode == 1:
                        state.received.append(payload.decode())
                    elif opcode == 8:
                        return
            except (ConnectionError, OSError):
                return

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.port = server.server_address[1]
    state.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return state
