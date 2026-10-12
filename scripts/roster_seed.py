"""Seed the roster into Redis from the repository, and verify the copy against the original.

THE ROSTER IS scripts/agent_roster.json. REDIS HOLDS A COPY AND NEVER THE ORIGINAL.
Gemini, architect lead, 2026-10-06 14:34:52Z: "Because Redis is volatile, progress state must be
reconstructible from the repository. Redis acts only as an ephemeral accelerator." `persistenceMode`
is DISABLED on redis-central, so a failover loses every key here. Nothing in Redis may be the only
copy of anything, and a roster authored in Redis would be exactly that.

So this is one-way by construction: it reads the file and writes Redis. There is no verb that reads
Redis and writes the file, and there should never be one.

WHAT IT WRITES, all under the v1 keyspace version ruled on 2026-10-06 15:21:43Z:

    v1:agent:<id>          HASH   one per INSTANCE: family, role, responsibility, runtime,
                                  gcp_identity, in_vpc, redis_read, redis_write, state, why
    v1:agent:family:<fam>  HASH   the family's role and responsibility, and its instance ids
    v1:agent:roster        SET    every instance id that may send or receive
    v1:agent:canon         HASH   alias -> canonical instance id, EVERY KEY LOWERCASED
    v1:agent:seeded        HASH   when, from which file digest, by which runtime

AND ONE THING BESIDE THE ROSTER, by a path of its own (the owner's capability map, 2026-10-12):

    v1:agent:<id>  field cap:<resource>   one JSON value: what was OBSERVED of this instance on one
                                          resource - read, push, review, merge, deploy, each its own
                                          state - for which runtime identity, from which event, when
                                          the evidence is from and when it was last checked.

    Its original is scripts/agent_capabilities.json, as the roster's is agent_roster.json.
    AN OBSERVATION IS NOT A GRANT. It says yes, no or unknown, and verified, reported or unknown.
    Nothing here gives or takes a right, and "unknown" means nobody looked, not "no".
    THE PATH IS NARROW ON PURPOSE. `capability` writes one hash field per observation and nothing
    else: not the fifteen roster fields, not a family, not the membership set, not the alias table,
    not the seed metadata. It never runs a seed, so a roster baked into an old image cannot be put
    back over the current one by asking for a capability. It refuses an id that is not ALREADY a
    member of v1:agent:roster in Redis, so it cannot create an instance either.

BY INSTANCE AND NOT BY FAMILY, because the owner is right that they are different things: Claude is
claude-code-cli AND claude-api AND pi1-cli - one role across several runtimes - and an access answer
that names only the family answers nothing.

THE ALIAS TABLE IS THE POINT. Codex has written to the board under SEVEN different sender tags, three
of them with per-session suffixes, and a case-sensitive watcher has already missed CODEX-DESKTOP rows
once. Every alias is lowercased on the way in and an id nobody owns is refused at send time rather
than vanishing.

NOTHING HERE IS A PRODUCTION PATH. It writes reference data that no running code reads yet, so it
does not touch the dual-run hold: that hold is on production paths CONSULTING Redis, and seeding a
table nothing consults is not that.

    python scripts/roster_seed.py check    # the file alone: valid, no duplicate ids, aliases sane
    python scripts/roster_seed.py seed     # write it to Redis, then verify what landed
    python scripts/roster_seed.py verify   # compare Redis against the file, report drift, change nothing
    python scripts/roster_seed.py table    # print the markdown table, so docs cannot drift from data
    python scripts/roster_seed.py capability-check   # the capability file alone: shapes, states, times
    python scripts/roster_seed.py capability         # write ONLY the cap: fields, then read them back
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redis_dual                                                        # noqa: E402

ROSTER = Path(__file__).resolve().parent / "agent_roster.json"
AGENT_KEY = redis_dual.KEY_VERSION + "agent:%s"
FAMILY_KEY = redis_dual.KEY_VERSION + "agent:family:%s"
ROSTER_SET = redis_dual.KEY_VERSION + "agent:roster"
CANON = redis_dual.KEY_VERSION + "agent:canon"
SEEDED = redis_dual.KEY_VERSION + "agent:seeded"

FIELDS = ("family", "name", "role", "responsibility", "surface", "runtime", "gcp_identity",
          "state", "in_vpc", "redis_read", "redis_write", "why", "wake", "rows", "last_seen")

# --- capability observations: a field beside the fifteen, never one of them ---------------------
CAPABILITIES = Path(__file__).resolve().parent / "agent_capabilities.json"
CAP_PREFIX = "cap:"
RIGHTS = ("read", "push", "review", "merge", "deploy")
CAN = ("yes", "no", "unknown")                       # what was observed. Never "granted", never "denied".
STATUS = ("verified", "reported", "unknown")         # how it is known: measured, said by someone, or not at all
RIGHT_KEYS = ("can", "status", "source_event", "evidence_at", "checked_at")
STALE_AFTER_SECONDS = 24 * 60 * 60                   # checked longer ago than this reads as STALE
CAP_VALUE_BYTES = 4096
_CAP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CAP_RESOURCE = re.compile(r"^[a-z][a-z0-9]{1,15}:[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_CAP_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def load(path=None) -> dict:
    return json.loads(Path(path or ROSTER).read_text(encoding="utf-8"))


def digest(path=None) -> str:
    text = Path(path or ROSTER).read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def instances(data) -> list:
    """Every instance, flattened, each carrying its family's role and responsibility."""
    out = []
    for family in data["families"]:
        for inst in family["instances"]:
            row = dict(inst)
            row["family"] = family["family"]
            row["name"] = family["name"]
            row["role"] = family["lead_role"]
            row["responsibility"] = family["responsibility"]
            out.append(row)
    return out


