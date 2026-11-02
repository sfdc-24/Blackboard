"""One state file for the laptop call helpers; explicit migration and recoverable unknown work.

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
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("CLI_CALL_STATE_DIR") or HERE
STATE = os.path.join(STATE_DIR, "cli_call_state.json")
LOCKFILE = STATE + ".lock"
STREAM_ID = re.compile(r"^(?:0-0|[0-9]{1,20}-[0-9]{1,20})$")
ASK_ID = re.compile(r"^call-[0-9a-f]{12}$")
# The old CANCELLED_AFTER_WRITE label did not prove withdrawal; keep it recoverable.
OPEN = ("PENDING", "LEGACY_OPEN", "ACCEPTED", "WRITING", "STORED", "PUBLISHED", "CANCELLING", "COMPLETING",
        "CANCELLED_AFTER_WRITE")
TERMINAL = ("DONE", "CANCELLED", "EXPIRED", "FAILED", "HISTORICAL_CLOSED")
OP_STATES = ("INTENT", "SENT", "CONFIRMED", "UNKNOWN")
LOCK_WAIT = 30.0
EXIT_LOCKED = 4
LEGACY_NAMES = ("cli_call_consumer.pending", "cli_call_consumer.after", "cli_call_accepted.txt")

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
    if ms == 0 and seq == 0:
        return "0-0"
    return "%d-%d" % (ms, seq - 1) if seq else "%d-%d" % (ms - 1, 2 ** 64 - 1)


def empty():
    return {"v": 2, "version": 0, "seen": "0-0", "tasks": {}}


def valid(state):
    if not (isinstance(state, dict) and state.get("v") == 2 and isinstance(state.get("version"), int)
            and not isinstance(state.get("version"), bool) and state["version"] >= 0
            and isinstance(state.get("tasks"), dict)):
        return False
    if state.get("seen") and not STREAM_ID.fullmatch(str(state["seen"])):
        return False
    for ask, task in state["tasks"].items():
        if not (isinstance(ask, str) and ASK_ID.fullmatch(ask) and isinstance(task, dict) and task.get("state") in OPEN + TERMINAL
                and isinstance(task.get("rev"), int) and not isinstance(task["rev"], bool) and task["rev"] >= 0
                and isinstance(task.get("ops", {}), dict)):
            return False
        if task.get("entry") and not STREAM_ID.fullmatch(str(task["entry"])):
            return False
        if any(not isinstance(op, dict) or op.get("state") not in OP_STATES for op in task.get("ops", {}).values()):
            return False
    return True


def load():
    """Read v2 state. Legacy evidence requires explicit migration, never a silent empty start."""
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        if any(os.path.exists(os.path.join(os.path.dirname(STATE), name)) for name in LEGACY_NAMES):
            raise SystemExit("STOP: legacy helper files exist; run call_ask.py migrate before intake")
        return empty()
    except ValueError:
        raise SystemExit("STOP: %s is not JSON; nothing done" % STATE)
    if not valid(state):
        raise SystemExit("STOP: %s is not a valid v2 state; nothing done" % STATE)
    return state


def _write(state):
    if not valid(state):
        raise SystemExit("STOP: refusing to write an invalid state")
    directory = os.path.dirname(os.path.abspath(STATE))
    fd, tmp = tempfile.mkstemp(prefix=".cli_call_state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE)
        # A durable rename on POSIX; Windows does not provide O_DIRECTORY.
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Txn:
    """The state under the lock. `save()` writes now (an INTENT must be on disk before its append)."""

    def __init__(self, state):
        self.state, self.base = state, state["version"]

    def save(self):
        self.base += 1
        self.state["version"] = self.base
        _write(self.state)


@contextlib.contextmanager
def migration_locked(wait=LOCK_WAIT, clock=time.monotonic, sleep=time.sleep):
    """Acquire only the OS lock, for explicit migration of an unreadable pre-v2 state.

    No old file is deleted or age-stolen. Normal callers must use locked(), which validates v2.
    """
    fd = os.open(LOCKFILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # Pre-v2 used a nonempty O_EXCL PID marker, not this kernel lock. Never pretend
        # that taking flock proves that older writer stopped, and never age-steal it.
        if os.fstat(fd).st_size and os.read(fd, 256).strip():
            raise SystemExit("STOP: legacy lock marker exists; stop the old writer and reconcile it first")
        end = clock() + wait
        while True:
            try:
                _lock(fd)
                break
            except OSError:
                if clock() >= end:
                    print("STOP: the state lock is held by a live process; nothing done (exit 4)", file=sys.stderr)
                    raise Busy()
                sleep(0.2)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(wait=LOCK_WAIT, clock=time.monotonic, sleep=time.sleep):
    """Validate and update v2 state under the same kernel lock used by explicit migration."""
    with migration_locked(wait, clock, sleep):
        txn = Txn(load())
        yield txn
        txn.save()


def touch(task, **fields):
    """Change a task and move its rev, so a step that waited elsewhere sees that it changed."""
    task.update(fields)
    task["rev"] = int(task.get("rev", 0)) + 1
    return task


def cursor(state=None, now=time.time):
    """Conservative scan floor, never a wall-clock seed.

    New acceptance uses a per-ask authoritative lookup, not this watermark. An unindexed cue
    cannot be excluded by a later task. Only a durably supplied contiguous scan watermark may
    advance seen; legacy migration resets it to zero and retains old cursors as evidence.
    """
    state = load() if state is None else state
    pending = [t for t in state["tasks"].values() if t["state"] in OPEN]
    if any(not t.get("entry") for t in pending):
        return "0-0"
    opened = [t["entry"] for t in pending if t.get("entry")]
    points = [pred(min(opened, key=sid))] if opened else []
    points.append(state.get("seen") or "0-0")
    return min(points, key=sid)


def ring_many(asks, source, wait=200.0):
    """Persist every cue in this bounded page before handing any one task to a worker."""
    asks = list(dict.fromkeys(asks))
    if len(asks) > 500 or any(not isinstance(a, str) or not ASK_ID.fullmatch(a) for a in asks):
        raise ValueError("invalid or excessive cue page")
    new = []
    with locked(wait=wait) as txn:
        for ask in asks:
            if ask in txn.state["tasks"]:
                continue
            txn.state["tasks"][ask] = {"state": "PENDING", "rev": 1, "ops": {}, "source": source,
                                       "entry": "", "rung": utc_stamp()}
            new.append(ask)
    return new


def ring(ask, source, entry="", wait=200.0):
    """A cue makes PENDING and nothing more. Returns True when this ask was new."""
    if not isinstance(ask, str) or not ASK_ID.fullmatch(ask):
        raise ValueError("invalid ask id")
    with locked(wait=wait) as txn:
        if ask in txn.state["tasks"]:
            return False
        txn.state["tasks"][ask] = {"state": "PENDING", "rev": 1, "ops": {}, "source": source,
                                   "entry": entry if STREAM_ID.fullmatch(entry or "") else "", "rung": utc_stamp()}
        return True
