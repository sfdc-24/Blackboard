"""Killable whole-operation boundary for the fixed, read-only Salesforce worker.

Only this child performs DNS/network I/O. The parent reads at most MAX_OUTPUT+1
bytes and always kills/reaps an unfinished child before returning or raising.
The child is trusted repository code, accepts no caller-supplied executable,
and never spawns other processes. Credentials travel only in its narrow env.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime


MAX_OUTPUT = 4096
MAX_TIMEOUT_SECONDS = 20
FACT_KEYS = frozenset({"org_id", "org_name", "org_type", "total", "site_total",
                       "site_last_7_days", "observed_at", "source"})
CREDENTIAL_KEYS = ("Headless_domain", "Headless_consumer_key", "Headless_consumer_secret")


class FactsUnavailable(RuntimeError):
    """Fixed redacted error; never includes child output or request details."""


def validate_result(value, expected_org_id):
    if not isinstance(value, dict) or set(value) != FACT_KEYS or value.get("org_id") != expected_org_id:
        raise FactsUnavailable("invalid Lead facts result")
    for key in ("org_name", "org_type"):
        text = value[key]
        if not isinstance(text, str) or not text.strip() or len(text) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise FactsUnavailable("invalid Lead facts result")
    for key in ("total", "site_total", "site_last_7_days"):
        count = value[key]
        if type(count) is not int or not 0 <= count <= 2**63 - 1:
            raise FactsUnavailable("invalid Lead facts result")
    if not value["site_last_7_days"] <= value["site_total"] <= value["total"]:
        raise FactsUnavailable("inconsistent Lead facts result")
    if value["source"] != "live SOQL on the Lead object":
        raise FactsUnavailable("invalid Lead facts result")
    stamp = value["observed_at"]
    if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp):
        raise FactsUnavailable("invalid Lead facts result")
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise FactsUnavailable("invalid Lead facts result") from None
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate result key")
        result[key] = value
    return result


def _kill_and_wait(process):
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    # Never return while the provider process is alive. This wait is process
    # reaping after an unconditional kill, not a grace period for provider I/O.
    process.wait()


def _run_child(command, environment, deadline, cancel):
    """Internal transport; executable selection is fixed by fetch_lead_facts.

    The sole parent thread only drains a bounded pipe, never runs provider code.
    Killing/reaping the child closes that pipe; the reader is joined as well.
    """
    if cancel.is_set() or time.monotonic() >= deadline:
        raise FactsUnavailable("Lead facts operation cancelled or timed out")
    process = None
    reader = None
    data = []
    read_failed = []
    ready = threading.Event()
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, close_fds=True, start_new_session=os.name == "posix",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        def read_output():
            try:
                data.append(process.stdout.read(MAX_OUTPUT + 1))
            except Exception:
                read_failed.append(True)
            finally:
                ready.set()

        reader = threading.Thread(target=read_output, name="lead-facts-output", daemon=True)
        reader.start()
        while True:
            if cancel.is_set() or time.monotonic() >= deadline:
                raise FactsUnavailable("Lead facts operation cancelled or timed out")
            if ready.is_set():
                if read_failed or not data or len(data[0]) > MAX_OUTPUT:
                    raise FactsUnavailable("invalid Lead facts output")
                if process.poll() is not None:
                    if process.returncode != 0:
                        raise FactsUnavailable("Lead facts worker failed")
                    return data[0]
            # cancel.wait wakes Stop promptly even when stdout has no bytes.
            cancel.wait(min(0.01, max(0, deadline - time.monotonic())))
    except FactsUnavailable:
        raise
    except Exception:
        raise FactsUnavailable("Lead facts worker unavailable") from None
    finally:
        if process is not None:
            _kill_and_wait(process)
            if reader is not None:
                reader.join()
            process.stdout.close()


def fetch_lead_facts(expected_org_id: str, timeout_seconds: float, cancel=None):
    started = time.monotonic()
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise FactsUnavailable("invalid Lead facts deadline")
    if not isinstance(expected_org_id, str) or not re.fullmatch(r"00D[A-Za-z0-9]{15}", expected_org_id):
        raise FactsUnavailable("invalid Lead facts organization")
    # No broad parent environment, model/API credentials, PATH lookup, argv
    # secret, or stdin protocol. -I ignores Python startup/path injection.
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "SYSTEMROOT") if key in os.environ}
    environment.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                        "STUDIO_SALESFORCE_ORG_ID": expected_org_id})
    for key in CREDENTIAL_KEYS:
        value = os.environ.get(key, "")
        if not value or len(value) > 8192 or "\x00" in value:
            raise FactsUnavailable("Lead facts configuration unavailable")
        environment[key] = value
    raw = _run_child([sys.executable, "-I", "-B", str(Path(__file__).resolve())], environment,
                     started + timeout_seconds, cancel or threading.Event())
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid constant")))
        result = validate_result(value, expected_org_id)
        if time.monotonic() >= started + timeout_seconds or (cancel is not None and cancel.is_set()):
            raise FactsUnavailable("Lead facts operation cancelled or timed out")
        return result
    except Exception:
        raise FactsUnavailable("invalid Lead facts result") from None


def child_main():
    try:
        # -I excludes even the script directory. Restore only the fixed source
        # root, never a path supplied in the environment or by a visitor.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from workers.org_facts import OrgFacts
        expected = os.environ["STUDIO_SALESFORCE_ORG_ID"]
        answer = OrgFacts.from_env(expected_org_id=expected).lead_counts()
        result = validate_result({key: answer[key] for key in FACT_KEYS}, expected)
        encoded = json.dumps(result, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_OUTPUT:
            return 1
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except BaseException:
        # No traceback, OAuth URL, response body, credential or raw exception
        # can enter stdout/stderr. The parent sees only a nonzero exit.
        return 1


if __name__ == "__main__":
    raise SystemExit(child_main())