def aliases(data) -> dict:
    """{lowercased alias: canonical instance id}. An id is always an alias of itself."""
    table = {}
    for row in instances(data):
        table[row["id"].lower()] = row["id"]
    for family in data["families"]:
        # A family name resolves to its FIRST instance: "codex" means the desktop, not a guess.
        first = family["instances"][0]["id"] if family["instances"] else ""
        if first:
            table.setdefault(family["family"].lower(), first)
    return table


def check(data) -> list:
    """Everything wrong with the file. Empty means it may be seeded."""
    problems, seen = [], {}
    for row in instances(data):
        rid = row.get("id", "")
        if not rid:
            problems.append("an instance has no id")
            continue
        if rid.lower() in seen:
            problems.append("instance id %r appears twice (%s and %s)" % (rid, seen[rid.lower()], row["family"]))
        seen[rid.lower()] = row["family"]
        for flag in ("in_vpc", "redis_read", "redis_write"):
            if not isinstance(row.get(flag), bool):
                problems.append("%s: %s must be true or false, not %r" % (rid, flag, row.get(flag)))
        if not row.get("why"):
            problems.append("%s: no reason given for its access verdict" % rid)
        # EVERY INSTANCE MUST NAME ITS DOORBELL, and "NONE" is a legitimate answer.
        #
        # On 2026-10-06 the owner said "wake Aya and get it to post the row". Four channels were
        # tried in turn before the answer was known: agent_waker.py (no aya entry), the grok API
        # route (answered in writing that it has no dispatch hands), `codex queue` (no active
        # session) and wa_send.py (delivers only to the owner's own number). Aya had been carried
        # here for a day as "mobile voice assistant / phone" - a surface, never a route - so the
        # hour went on rediscovering that nothing on this box can ring it.
        #
        # A roster that lists who exists and not how to reach them turns every wake request into
        # the same search. So an instance with no `wake` is REFUSED, and an instance nothing can
        # wake must say NONE and say what it would take. The gap is then written down once.
        if not row.get("wake"):
            problems.append("%s: no wake path named. Say how to ring it, or NONE and what it "
                            "would take - an unnamed doorbell gets rediscovered every time" % rid)
        # An access claim that contradicts reachability is the kind of thing that goes unnoticed.
        if row.get("redis_read") and not row.get("in_vpc"):
            problems.append("%s claims redis_read while outside the VPC, which is not possible" % rid)
        if row.get("redis_write") and not row.get("redis_read"):
            problems.append("%s claims write without read" % rid)
    if not data.get("version") or not data.get("updated"):
        problems.append("the file needs a version and an updated date")
    return problems


