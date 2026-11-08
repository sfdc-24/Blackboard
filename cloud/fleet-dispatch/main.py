#!/usr/bin/env python3
"""Fleet dispatch: one worker blocked on the cloud agents' Redis lanes. Default off.

Q1b of the Redis doorbell. Laptop agents wait on their own lanes (Q1a). This
process is the cloud half: gemini and claude-api are Cloud Run jobs, grok is an
HTTP endpoint that is still off. It does not decide a reply. It rings, through
cloud/inbox-wake's routes, and those agents read their own lanes.

THE FLAG. FLEET_DISPATCH must be exactly "on". Anything else, including unset,
idles. It does not import the Redis client and it does not open a socket. The
image also sets the variable to off. Turning it off is the rollback that does
not need a new build.

THE ALLOWLIST. FLEET_DISPATCH_AGENTS is a comma-separated list of canonical
ids. Empty, unset, or all unknown names wakes nobody: fail closed. A name whose
route is not a cloud route (the laptop agents go through GitHub Actions) is not
a lane this process blocks on.

THE LOOP. XREAD BLOCK 30000 on fleet:inbox:<agent>, from the cursor
fleet:dispatch:cursor:<agent>. One blocked read is one 30s window. Every new
entry is claimed with SET NX fleet:wake:seen:<agent>:<stream id>, the same
family inbox-wake uses, and the window collapses to one ring per agent carrying
the newest id. The ring is inbox-wake's Wakers.dispatch on that agent's ROUTES
entry. It is not reimplemented here.

THE CURSOR MOVES LAST. The ring is attempted, then
fleet:dispatch:last:<agent> records id, outcome and at, and only then is the
cursor set to the newest id. A crash after the ring and before that record
leaves the cursor behind, so the entry is read again. The seen key is already
held, so the second pass does not wake a second time, and it still records and
moves the cursor. One reconsideration, not a loop, and not a double wake.

Deploy is not this file's job. cloud/fleet-dispatch/deploy.sh can plan it.
Nothing here is applied.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

BLOCK_MS = 30000
FLAG = "FLEET_DISPATCH"
AGENTS_ENV = "FLEET_DISPATCH_AGENTS"
LANE_PREFIX = "fleet:inbox:"
CURSOR = "fleet:dispatch:cursor:"
LAST = "fleet:dispatch:last:"
# Kinds inbox-wake can ring from inside this VPC. github_actions is a laptop.
CLOUD_KINDS = ("cloud_run_job", "http")
START = "0-0"


def load_inbox_wake():
    """The route table and the seen-key, borrowed. The image copies main.py to
    /app/inbox_wake.py. A checkout loads the sibling file. Either way there is
    one implementation."""
    sibling = Path(__file__).resolve().parents[1] / "inbox-wake" / "main.py"
    if sibling.is_file():
        spec = importlib.util.spec_from_file_location("inbox_wake", sibling)
        module = importlib.util.module_from_spec(spec)
        sys.modules["inbox_wake"] = module
        spec.loader.exec_module(module)
        return module
    import inbox_wake  # noqa: PLC0415 - the flat image layout
    return inbox_wake


inbox_wake = load_inbox_wake()


def enabled_from_env(environ) -> bool:
    return environ.get(FLAG) == "on"


def cloud_agents():
    """Agents whose wake route this process can ring. Order is ROUTES order."""
    return tuple(
        agent for agent, (kind, _target) in inbox_wake.ROUTES.items()
        if kind in CLOUD_KINDS
    )


def allowed_agents(environ):
    """FLEET_DISPATCH_AGENTS, canonical cloud routes only. Empty wakes nobody."""
    cloud = cloud_agents()
    chosen = []
    for part in (environ.get(AGENTS_ENV) or "").split(","):
        name = inbox_wake.canonical(part)
        if name in cloud and name not in chosen:
            chosen.append(name)
    return tuple(chosen)


def connect(environ, factory):
    """A Redis client, or None. None means nothing was opened.

    The flag is checked before the allowlist, and the factory is not called
    for either refusal. A factory that connects as a side effect of being
    constructed must not run while this is off."""
    if not enabled_from_env(environ):
        return None
    if not allowed_agents(environ):
        return None
    return factory()


def lane(agent: str) -> str:
    return LANE_PREFIX + agent


def stream_id_key(entry_id):
    """Redis stream ids sort by millisecond, then sequence. Both are integers."""
    text = str(entry_id)
    ms, sep, seq = text.partition("-")
    if not sep or not ms.isdigit() or not seq.isdigit():
        raise ValueError(text)
    return (int(ms), int(seq))


def textify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def cursor_of(client, agent: str) -> str:
    value = textify(client.get(CURSOR + agent))
    return value or START


def message_for(agent: str, entry_id: str, fields: dict) -> dict:
    """The id the ring carries is the stream id. A task_id in the fields is the
    writer's own id; inbox-wake's sweep uses it, and this process must claim
    the same seen-key family under the stream id the spec names."""
    return {
        "id": entry_id,
        "to": agent,
        "by": textify(fields.get("by")),
        "text": textify(fields.get("text")),
    }


def record_last(client, agent: str, entry_id: str, outcome: str, at: int) -> None:
    client.set(LAST + agent, json.dumps(
        {"at": at, "id": entry_id, "outcome": outcome},
        separators=(",", ":"), sort_keys=True))


def write_cursor(client, agent: str, entry_id: str) -> None:
    client.set(CURSOR + agent, entry_id)


def release_claims(seen, agent: str, claimed) -> None:
    for entry_id in claimed:
        seen.release("%s:%s" % (agent, entry_id))


def invoke_ring(seen, agent, newest_id, fields, ring, claimed) -> str:
    """Ring, and give the claim back if the ring did not happen.

    A failure here is an attempt: the caller still records it and still moves
    the cursor, so a missing GROK_WAKE_URL cannot spin the loop. Releasing the
    claim lets inbox-wake's sweep try the same id later. A success keeps the
    claim, which is what stops the crash retry from waking twice."""
    try:
        ring(agent, message_for(agent, newest_id, fields))
    except inbox_wake.Unconfigured:
        release_claims(seen, agent, claimed)
        return "unconfigured"
    except Exception:
        release_claims(seen, agent, claimed)
        return "error"
    return "woke"


def deliver(client, agent: str, entries, ring, clock) -> dict:
    """One agent, one window. Claim every new id, ring the newest, record, then
    move the cursor. record_last raises straight through: write_cursor is not
    reached, and the next pass reads this window again."""
    usable = []
    for entry_id, fields in entries:
        entry_id = textify(entry_id)
        try:
            stream_id_key(entry_id)
        except ValueError:
            continue
        usable.append((entry_id, fields or {}))
    if not usable:
        return {"agent": agent, "outcome": "empty"}
    usable.sort(key=lambda item: stream_id_key(item[0]))
    seen = inbox_wake.RedisSeen(client)
    claimed = []
    for entry_id, _fields in usable:
        if seen.claim("%s:%s" % (agent, entry_id)):
            claimed.append(entry_id)
    newest_id, newest_fields = usable[-1]
    # The seen key was taken before the ring. A crash after the ring and before
    # record_last leaves the cursor behind, so this entry is read again. The
    # claim fails that second time: that is the double wake the NX stops. The
    # duplicate outcome is still recorded, and only then does the cursor move.
    if newest_id not in claimed:
        outcome = "duplicate"
    else:
        outcome = invoke_ring(seen, agent, newest_id, newest_fields, ring, claimed)
    at = int(clock())
    record_last(client, agent, newest_id, outcome, at)
    write_cursor(client, agent, newest_id)
    return {"agent": agent, "id": newest_id, "outcome": outcome, "at": at}


def parse_xread(raw):
    """redis-py's xread shape: [[stream, [(id, fields), ...]], ...]. None and
    [] are a timeout, which is a quiet window."""
    parsed = []
    for item in raw or []:
        stream, entries = item[0], item[1]
        rows = []
        for entry in entries or []:
            rows.append((textify(entry[0]), entry[1] or {}))
        parsed.append((textify(stream), rows))
    return parsed


def tick(client, agents, ring, clock=time.time) -> list:
    """One XREAD BLOCK. Entries already in the lane collapse with whatever
    arrives before the block returns: one ring per agent, the newest id."""
    streams = {lane(agent): cursor_of(client, agent) for agent in agents}
    if not streams:
        return []
    raw = client.xread(streams, block=BLOCK_MS)
    results = []
    allowed = set(agents)
    for stream, entries in parse_xread(raw):
        if not stream.startswith(LANE_PREFIX):
            continue
        agent = stream[len(LANE_PREFIX):]
        if agent not in allowed or not entries:
            continue
        results.append(deliver(client, agent, entries, ring, clock))
    return results


def ring_for(wakers):
    """Dispatch through inbox-wake's route table. The target lives in ROUTES."""
    def ring(agent, message):
        wakers.dispatch(agent, inbox_wake.ROUTES[agent], message)
    return ring


