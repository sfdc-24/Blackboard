#!/usr/bin/env python3
"""Capability observations beside the roster: one entrypoint, three verbs, and NO SEED IN IT.

    python scripts/roster_capability.py check     # the file alone: shapes, states, bases, real times
    python scripts/roster_capability.py verify    # READ ONLY: compare Redis with the file, change nothing
    python scripts/roster_capability.py write     # write only cap: fields, each by one compare-and-set

EXACTLY ONE ARGUMENT, AND IT IS ONE OF THOSE THREE. Anything else - no argument, a second argument,
an unknown word, `seed` - is refused with exit 2 before the file is read or a connection is made.
This lived inside roster_seed.py for one commit, and `roster_seed.py seed capability` ran the
destructive seed, because that file dispatches on its first word (Aya on be4a776). So it has its own
file, and that file cannot seed: it does not import the seeder.

WHAT AN OBSERVATION IS (the owner's capability map, 2026-10-12)
    What was SEEN of one roster instance on one resource: read, push, review, merge, deploy, each
    its own state, for one named runtime identity. Each state says
        can       yes, no or unknown
        status    verified, reported or unknown
        basis     executed, acl, stated or none
        source_event, evidence_at, checked_at
    AN OBSERVATION IS NOT A GRANT, and unknown means nobody looked, not "no".
    VERIFIED MEANS EXECUTED: that runtime did the thing and it was observed. What an access list says
    (basis acl) and what somebody says (basis stated) are REPORTED. A harness can refuse what the
    list allows, and the list can allow what was never tried, so the two are never the same claim.
    STALE is told from checked_at, the time somebody last looked: over a day, and it reads STALE.

WHERE IT LIVES
    The original is scripts/agent_capabilities.json. Redis holds a copy: one field, cap:<resource>,
    on the EXISTING hash v1:agent:<id>, holding one canonical JSON value. No new key, no index.

WHAT `write` MAY DO, AND NOTHING ELSE
    One compare-and-set per observation, in one Lua script on the server (CAS_LUA): the id is a
    member of v1:agent:roster, every one of the fifteen canonical fields exists on its hash, and the
    cap: field still holds exactly what was read a moment before; only then is that ONE field set.
    A missing or incomplete hash is refused, never created. No DEL, HDEL, SADD or SREM is ever sent.
    BEFORE ANY WRITE, every observation is judged against what Redis holds:
        the same value              a retry: nothing is written
        a check OLDER than stored   refused as stale: newer evidence is not overwritten
        the same check time, a different state      refused as a conflict
        a stored value this cannot read             refused: it is not overwritten
    ONE REFUSAL AND NOTHING IS WRITTEN for any observation.
    It never loads the roster FILE: a roster baked into an old image is an old roster.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redis_dual                                                        # noqa: E402

CAPABILITIES = Path(__file__).resolve().parent / "agent_capabilities.json"
VERBS = ("check", "verify", "write")
USAGE = "usage: roster_capability.py check|verify|write   (exactly one argument; this file cannot seed)"

# The roster's own names, said again here rather than imported: this file does not import the seeder.
# tests/test_roster_seed.py keeps them equal to roster_seed's.
AGENT_KEY = redis_dual.KEY_VERSION + "agent:%s"
ROSTER_SET = redis_dual.KEY_VERSION + "agent:roster"
CANONICAL_FIELDS = ("family", "name", "role", "responsibility", "surface", "runtime", "gcp_identity",
                    "state", "in_vpc", "redis_read", "redis_write", "why", "wake", "rows", "last_seen")
CAP_PREFIX = "cap:"

RIGHTS = ("read", "push", "review", "merge", "deploy")
CAN = ("yes", "no", "unknown")                       # what was observed. Never "granted", never "denied".
STATUS = ("verified", "reported", "unknown")
BASIS = ("executed", "acl", "stated", "none")
BASIS_FOR = {"verified": ("executed",), "reported": ("acl", "stated"), "unknown": ("none",)}
RIGHT_KEYS = ("can", "status", "basis", "source_event", "evidence_at", "checked_at")
STALE_AFTER_SECONDS = 24 * 60 * 60
FUTURE_SKEW_SECONDS = 5 * 60                         # a clock may run a little ahead; a time beyond this is refused
CAP_VALUE_BYTES = 4096
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RESOURCE = re.compile(r"^[a-z][a-z0-9]{1,15}:[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# COMPARE-AND-SET, ONE COMMAND ON THE SERVER. KEYS: the instance hash, the roster set. ARGV: the id,
# the cap field, the value read a moment ago ("" for absent), the new value, then the canonical fields.
# A read followed by a separate HSET could overwrite evidence written in between, or create a hash
# that was deleted in between (Aya on be4a776); in one script the checks and the write are one step.
CAS_LUA = """
if redis.call('sismember', KEYS[2], ARGV[1]) ~= 1 then return 'not-a-member' end
for i = 5, #ARGV do
  if redis.call('hexists', KEYS[1], ARGV[i]) ~= 1 then return 'incomplete' end