def expected(data) -> dict:
    """Exactly what a seed writes: {key: mapping} for every hash, plus the membership set.

    One description, used by BOTH seed() and verify(). They used to describe the copy separately,
    which is how verify() came to check the instance hashes and silently ignore the families, the
    alias table and the seed metadata - so a half-written copy produced an empty drift list and the
    CLI's "the copy matches the file"."""
    hashes = {}
    for row in instances(data):
        hashes[AGENT_KEY % row["id"]] = {
            f: ("true" if row.get(f) is True else "false" if row.get(f) is False
                else str(row.get(f, ""))) for f in FIELDS}
    for family in data["families"]:
        hashes[FAMILY_KEY % family["family"]] = {
            "name": family["name"], "role": family["lead_role"],
            "responsibility": family["responsibility"], "appointed": family.get("appointed", ""),
            "instances": ",".join(i["id"] for i in family["instances"])}
    table = aliases(data)
    if table:
        hashes[CANON] = dict(table)
    hashes[SEEDED] = {"at": data.get("updated", ""), "file_digest": digest(),
                      "version": data.get("version", ""), "by": "roster_seed.py"}
    return {"hashes": hashes, "members": {row["id"] for row in instances(data)}}


def seed(conn, data) -> dict:
    """Make the copy match the file. Adds, replaces AND removes.

    THE SEED MUST CONVERGE. Copilot, PR 323: seeding only ever added, so an instance removed or
    renamed in the file left its membership and its hash behind in Redis, every later verify
    reported that id as drift, and the documented seeding operation could not bring the copy back
    into agreement - the one thing a seeder exists to do. Each hash is REPLACED rather than merged,
    for the same reason: a field dropped from the file would otherwise survive forever.

    It removes only what IT manages - the ids in the roster set and the aliases in the canon hash.
    An unrelated key under v1: is none of this function's business and is left alone."""
    want = expected(data)
    counts = {"instances": 0, "families": 0, "aliases": 0, "removed_instances": 0,
              "removed_aliases": 0}

    stale_ids = set(conn.smembers(ROSTER_SET) or set()) - want["members"]
    for row_id in sorted(stale_ids):
        conn.delete(AGENT_KEY % row_id)
        conn.srem(ROSTER_SET, row_id)
        counts["removed_instances"] += 1

    stale_aliases = set(conn.hgetall(CANON) or {}) - set(want["hashes"].get(CANON, {}))
    for alias in sorted(stale_aliases):
        conn.hdel(CANON, alias)
        counts["removed_aliases"] += 1

    for key, mapping in want["hashes"].items():
        conn.delete(key)                      # replace, never merge
        conn.hset(key, mapping=mapping)
        if key.startswith(redis_dual.KEY_VERSION + "agent:family:"):
            counts["families"] += 1
        elif key == CANON:
            counts["aliases"] = len(mapping)
        elif key != SEEDED:
            counts["instances"] += 1
    for row_id in sorted(want["members"]):
        conn.sadd(ROSTER_SET, row_id)
    return counts


