#!/usr/bin/env python3
r"""Read the board from a cloud runtime and fingerprint what it saw. Reads only.

WHAT THIS IS FOR
    Step 3 of the cutover sequence in docs/OPENAI-CLOUD-MIGRATION.md: "Run a
    read-only cloud probe and compare the addressed-row result with the laptop."
    It runs before anything in the cloud is allowed to write, and its whole job
    is to answer one question honestly: **does a container see the same board the
    laptop sees?**

    It prints a FINGERPRINT rather than the board - counts, the newest timestamp,
    and a digest over the addressed Row_IDs - so the two sides can be compared by
    equality instead of by reading two long lists and hoping.

READ-ONLY BY CONSTRUCTION, NOT BY PROMISE
    A self-check at startup walks THIS MODULE's syntax tree and refuses to run
    unless every call into the bus is either load_env or a fetch whose action is
    the literal "read". The guarantee is enforced by the program rather than
    asserted in a comment, and it is a check on BEHAVIOUR rather than on text -
    the first version scanned string literals and refused the probe because its
    own error message contained the word "write".

WHY IT SHARES THE FLEET'S OWN TRANSPORT RATHER THAN REIMPLEMENTING THE READ
    A second copy of the read path is how a reader and a writer come to disagree
    about what the board says, which is the exact class of bug this probe exists
    to detect. bus.py also retries a health-blob response instead of accepting it
    as an empty board - a read missing `title` returns ok/service/time at HTTP 200
    and is otherwise indistinguishable from a board with nothing on it.

CREDENTIALS
    Injected BUS_URL and BUS_SECRET, per docs/CLOUD-CREDENTIAL-CONTRACT.md. There
    is no .env in this image and no code path that looks for one; bus.load_env
    raises if both are not present in the environment.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# board_say, not bus, and the difference decides whether this works at all:
# bus._fetch_once falls back to a payload-less GET when the gateway answers
# 302, which makes Apps Script run with no parameters and return its health
# object. board_say.bus_get puts everything in the query string, so a
# redirect cannot lose the request - and the gateway only applies `match` on
# that path. See the note in its docstring.
import board_say as board  # noqa: E402

# Optional so the probe still runs with no state configured at all. A
# missing store must degrade to 'no cursor', never to a crash - the probe's
# first job is the parity read, and bookkeeping must not be able to stop it.
try:
    import state_store  # noqa: E402
except Exception:  # noqa: BLE001
    state_store = None

BOARD = os.environ.get("BOARD_TITLE", "Blackboard - Alpha DB")
ME = os.environ.get("AGENT_TAG", "claude-code-cli")

# WHY A CUTOFF IS NOT OPTIONAL FOR A REAL COMPARISON
#   This board is live. claude-mobile and the WhatsApp pipeline both write to it,
#   and the first laptop run came back with a newest row seconds old. Two
#   fingerprints taken a minute apart would therefore differ for a reason that
#   has nothing to do with whether the cloud can see the board - and a comparison
#   that fails for the wrong reason is worse than none, because it gets dismissed
#   as noise or chased as a defect.
#
#   COMPARED_THROUGH is passed identically to both sides. The digest covers rows
#   at or before it; anything newer is counted and reported separately, as the
#   evidence that the board is moving rather than as a mismatch.
CUTOFF = (os.environ.get("COMPARED_THROUGH") or "").strip()

# Five, matching bus.fetch's default for reads, and for the same documented
# reason. The sleep is linear rather than exponential so the worst case stays
# inside a task timeout that can be reasoned about: 5 attempts, 2+4+6+8s of
# waiting, plus however long each hop takes.
# Its OWN cursor, named separately from any waker's. Sharing a state document
# with the waker would make this probe a second writer to the cursor the live
# doorbell depends on, which is the collision class the store exists to make
# safe rather than one to walk into.
CURSOR_NAME = os.environ.get("CURSOR_NAME") or "board_probe"
ATTEMPTS = int(os.environ.get("READ_ATTEMPTS") or 5)
RETRY_SLEEP = int(os.environ.get("RETRY_SLEEP") or 2)

TS_SHAPE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z?")

# The only board entry points this probe may use. load_env reads credentials
# from the environment; bus_get performs one HTTP GET whose action is checked
# below. Anything else on that module - and every write lives there - fails the
# guard.
ALLOWED_BUS_CALLS = {"load_env", "bus_get"}


def refuse_if_this_module_can_write() -> None:
    """Prove read-only from the syntax tree, not from the text.

    THE FIRST VERSION OF THIS FUNCTION REFUSED THE PROBE. It scanned every string
    literal for write words, and its own error message contained "write". That is
    the third time this repository has been caught by the same mistake - a
    workflow grepping its suites for "urlopen", a shell-out check matching the
    sentence that recorded the removal, and this. A substring cannot tell code
    from commentary, and the fix is never a longer exclusion list.

    What makes this probe read-only is the ACTION it sends. So: walk the tree,
    find every call on the `bus` module, and require that only load_env and fetch
    are used and that every fetch passes action="read" as a literal. A write
    added later cannot satisfy both, and prose cannot violate either.
    """
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)

    problems = []
    fetches = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "board"):
            continue
        if fn.attr not in ALLOWED_BUS_CALLS:
            problems.append("board.%s() is not an allowed call" % fn.attr)
            continue
        if fn.attr != "bus_get":
            continue

        fetches += 1
        params = next((a for a in node.args if isinstance(a, ast.Dict)), None)
        if params is None:
            problems.append("board.bus_get() without a literal params dict - "
                            "the action cannot be verified")
            continue
        action = None
        for k, v in zip(params.keys, params.values):
            if isinstance(k, ast.Constant) and k.value == "action":
                action = v
        if not (isinstance(action, ast.Constant) and action.value == "read"):
            problems.append("board.bus_get() action is not the literal 'read'")

    if fetches == 0:
        problems.append("no board.bus_get() found - this probe reads nothing, "
                        "so a green run would prove nothing")
    if problems:
        print("REFUSING, this probe is not read-only:", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        raise SystemExit(2)


def fingerprint() -> dict:
    env = board.load_env()

    # BOUNDED RETRY, AND ONLY BECAUSE THIS IS A READ.
    #
    # The googleusercontent redirect target intermittently answers 404 - bus.py
    # says so in its own docstring and defaults reads to five attempts for that
    # reason. board_say.bus_get follows the redirect chain but does not retry, so
    # the first refusal surfaced here as a failed job.
    #
    # A read is idempotent, so replaying one costs seconds. An append is not, and
    # this probe cannot send one: its AST guard refuses any action but "read".
    attempts = []
    data = None
    for i in range(1, ATTEMPTS + 1):
        # No secret in this dict: bus_get adds it from env itself, so the
        # credential is never handed around by this file.
        #
        # A timeout or a dropped connection is a failed attempt too, not a
        # crash: on 2026-09-24 the 07:15Z run raised TimeoutError on its first
        # read and exited 1, so the soak counted the hour as missed even though
        # the next attempt would have been seconds away.
        try:
            code, body = board.bus_get(env, {"action": "read", "title": BOARD,
                                             "match": ME})
        except (TimeoutError, OSError) as exc:  # URLError is an OSError
            attempts.append("error %s" % type(exc).__name__)
            if i < ATTEMPTS:
                time.sleep(RETRY_SLEEP * i)
            continue
        if code != 200:
            attempts.append("HTTP %s" % code)
        elif not body.lstrip().startswith("{"):
            attempts.append("non-JSON %db" % len(body))
        else:
            parsed = json.loads(body)
            if isinstance(parsed.get("rows"), list):
                attempts.append("ok")
                data = parsed
                break
            # ok/service/time with no rows: a read the gateway did not understand.
            # Indistinguishable from an empty board unless it is called out.
            attempts.append("health-blob")
        if i < ATTEMPTS:
            time.sleep(RETRY_SLEEP * i)

    if data is None:
        raise SystemExit("board unreadable after %d attempts: %s"
                         % (len(attempts), ", ".join(attempts)))
    rows = data["rows"]

    dateable, undateable, ids, after_cutoff = [], 0, [], 0
    for r in rows:
        if not isinstance(r, list) or len(r) < 2:
            undateable += 1
            continue
        ts = str(r[1])
        if TS_SHAPE.fullmatch(ts):
            dateable.append(ts[:19])
            if CUTOFF and ts[:19] > CUTOFF[:19]:
                after_cutoff += 1
                continue           # live traffic: counted, not compared
        else:
            undateable += 1
            # An undateable row cannot be placed against the cutoff, so it stays
            # in the compared set. Both sides see the same 18 of them, and
            # dropping them would hide the one class of row most likely to differ
            # between two readers.
        ids.append(str(r[0]))

    # Order-independent: the two sides must agree on the SET of rows addressed to
    # this tag, not on the order a gateway happened to return them in.
    digest = hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()

    return {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "where": "cloud-run" if os.environ.get("K_SERVICE") or
                 os.environ.get("CLOUD_RUN_JOB") else "local",
        "board": BOARD,
        "match": ME,
        "rows_returned": len(rows),
        "gateway_filtered": data.get("filtered"),
        "read_attempts": attempts,
        "dateable": len(dateable),
        "undateable": undateable,
        "newest": max(dateable) if dateable else None,
        "oldest": min(dateable) if dateable else None,
        "compared_through": CUTOFF or None,
        "rows_after_cutoff": after_cutoff,
        "rows_compared": len(ids),
        "row_id_sha256": digest,
        "credential_source": "injected" if (
            os.environ.get("BUS_URL") and os.environ.get("BUS_SECRET")) else "file",
    }


def carry_the_cursor(fp: dict) -> dict:
    """Advance a durable cursor to the newest row this run saw.

    WHAT THIS PROVES THAT A UNIT TEST CANNOT
        Every Cloud Run execution is a fresh container, so two executions are a
        restart. If the second one reports the first one's watermark as
        `previous`, state survived the container - which is the whole of cutover
        step 4 and cannot be demonstrated with a fake.

    WHY IT IS NOT A BOARD WRITE
        The read-only guard governs what this probe does to THE BOARD. This is the
        probe's own bookkeeping in its own GCS object - a different resource, a
        different permission. The job still cannot write a board row and the guard
        still proves it.

    WHY A FAILURE HERE IS REPORTED RATHER THAN RAISED
        The parity read is the job. Bookkeeping must not be able to stop it, so a
        broken or unreachable store degrades to a reported reason.
    """
    uri = os.environ.get("BLACKBOARD_STATE_URI") or ""
    if not uri:
        return {"backend": None, "reason": "BLACKBOARD_STATE_URI not set"}
    if state_store is None:
        return {"backend": uri, "reason": "state_store did not import"}
    newest = fp.get("newest")
    if not newest:
        return {"backend": uri, "reason": "nothing dateable to record"}
    try:
        store = state_store.open_store(uri)
        result = state_store.advance(store, CURSOR_NAME, "watermark", newest)
        result["backend"] = store.describe()
        return result
    except Exception as exc:  # noqa: BLE001
        return {"backend": uri, "reason": "%s: %s" % (type(exc).__name__, exc)}


def main() -> int:
    refuse_if_this_module_can_write()
    fp = fingerprint()
    fp["cursor"] = carry_the_cursor(fp)
    # One line of JSON, so the laptop side can diff it by equality rather than by
    # eye. Cloud Run Jobs put stdout straight into Cloud Logging, which parses a
    # single JSON line into jsonPayload rather than textPayload.
    print(json.dumps(fp, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
