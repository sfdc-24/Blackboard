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
"""
from __future__ import annotations

import hashlib
import json
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


def seed(conn, data) -> dict:
    counts = {"instances": 0, "families": 0, "aliases": 0}
    for row in instances(data):
        mapping = {f: ("true" if row.get(f) is True else "false" if row.get(f) is False
                       else str(row.get(f, "")))
                   for f in FIELDS}
        conn.hset(AGENT_KEY % row["id"], mapping=mapping)
        conn.sadd(ROSTER_SET, row["id"])
        counts["instances"] += 1
    for family in data["families"]:
        conn.hset(FAMILY_KEY % family["family"], mapping={
            "name": family["name"], "role": family["lead_role"],
            "responsibility": family["responsibility"], "appointed": family.get("appointed", ""),
            "instances": ",".join(i["id"] for i in family["instances"])})
        counts["families"] += 1
    table = aliases(data)
    if table:
        conn.hset(CANON, mapping=table)
        counts["aliases"] = len(table)
    conn.hset(SEEDED, mapping={"at": data.get("updated", ""), "file_digest": digest(),
                               "version": data.get("version", ""), "by": "roster_seed.py"})
    return counts


def verify(conn, data) -> list:
    """Drift between the file and the copy. Reported, never corrected: a verify that fixes what it
    finds is a seed wearing a read-only name."""
    drift = []
    stored_ids = set(conn.smembers(ROSTER_SET) or set())
    file_ids = {row["id"] for row in instances(data)}
    for extra in sorted(stored_ids - file_ids):
        drift.append("in Redis and not in the file: %s" % extra)
    for missing in sorted(file_ids - stored_ids):
        drift.append("in the file and not in Redis: %s" % missing)
    for row in instances(data):
        stored = conn.hgetall(AGENT_KEY % row["id"]) or {}
        if not stored:
            continue
        for f in FIELDS:
            want = ("true" if row.get(f) is True else "false" if row.get(f) is False
                    else str(row.get(f, "")))
            if stored.get(f, "") != want:
                drift.append("%s: field %s differs" % (row["id"], f))
    seeded = conn.hgetall(SEEDED) or {}
    if seeded.get("file_digest") and seeded["file_digest"] != digest():
        drift.append("the file has changed since it was seeded (digest %s, now %s)"
                     % (seeded["file_digest"], digest()))
    return drift


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
        print("--- read back, because a seed that is not read back is an intention ---")
    drift = verify(conn, data)
    for line in drift:
        print("DRIFT: " + line)
    print("VERDICT: %s" % ("the copy matches the file" if not drift else "%d difference(s)" % len(drift)))
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