end
local held = redis.call('hget', KEYS[1], ARGV[2])
if held == false then held = '' end
if held ~= ARGV[3] then return 'changed' end
redis.call('hset', KEYS[1], ARGV[2], ARGV[4])
return 'written'
"""


def load(path=None) -> dict:
    return json.loads(Path(path or CAPABILITIES).read_text(encoding="utf-8"))


def _time(stamp):
    """The moment a stamp names, or None when it is not a real time in the one accepted form."""
    if not isinstance(stamp, str) or not _STAMP.match(stamp):
        return None
    try:
        return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:                                # 2026-02-30 has the shape and is not a day
        return None


def _plain(value, limit) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(ch) < 32 for ch in value)


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def check(data, now=None) -> list:
    """Everything wrong with the capability file. Empty means it may be written.

    STRICT ON PURPOSE: this is read by agents deciding who to hand work to."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    latest = now + datetime.timedelta(seconds=FUTURE_SKEW_SECONDS)
    problems, seen = [], set()
    if not isinstance(data, dict) or not isinstance(data.get("observations"), list):
        return ["the file needs an observations list"]
    if not data.get("version") or not data.get("updated"):
        problems.append("the file needs a version and an updated date")
    for index, obs in enumerate(data["observations"], 1):
        where = "observation %d" % index
        if not isinstance(obs, dict):
            problems.append("%s is not an object" % where)
            continue
        rid, resource = obs.get("id"), obs.get("resource")
        if not isinstance(rid, str) or not _ID.match(rid):
            problems.append("%s: id %r is not an instance id" % (where, rid))
            continue
        if not isinstance(resource, str) or not _RESOURCE.match(resource):
            problems.append("%s (%s): resource %r must be <kind>:<name>, e.g. github:owner/repo" % (where, rid, resource))
            continue
        where = "%s on %s" % (rid, resource)
        if (rid.lower(), resource) in seen:
            problems.append("%s appears twice" % where)
        seen.add((rid.lower(), resource))
        if set(obs) != {"id", "resource", "runtime_identity", "rights"}:
            problems.append("%s: an observation is id, resource, runtime_identity and rights, nothing else" % where)
        if not _plain(obs.get("runtime_identity"), 200):
            problems.append("%s: no runtime_identity. Say WHICH login or account was observed" % where)
        rights = obs.get("rights")
        if not isinstance(rights, dict) or set(rights) != set(RIGHTS):
            problems.append("%s: rights must be exactly %s, each its own state" % (where, ", ".join(RIGHTS)))
            continue
        sound = True
        for right in RIGHTS:
            state = rights[right]
            before = len(problems)
            if not isinstance(state, dict) or set(state) != set(RIGHT_KEYS):
                problems.append("%s, %s: a state is exactly %s" % (where, right, ", ".join(RIGHT_KEYS)))
                sound = False
                continue
            can, status, basis = state["can"], state["status"], state["basis"]
            if can not in CAN or status not in STATUS or basis not in BASIS:
                problems.append("%s, %s: can is one of %s, status one of %s, basis one of %s; not %r / %r / %r. "
                                "An observation is never a grant or a denial" % (where, right, CAN, STATUS, BASIS, can, status, basis))
                sound = False
                continue
            if (can == "unknown") != (status == "unknown"):
                problems.append("%s, %s: unknown is unknown in both, or in neither (%s / %s)" % (where, right, can, status))
            if basis not in BASIS_FOR[status]:
                problems.append("%s, %s: %s needs basis %s, not %s. An access list or a statement is REPORTED; "
                                "only what the runtime did is verified" % (where, right, status, " or ".join(BASIS_FOR[status]), basis))
            checked = _time(state["checked_at"])
            if checked is None:
                problems.append("%s, %s: checked_at must be a real time, YYYY-MM-DDTHH:MM:SSZ: when somebody last looked" % (where, right))
            elif checked > latest:
                problems.append("%s, %s: checked_at is in the future" % (where, right))
            if status == "unknown":
                if state["evidence_at"] != "" or not isinstance(state["source_event"], str):
                    problems.append("%s, %s: unknown has no evidence time" % (where, right))
            else:
                if not _plain(state["source_event"], 240):
                    problems.append("%s, %s: %s needs its source_event: what was run, or who said it" % (where, right, status))
                evidence = _time(state["evidence_at"])
                if evidence is None:
                    problems.append("%s, %s: evidence_at must be a real time, YYYY-MM-DDTHH:MM:SSZ" % (where, right))
                elif evidence > latest:
                    problems.append("%s, %s: evidence_at is in the future" % (where, right))
                elif checked is not None and evidence > checked:
                    problems.append("%s, %s: the evidence is dated after its own check" % (where, right))
            sound = sound and len(problems) == before
        if sound and len(cap_value(obs).encode("utf-8")) > CAP_VALUE_BYTES:
            problems.append("%s: the value is over %d bytes" % (where, CAP_VALUE_BYTES))
    return problems