def verify(conn, data) -> list:
    """Drift between the file and the copy. Reported, never corrected: a verify that fixes what it
    finds is a seed wearing a read-only name.

    IT CHECKS EVERYTHING A SEED WRITES. Copilot, PR 323: a missing instance hash was silently
    skipped, and the family hashes, the alias table and the seed metadata were never compared at
    all - so an incomplete or corrupted copy produced an empty drift list and the CLI printed "the
    copy matches the file". A verify that can only confirm is not a verify. It now walks expected()
    - the same description seed() writes from - so the two cannot drift apart either."""
    drift = []
    want = expected(data)
    instance_keys = {AGENT_KEY % member for member in want["members"]}

    stored_ids = set(conn.smembers(ROSTER_SET) or set())
    for extra in sorted(stored_ids - want["members"]):
        drift.append("in Redis and not in the file: %s" % extra)
    for missing in sorted(want["members"] - stored_ids):
        drift.append("in the file and not in Redis: %s" % missing)

    for key, mapping in sorted(want["hashes"].items()):
        stored = conn.hgetall(key) or {}
        if not stored:
            # NOT skipped. An id in the membership set whose hash is absent is the exact shape of a
            # half-written copy, and silence about it was what let that pass as agreement.
            drift.append("missing from Redis entirely: %s" % key)
            continue
        if key == SEEDED:
            # The metadata is compared on the one field that means anything: whether the copy was
            # made from THIS file. Absent metadata is itself drift.
            if not stored.get("file_digest"):
                drift.append("%s: no file_digest, so the copy cannot be traced to a file" % key)
            elif stored["file_digest"] != digest():
                drift.append("the file has changed since it was seeded (digest %s, now %s)"
                             % (stored["file_digest"], digest()))
            continue
        for name, value in sorted(mapping.items()):
            if stored.get(name, "") != value:
                drift.append("%s: field %s differs" % (key, name))
        for name in sorted(set(stored) - set(mapping)):
            # A capability field on an INSTANCE hash is not roster drift: it has its own original
            # and its own verify (verify_capabilities). Anything else beside the fifteen still is,
            # and so is a cap: field on a family, the alias table or the seed metadata.
            if key in instance_keys and name.startswith(CAP_PREFIX):
                continue
            drift.append("%s: field %s is in Redis and not in the file" % (key, name))
    return drift


def load_capabilities(path=None) -> dict:
    return json.loads(Path(path or CAPABILITIES).read_text(encoding="utf-8"))


def _when(stamp):
    return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def _plain(value, limit) -> bool:
    """A short piece of text with no control character in it."""
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(ch) < 32 for ch in value)


def check_capabilities(data) -> list:
    """Everything wrong with the capability file. Empty means it may be written.

    STRICT ON PURPOSE: this is read by agents deciding who to hand work to. A state outside the
    lists, a right left out, a time that is not a time, evidence dated after its own check - each is
    refused whole rather than written and explained later."""
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
        if not isinstance(rid, str) or not _CAP_ID.match(rid):
            problems.append("%s: id %r is not an instance id" % (where, rid))
            continue
        if not isinstance(resource, str) or not _CAP_RESOURCE.match(resource):
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
        for right in RIGHTS:
            state = rights[right]
            if not isinstance(state, dict) or set(state) != set(RIGHT_KEYS):
                problems.append("%s, %s: a state is exactly %s" % (where, right, ", ".join(RIGHT_KEYS)))
                continue
            can, status = state["can"], state["status"]
            if can not in CAN or status not in STATUS:
                problems.append("%s, %s: can is one of %s and status one of %s, not %r / %r. An observation "
                                "is never a grant or a denial" % (where, right, CAN, STATUS, can, status))
                continue
            if (can == "unknown") != (status == "unknown"):
                problems.append("%s, %s: unknown is unknown in both, or in neither (%s / %s)" % (where, right, can, status))
            checked = state["checked_at"]
            if not isinstance(checked, str) or not _CAP_STAMP.match(checked):
                problems.append("%s, %s: checked_at must be YYYY-MM-DDTHH:MM:SSZ, the time somebody last looked" % (where, right))
                continue
            if status == "unknown":
                if state["evidence_at"] != "" or not isinstance(state["source_event"], str):
                    problems.append("%s, %s: unknown has no evidence time" % (where, right))
                continue
            if not _plain(state["source_event"], 240):
                problems.append("%s, %s: %s needs its source_event: what was run or who said it" % (where, right, status))
            evidence = state["evidence_at"]
            if not isinstance(evidence, str) or not _CAP_STAMP.match(evidence):
                problems.append("%s, %s: evidence_at must be YYYY-MM-DDTHH:MM:SSZ" % (where, right))
            elif _when(evidence) > _when(checked):
                problems.append("%s, %s: the evidence is dated after its own check" % (where, right))
        if not problems and len(cap_value(obs).encode("utf-8")) > CAP_VALUE_BYTES:
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
    return "STALE" if (now - _when(state["checked_at"])).total_seconds() > STALE_AFTER_SECONDS else "fresh"


