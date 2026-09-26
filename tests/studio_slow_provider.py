"""A loopback Anthropic endpoint for the studio's deadline tests (no network).

Codex's probe on #272 (CODEX-PR272-8A9349E-NOGO-20260926T020927Z): the
installed SDK, pointed at a loopback server that trickles a valid body a byte
at a time, ran past its own per-operation timeout. These tests drive the real
SDK the same way. Each POST gets the next scripted reply (the last repeats):

    ("slow", body, seconds_per_byte)   200, then the body one byte at a time
    ("fast", body)                     200 and the whole body at once
    ("status", code, error_type)       an Anthropic-shaped error with that status

`served` records each request: when its body finished, or when the client
dropped the connection, in seconds after the headers went out.
CI runs these suites in an empty network namespace with only loopback up.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def message(text: str) -> dict:
    return {"id": "msg_loopback", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def tool_message(name: str, tool_input: dict) -> dict:
    return {"id": "msg_loopback", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "tool_use", "id": "toolu_loopback", "name": name, "input": tool_input}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


class SlowProvider:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.served: list[dict] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with owner.lock:
                    reply = owner.replies[min(len(owner.served), len(owner.replies) - 1)]
                    record = {"path": self.path}
                    owner.served.append(record)
                kind = reply[0]
                if kind == "status":
                    body = json.dumps({"type": "error", "error": {"type": reply[2], "message": "loopback"}}).encode()
                    self.send_response(reply[1])
                else:
                    body = json.dumps(reply[1]).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                started = time.monotonic()
                try:
                    if kind == "slow":
                        for i in range(len(body)):
                            self.wfile.write(body[i:i + 1])
                            self.wfile.flush()
                            time.sleep(reply[2])
                    else:
                        self.wfile.write(body)
                        self.wfile.flush()
                    record["complete"] = time.monotonic() - started
                except OSError:
                    record["dropped"] = time.monotonic() - started

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def client(self):
        import anthropic
        return anthropic.Anthropic(api_key="sk-ant-loopback-test", base_url=self.url, max_retries=0, timeout=30.0)

    def wait_settled(self, index: int, seconds: float = 5.0) -> dict:
        """The request's record once its body finished or its client dropped."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with self.lock:
                if len(self.served) > index and ("complete" in self.served[index] or "dropped" in self.served[index]):
                    return dict(self.served[index])
            time.sleep(0.02)
        with self.lock:
            return dict(self.served[index]) if len(self.served) > index else {}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