def cap_field(resource: str) -> str:
    return CAP_PREFIX + resource


def cap_value(obs) -> str:
    """The one JSON value stored for an observation: canonical, so a read-back can be compared exactly."""
    return json.dumps({"runtime_identity": obs["runtime_identity"], "rights": obs["rights"]},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def staleness(state, now=None) -> str:
    """fresh, STALE or unknown, from when somebody last LOOKED, not from when the evidence is dated:
    an old fact checked this morning is fresh, and a fact nobody has checked since yesterday is not."""
    if state.get("status") == "unknown":
        return "unknown"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return "STALE" if (now - _time(state["checked_at"])).total_seconds() > STALE_AFTER_SECONDS else "fresh"


def expected(data) -> dict:
    """{instance key: {cap field: value}}: exactly what `write` holds Redis to, used by write and by verify."""
    out = {}
    for obs in data["observations"]:
        out.setdefault(AGENT_KEY % obs["id"], {})[cap_field(obs["resource"])] = cap_value(obs)
    return out


def decide(stored, obs) -> tuple:
    """("write" | "same" | "refuse", why) for one observation against what Redis holds for its field.

    NEWER EVIDENCE IS NEVER OVERWRITTEN BY OLDER, and two different claims about one moment are a
    conflict a person settles, not something the later writer wins."""
    stored = _text(stored)
    if stored in (None, ""):
        return "write", ""
    if stored == cap_value(obs):
        return "same", ""
    try:
        held = json.loads(stored)
        pairs = [(right, held["rights"][right], obs["rights"][right]) for right in RIGHTS]
        times = [(right, _time(old["checked_at"]), _time(new["checked_at"]), old, new) for right, old, new in pairs]
    except (ValueError, KeyError, TypeError):
        return "refuse", "the stored value is not one this path can read, so it is not overwritten"
    if any(was is None for _, was, _, _, _ in times):
        return "refuse", "the stored value is not one this path can read, so it is not overwritten"
    newer = False
    for right, was, now, old, new in times:
        if now < was:
            return "refuse", ("stale: %s was checked at %s in Redis and at %s here; newer evidence is not overwritten"
                              % (right, old["checked_at"], new["checked_at"]))
        if now == was and old != new:
            return "refuse", "conflict: %s has the same check time (%s) and a different state" % (right, new["checked_at"])
        newer = newer or now > was
    if not newer:
        return "refuse", "conflict: no check here is newer than the stored one, and the value differs"
    return "write", ""


def _members(conn) -> set:
    return {_text(member) for member in (conn.smembers(ROSTER_SET) or set())}


def _hash(conn, key) -> dict:
    return {_text(name): _text(value) for name, value in (conn.hgetall(key) or {}).items()}


def write(conn, data) -> dict:
    """Judge every observation, then compare-and-set the ones that are new. See the module's account."""
    out = {"written": [], "same": [], "refused": [], "lost": [], "changed_elsewhere": [], "not_read_back": []}
    members, plan, before = _members(conn), [], {}
    for obs in data["observations"]:
        key, field = AGENT_KEY % obs["id"], cap_field(obs["resource"])
        where = "%s %s" % (key, field)
        if obs["id"] not in members:
            out["refused"].append("%s: %s is not in %s in Redis; this path creates no instance" % (where, obs["id"], ROSTER_SET))
            continue
        if key not in before:
            before[key] = _hash(conn, key)
        held = before[key]
        absent = [name for name in CANONICAL_FIELDS if name not in held]
        if absent:
            out["refused"].append("%s: the hash is missing or incomplete (%d of %d canonical fields absent); it is "
                                  "not created or completed here" % (where, len(absent), len(CANONICAL_FIELDS)))
            continue
        verdict, why = decide(held.get(field), obs)
        if verdict == "refuse":
            out["refused"].append("%s: %s" % (where, why))
            continue
        plan.append((where, obs, key, field, verdict, held.get(field, "")))
    if out["refused"]:
        return out                                    # one refusal, and nothing is written for anyone
    ours, lost = {}, set()
    for where, obs, key, field, verdict, held in plan:
        ours.setdefault(key, set()).add(field)
        if verdict == "same":
            out["same"].append(where)
            continue
        answer = _text(conn.eval(CAS_LUA, 2, key, ROSTER_SET, obs["id"], field, held, cap_value(obs), *CANONICAL_FIELDS))
        if answer == "written":
            out["written"].append(where)
        else:
            # Somebody moved it between the read and the write. Nothing was set; say which and why.
            out["lost"].append("%s: %s" % (where, answer))
            lost.add(where)
    for where, obs, key, field, verdict, held in plan:
        after = _hash(conn, key)
        if where not in lost and after.get(field) != cap_value(obs):
            out["not_read_back"].append(where)
        for name in sorted((set(before[key]) | set(after)) - ours[key]):
            line = "%s: field %s" % (key, name)
            if before[key].get(name) != after.get(name) and line not in out["changed_elsewhere"]:
                out["changed_elsewhere"].append(line)
    return out


def verify(conn, data) -> list:
    """READ ONLY. Drift between the capability file and its copy, reported and never corrected. Sends
    SMEMBERS and HGETALL and nothing else. A cap: field in Redis that the file does not hold is drift
    too: the original is the file."""
    drift = []
    want = expected(data)
    members = _members(conn)
    for obs in data["observations"]:
        if obs["id"] not in members:
            drift.append("%s: %s is not in %s in Redis" % (AGENT_KEY % obs["id"], obs["id"], ROSTER_SET))
    for key in sorted(set(want) | {AGENT_KEY % member for member in members}):
        held = _hash(conn, key)
        stored = {name: value for name, value in held.items() if name.startswith(CAP_PREFIX)}
        fields = want.get(key, {})
        if fields and any(name not in held for name in CANONICAL_FIELDS):
            drift.append("%s: the hash is missing or incomplete" % key)
        for name, value in sorted(fields.items()):
            if name not in stored:
                drift.append("%s: %s is in the file and not in Redis" % (key, name))
            elif stored[name] != value:
                drift.append("%s: %s differs" % (key, name))
        for name in sorted(set(stored) - set(fields)):
            drift.append("%s: %s is in Redis and not in the file" % (key, name))
    return drift


def table(data, now=None) -> str:
    """What the file says, one line a right: the state, how it is known, on what basis, and whether the check is fresh."""
    lines = []
    for obs in data["observations"]:
        lines.append("%s on %s, observed for: %s" % (obs["id"], obs["resource"], obs["runtime_identity"]))
        for right in RIGHTS:
            state = obs["rights"][right]
            lines.append("    %-6s %-7s %-8s %-8s %-7s checked %s  %s" % (
                right, state["can"], state["status"], state["basis"], staleness(state, now), state["checked_at"],
                state["source_event"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in VERBS:
        print("REFUSED: " + USAGE)
        return 2                                      # before the file is read and before any connection
    what = argv[0]
    data = load()
    problems = check(data)
    for problem in problems:
        print("REFUSED: " + problem)
    if problems:
        return 1
    print("capability file OK: %d observation(s), version %s, updated %s"
          % (len(data["observations"]), data.get("version"), data.get("updated")))
    print(table(data))
    if what == "check":
        return 0
    conn = redis_dual.client(redis_dual.Settings(), precheck=False)
    if conn is None:
        print("UNKNOWN: no Redis connection, so nothing was %s. This is not success."
              % ("written" if what == "write" else "verified"))
        return 3
    bad = False
    if what == "write":
        out = write(conn, data)
        for line in out["refused"]:
            print("REFUSED, NOTHING WRITTEN FOR ANYONE: " + line)
        if out["refused"]:
            return 1
        print("written: %d field(s) by compare-and-set; already the same: %d; nothing else was sent"
              % (len(out["written"]), len(out["same"])))
        for line in out["lost"]:
            print("NOT WRITTEN, IT MOVED BETWEEN THE READ AND THE WRITE: " + line)
        for line in out["changed_elsewhere"]:
            print("CHANGED WHILE WRITING (not by this path): " + line)
        for line in out["not_read_back"]:
            print("NOT READ BACK AS WRITTEN: " + line)
        bad = bool(out["lost"] or out["changed_elsewhere"] or out["not_read_back"])
    drift = verify(conn, data)
    for line in drift:
        print("DRIFT: " + line)
    bad = bad or bool(drift)
    print("VERDICT: %s" % ("every capability field in Redis is as the file has it" if not bad else "NOT CLEAN, see above"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