def production_wakers(environ):
    return inbox_wake.Wakers(
        grok_url=environ.get("GROK_WAKE_URL") or "",
        token_provider=inbox_wake._metadata_token,
    )


def redis_client():
    """The fleet's client, opened only after the flag and the allowlist have
    allowed it. precheck=False: this process exists to block on the socket,
    and a 1.5s probe is how a cold VPC interface looks unreachable."""
    import redis_dual  # noqa: PLC0415 - not imported while the flag is off
    return redis_dual.client(redis_dual.Settings(), precheck=False)


def _idle(sleep, iterations, reason) -> None:
    print("fleet-dispatch idle: %s" % reason, flush=True)
    if iterations is not None:
        return
    while True:
        sleep(3600)


def run(environ=None, factory=None, ring=None, sleep=time.sleep,
        iterations=None, clock=time.time) -> str:
    """Block forever, or for `iterations` ticks in a test. Off, and an empty
    allowlist, return without calling factory."""
    environ = os.environ if environ is None else environ
    if not enabled_from_env(environ):
        _idle(sleep, iterations, "FLEET_DISPATCH is not on")
        return "idle"
    agents = allowed_agents(environ)
    if not agents:
        _idle(sleep, iterations, "FLEET_DISPATCH_AGENTS is empty")
        return "idle"
    client = (factory or redis_client)()
    if client is None:
        _idle(sleep, iterations, "no redis client")
        return "no_client"
    if ring is None:
        ring = ring_for(production_wakers(environ))
    steps = 0
    while iterations is None or steps < iterations:
        for item in tick(client, agents, ring, clock):
            # id and outcome only. The lane text is the message, and it does
            # not belong in the log.
            print("fleet-dispatch agent=%s id=%s outcome=%s" % (
                item.get("agent"), item.get("id"), item.get("outcome")), flush=True)
        steps += 1
    return "ran"


def main() -> None:
    run()


if __name__ == "__main__":
    main()
