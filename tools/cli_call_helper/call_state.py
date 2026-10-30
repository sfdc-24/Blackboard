"""One state file for the laptop call helpers (v5, Oct 30). Contract v2 (Drive 1_fBs2qdR1i-yIx_v5IbaqhxHBS3rXszm)
plus addendum CLAUDE-CLI-CONTRACT-V2-DEPS-20261030T132707Z.

cli_call_state.json = {"v": 2, "version": n, "seen": "<stream id>",
                       "tasks": {"<ask>": {"state": ..., "rev": n, "ops": {"<op id>": {"state", "at"}}, ...}}}

THE LOCK (contract 1). An OS byte-range lock (msvcrt.locking; fcntl.flock off Windows) on byte 0 of
cli_call_state.lock, held on an open handle. The kernel drops it when the holder exits for any reason, so there is
no pid check, no age-based break and no steal. A process that cannot take it in time exits 4 (fail closed).
Every write happens only under the lock: read, change, version+1, temp file + fsync + os.replace. The compare is
valid because only the holder writes; os.replace is never treated as a compare-and-swap by itself.
A task carries its own `rev`. A step that waited outside the lock re-reads the task and re-plans when the rev moved.
"""
import contextlib
import datetime as dt
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("CLI_CALL_STATE_DIR") or HERE
STATE = os.path.join(STATE_DIR, "cli_call_state.json")
LOCKFILE = STATE + ".lock"
STREAM_ID = re.compile(r"^\d{10,20}-\d+$")
ASK_ID = re.compile(r"^call-[0-9a-f]{12}$")
OPEN = ("PENDING", "LEGACY_OPEN", "ACCEPTED", "WRITING", "STORED", "PUBLISHED")
TERMINAL = ("DONE", "CANCELLED", "CANCELLED_AFTER_WRITE", "EXPIRED", "FAILED", "HISTORICAL_CLOSED")
OP_STATES = ("INTENT", "SENT", "CONFIRMED", "UNKNOWN")
LOCK_WAIT = 30.0
EXIT_LOCKED = 4

if os.name == "nt":
    import msvcrt

    def _lock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)            # OSError while another handle holds it

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:                                                       # pragma: no cover - the laptop is Windows
    import fcntl

    def _lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


class Busy(SystemExit):
    def __init__(self):
        super().__init__(EXIT_LOCKED)


def utc_stamp(t=None):
    t = time.time() if t is None else t
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sid(value):
    """A stream id as a comparable tuple."""
    a, b = str(value).split("-")
    return int(a), int(b)


def pred(value):
    """The id just before `value`, so an exclusive read after it still returns `value` itself."""
    ms, seq = sid(value)
    return "%d-%d" % (ms, seq - 1) if seq else "%d-%d" % (ms - 1, 2 ** 64 - 1)


def empty():
    return {"v": 2, "version": 0, "seen": "", "tasks": {}}


def valid(state):
    if not (isinstance(state, dict) and state.get("v") == 2 and isinstance(state.get("version"), int)
            and isinstance(state.get("tasks"), dict)):
        return False
    if state.get("seen") and not STREAM_ID.match(str(state["seen"])):
        return False
    for ask, task in state["tasks"].items():
        if not (ASK_ID.match(ask) and isinstance(task, dict) and task.get("state") in OPEN + TERMINAL
                and isinstance(task.get("rev"), int) and isinstance(task.get("ops", {}), dict)):
            return False
        if task.get("entry") and not STREAM_ID.match(str(task["entry"])):
            return False
        if any(not isinstance(op, dict) or op.get("state") not in OP_STATES for op in task.get("ops", {}).values()):
            return False
    return True


def load():
    """The state. Missing: empty (run `call_ask.py migrate` before the first live use). Broken: stop."""
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return empty()
    except ValueError:
        raise SystemExit("STOP: %s is not JSON; nothing done" % STATE)
    if not valid(state):
        raise SystemExit("STOP: %s is not a valid v2 state; nothing done" % STATE)
    return state


def _write(state):
    if not valid(state):
        raise SystemExit("STOP: refusing to write an invalid state")
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


class Txn:
    """The state under the lock. `save()` writes now (an INTENT must be on disk before its append)."""

    def __init__(self, state):
        self.state, self.base = state, state["version"]

    def save(self):
        self.base += 1
        self.state["version"] = self.base
        _write(self.state)


@contextlib.contextmanager
def locked(wait=LOCK_WAIT, clock=time.monotonic, sleep=time.sleep):
    """Hold the OS lock and yield a Txn; whatever the caller left in it is written at the end.
    An exception inside writes nothing more (what was saved stays saved)."""
    fd = os.open(LOCKFILE, os.O_RDWR | os.O_CREAT)
    try:
        end = clock() + wait
        while True:
            try:
                _lock(fd)
                break
            except OSError:
                if clock() > end:
                    print("STOP: the state lock is held by a live process; nothing done (exit 4)", file=sys.stderr)
                    raise Busy()
                sleep(0.2)
        try:
            txn = Txn(load())
            yield txn
            txn.save()
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def touch(task, **fields):
    """Change a task and move its rev, so a step that waited elsewhere sees that it changed."""
    task.update(fields)
    task["rev"] = int(task.get("rev", 0)) + 1
    return task


def cursor(state=None, now=time.time):
    """The lowest unfinished point (contract 3): the inbox is read after this. It never passes an open task's
    entry, however many later asks were accepted."""
    state = state or load()
    opened = [t["entry"] for t in state["tasks"].values() if t["state"] in OPEN and t.get("entry")]
    points = [pred(min(opened, key=sid))] if opened else []
    if state.get("seen"):
        points.append(state["seen"])
    return min(points, key=sid) if points else "%d-0" % int(now() * 1000)


def ring(ask, source, entry="", wait=200.0):
    """A cue makes PENDING and nothing more. Returns True when this ask was new."""
    with locked(wait=wait) as txn:
        if ask in txn.state["tasks"]:
            return False
        txn.state["tasks"][ask] = {"state": "PENDING", "rev": 1, "ops": {}, "source": source,
                                   "entry": entry if STREAM_ID.match(entry or "") else "", "rung": utc_stamp()}
        return True