def expected_capabilities(data) -> dict:
    """{instance key: {cap field: value}}: exactly what `capability` writes, used by the write and by the verify."""
    out = {}
    for obs in data["observations"]:
        out.setdefault(AGENT_KEY % obs["id"], {})[cap_field(obs["resource"])] = cap_value(obs)
    return out


def put_capabilities(conn, data) -> dict:
    """Write ONLY the capability fields, then prove nothing else moved.

    ALL OR NOTHING ON IDENTITY: every id must already be a member of v1:agent:roster in Redis, or
    nothing at all is written - not even the observations whose ids are fine. This path does not
    create instances, and it does not consult the roster FILE, which in an old image is an old roster.
    ONE COMMAND PER FIELD: HSET of a single field. No DEL, no HDEL, no SADD, no SREM, no seed.
    READ BEFORE AND AFTER: every hash touched is read whole on both sides; any field other than
    the ones written here that differs is reported as a failure, and each written field is read back."""
    want = expected_capabilities(data)
    members = {m.decode("utf-8") if isinstance(m, bytes) else m for m in (conn.smembers(ROSTER_SET) or set())}
    unknown_ids = sorted(obs["id"] for obs in data["observations"] if obs["id"] not in members)
    if unknown_ids:
        return {"written": 0, "refused": unknown_ids, "changed_elsewhere": [], "not_read_back": []}
    before = {key: dict(conn.hgetall(key) or {}) for key in want}
    written = 0
    for key, fields in sorted(want.items()):
        for name, value in sorted(fields.items()):
            conn.hset(key, mapping={name: value})
            written += 1
    changed, unread = [], []
    for key, fields in sorted(want.items()):
        after = dict(conn.hgetall(key) or {})
        for name in sorted((set(before[key]) | set(after)) - set(fields)):
            if before[key].get(name) != after.get(name):
                changed.append("%s: field %s" % (key, name))
        for name, value in sorted(fields.items()):
            if after.get(name) != value:
                unread.append("%s: field %s" % (key, name))
    return {"written": written, "refused": [], "changed_elsewhere": changed, "not_read_back": unread}


def verify_capabilities(conn, data) -> list:
    """Drift between the capability file and its copy. Reported, never corrected. A cap: field in Redis
    that the file does not hold is drift too: the original is the file."""
    drift = []
    want = expected_capabilities(data)
    members = {m.decode("utf-8") if isinstance(m, bytes) else m for m in (conn.smembers(ROSTER_SET) or set())}
    for key in sorted(set(want) | {AGENT_KEY % member for member in members}):
        stored = {name: value for name, value in (conn.hgetall(key) or {}).items() if name.startswith(CAP_PREFIX)}
        fields = want.get(key, {})
        for name, value in sorted(fields.items()):
            if name not in stored:
                drift.append("%s: %s is in the file and not in Redis" % (key, name))
            elif stored[name] != value:
                drift.append("%s: %s differs" % (key, name))
        for name in sorted(set(stored) - set(fields)):
            drift.append("%s: %s is in Redis and not in the file" % (key, name))
    return drift


def capability_table(data, now=None) -> str:
    """What the file says, one line a right, with how it is known and whether the check is still fresh."""
    lines = []
    for obs in data["observations"]:
        lines.append("%s on %s, observed for: %s" % (obs["id"], obs["resource"], obs["runtime_identity"]))
        for right in RIGHTS:
            state = obs["rights"][right]
            lines.append("    %-6s %-7s %-8s %-7s checked %s  %s" % (
                right, state["can"], state["status"], staleness(state, now), state["checked_at"], state["source_event"]))
    return "\n".join(lines)


