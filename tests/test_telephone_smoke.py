"""The two telephone entry points, run as processes on this box, with no cloud around them.

Every other suite drives the modules through injected adapters. This one runs main() the way the
image does: drive-intake's real server on a loopback port, and the worker job as a process. It
proves the wiring that no unit test reaches - the server loop, env parsing, the honest lines when
Redis and Docs are absent - and it is loopback only: the server binds 127.0.0.1, and nothing
leaves this machine.
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INTAKE = str(REPO / "cloud" / "drive-intake" / "main.py")
WORKER = str(REPO / "cloud" / "claude-api-worker" / "main.py")
FOLDER = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def listen_addresses(port: int):
    """Local addresses in LISTEN for this port, from /proc. None when this box has no tcp table."""
    paths = [Path(name) for name in ("/proc/net/tcp", "/proc/net/tcp6")]
    if not any(path.exists() for path in paths):
        return None
    hexport = "%04X" % port
    found = set()
    for path in paths:
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            ip, hexp = parts[1].split(":")
            if hexp == hexport and parts[3] == "0A":
                found.add(ip)
    return found


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DRIVE_INTAKE_", "CLAUDE_API_", "REDIS_", "INBOX_WAKE_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


class IntakeServer:
    def __init__(self, flag):
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-B", INTAKE],
            env=clean_env(PORT=str(self.port), DRIVE_INTAKE_BIND="127.0.0.1", DRIVE_INTAKE_ENABLED=flag,
                          DRIVE_INTAKE_POLL_TOKEN="pt", DRIVE_INTAKE_CHANNEL_TOKEN="ct",
                          DRIVE_INTAKE_FOLDER_ID=FOLDER, DRIVE_INTAKE_API_WORKER="claude-api"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def __enter__(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                self.get("/healthz")
                return self
            except Exception:  # noqa: BLE001 - not up yet
                if self.proc.poll() is not None:
                    raise RuntimeError("drive-intake exited: %s" % (self.proc.stdout.read() or ""))
                time.sleep(0.2)
        raise RuntimeError("drive-intake did not come up")

    def __exit__(self, *_exc):
        self.proc.terminate()
        try:
            self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()

    def get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def post(self, path, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=b"", method="POST",
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


class IntakeSmokeTest(unittest.TestCase):
    def test_flag_off_the_server_answers_health_and_refuses_an_unauthenticated_poll(self):
        with IntakeServer("off") as server:
            addrs = listen_addresses(server.port)
            if addrs is not None:
                self.assertEqual({"0100007F"}, addrs)   # 127.0.0.1, nothing else
            status, health = server.get("/healthz")
            self.assertEqual(200, status)
            self.assertFalse(health["ok"])
            self.assertFalse(health["enabled"])
            self.assertEqual(("unknown", "unknown"), (health["channel"], health["poll"]))
            self.assertEqual({"claude-code-cli": "offline", "api": "claude-api"}, health["worker"])
            self.assertEqual(401, server.post("/poll")[0])
            self.assertEqual(401, server.post("/hooks/drive", {"X-Goog-Resource-State": "update"})[0])
            self.assertEqual((200, {"enabled": False, "intake": []}),
                             server.post("/poll", {"X-Drive-Intake-Poll-Token": "pt"}))
            self.assertEqual((200, {"enabled": False, "intake": []}),
                             server.post("/poll", {"Authorization": "Bearer pt"}))
            self.assertEqual(404, server.post("/nothing")[0])

    def test_flag_on_with_no_store_around_it_says_no_store_and_never_ok(self):
        with IntakeServer("on") as server:
            addrs = listen_addresses(server.port)
            if addrs is not None:
                self.assertEqual({"0100007F"}, addrs)
            status, health = server.get("/healthz")
            self.assertTrue(health["enabled"])
            self.assertFalse(health["ok"])
            self.assertEqual("unknown", health["poll"])
            self.assertEqual((200, {"enabled": True, "intake": [{"status": "no_store", "task": None}]}),
                             server.post("/poll", {"X-Drive-Intake-Poll-Token": "pt"}))
            self.assertEqual(401, server.post("/poll", {"X-Drive-Intake-Poll-Token": "wrong"})[0])


class WorkerSmokeTest(unittest.TestCase):
    def run_job(self, **env):
        run = subprocess.run([sys.executable, "-B", WORKER], env=clean_env(**env), capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(0, run.returncode, run.stderr)
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    def test_flags_off_prints_disabled_and_nothing_else(self):
        self.assertEqual([{"status": "disabled"}], self.run_job())
        self.assertEqual([{"status": "disabled"}], self.run_job(CLAUDE_API_WORKER_ENABLED="ON"))

    def test_flags_on_with_no_redis_here_says_so_and_runs_nothing(self):
        lines = self.run_job(CLAUDE_API_WORKER_ENABLED="on", CLAUDE_API_LANE_ENABLED="on")
        self.assertEqual("no_redis", lines[0]["status"])
        self.assertEqual({"considered": [], "lane": [{"status": "no_store", "task": None}], "status": "no_store"},
                         lines[-1])
        lines = self.run_job(CLAUDE_API_LANE_ENABLED="on")
        self.assertEqual("lane_only", lines[-1]["status"])
        self.assertEqual([{"status": "no_store", "task": None}], lines[-1]["lane"])


if __name__ == "__main__":
    unittest.main()
