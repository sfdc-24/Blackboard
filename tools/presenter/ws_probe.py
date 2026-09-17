#!/usr/bin/env python3
"""Prove one DevTools round trip, in a FILE rather than an inline heredoc.

WHY A FILE
  The same probe as a shell heredoc was rejected before it ran: the sandbox read
  a Python variable as a deletion target. That is the sixth script lost tonight
  to text passing through a shell. A file has no shell in the path.

WHAT IT ESTABLISHES
  The handshake already returns 101 with a valid Sec-WebSocket-Accept, so the
  upgrade is fine. The connection dies on the FIRST frame. The hypothesis under
  test is the Origin header: Chrome 153 resets DevTools sockets carrying an
  unexpected Origin when the browser was not launched with a matching
  --remote-allow-origins. This sends none.
"""
import base64
import json
import os
import socket
import struct
import urllib.request
from urllib.parse import urlparse

DEBUG = "http://127.0.0.1:9222"


def page_target():
    raw = urllib.request.urlopen(DEBUG + "/json", timeout=8).read()
    for t in json.loads(raw):
        if t.get("type") == "page" and not t.get("url", "").startswith("devtools://"):
            return t
    raise SystemExit("  no page target")


def main():
    t = page_target()
    print("  target url :", t.get("url"))
    u = urlparse(t["webSocketDebuggerUrl"])
    sock = socket.create_connection((u.hostname, u.port), timeout=10)

    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ) % (u.path, u.hostname, u.port, key)
    sock.sendall(handshake.encode())

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise SystemExit("  socket closed during handshake")
        buf += chunk
    status = buf.decode("latin-1").split("\r\n")[0]
    print("  handshake  :", status)
    if "101" not in status:
        raise SystemExit("  upgrade refused")

    msg = json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": "document.title", "returnByValue": True},
    }).encode()

    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(msg))
    size = len(msg)
    if size < 126:
        header = struct.pack("!BB", 0x81, 0x80 | size)
    else:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, size)
    sock.sendall(header + mask + masked)
    print("  frame sent :", len(header) + 4 + size, "bytes")

    def take(count):
        out = b""
        while len(out) < count:
            chunk = sock.recv(count - len(out))
            if not chunk:
                raise SystemExit("  SOCKET RESET by peer after the frame")
            out += chunk
        return out

    head = take(2)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", take(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", take(8))[0]
    payload = take(length).decode("utf-8")
    print("  REPLY      :", payload[:220])
    sock.close()
    print("  RESULT     : one full DevTools round trip succeeded")


if __name__ == "__main__":
    main()