def capability_main(what) -> int:
    """`capability-check` and `capability`. Neither loads the roster FILE: the roster in this image may
    be an old one, and this path must not lean on it."""
    data = load_capabilities()
    problems = check_capabilities(data)
    for problem in problems:
        print("REFUSED: " + problem)
    if problems:
        return 1
    print("capability file OK: %d observation(s), version %s, updated %s"
          % (len(data["observations"]), data.get("version"), data.get("updated")))
    print(capability_table(data))
    if what == "capability-check":
        return 0
    conn = redis_dual.client(redis_dual.Settings(), precheck=False)
    if conn is None:
        print("UNKNOWN: no Redis connection, so nothing was written. This is not success.")
        return 2
    out = put_capabilities(conn, data)
    if out["refused"]:
        print("REFUSED, NOTHING WRITTEN: not in %s in Redis: %s. This path creates no instance."
              % (ROSTER_SET, ", ".join(out["refused"])))
        return 1
    print("written: %d capability field(s), by HSET of one field each; nothing else was sent" % out["written"])
    for line in out["changed_elsewhere"]:
        print("CHANGED WHILE WRITING (not by this path): " + line)
    for line in out["not_read_back"]:
        print("NOT READ BACK AS WRITTEN: " + line)
    drift = verify_capabilities(conn, data)
    for line in drift:
        print("DRIFT: " + line)
    bad = bool(out["changed_elsewhere"] or out["not_read_back"] or drift)
    print("VERDICT: %s" % ("every capability field reads back as the file has it, and no other field moved"
                           if not bad else "NOT CLEAN, see above"))
    return 1 if bad else 0


def table(data) -> str:
    """The markdown table, generated from the data so a document cannot drift from it."""
    lines = ["| Instance | Family | Runtime | In VPC | Redis read | Redis write | State |",
             "|---|---|---|---|---|---|---|"]
    for row in instances(data):
        lines.append("| `%s` | %s | %s | %s | %s | %s | %s |" % (
            row["id"], row["name"], row.get("runtime", ""),
            "yes" if row.get("in_vpc") else "no",
            "**yes**" if row.get("redis_read") else "no",
            "**yes**" if row.get("redis_write") else "no",
            (row.get("state", "") or "")[:48]))
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    what = argv[0] if argv else "check"
    if what in ("capability", "capability-check"):
        return capability_main(what)
    data = load()

    if what == "table":
        print(table(data))
        return 0

    problems = check(data)
    for problem in problems:
        print("REFUSED: " + problem)
    if problems:
        return 1
    rows = instances(data)
    print("file OK: %d instance(s) in %d famil(ies), %d alias(es), digest %s"
          % (len(rows), len(data["families"]), len(aliases(data)), digest()))
    print("with Redis access today: %s"
          % (", ".join(r["id"] for r in rows if r.get("redis_read")) or "nobody"))
    if what == "check":
        return 0

    settings = redis_dual.Settings()
    conn = redis_dual.client(settings, precheck=False)
    if conn is None:
        print("UNKNOWN: no Redis connection, so nothing was %s. This is not success."
              % ("seeded" if what == "seed" else "verified"))
        return 2

    if what == "seed":
        counts = seed(conn, data)
        print("seeded: %s" % json.dumps(counts, sort_keys=True))
        print("NOTE: a seed REPLACES each instance hash, so any cap: field is gone until "
              "`roster_seed.py capability` is run again; its original is agent_capabilities.json.")
        print("--- read back, because a seed that is not read back is an intention ---")
    drift = verify(conn, data)
    for line in drift:
        print("DRIFT: " + line)
    print("VERDICT: %s" % ("the copy matches the file" if not drift else "%d difference(s)" % len(drift)))
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
