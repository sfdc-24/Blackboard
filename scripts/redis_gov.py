"""Governed read/write access to Redis for every agent: one controller, one access list, one audit trail.

THE OWNER'S WORDS, 2026-10-08 ~01:40Z, directly to claude-code-cli:
    "Get Codex, Grok, Cursor in there so they can help you"
    "also Gemini needs access in Redis"
    "Everyone gets read write access, you control the Redis access rights and make actions auditable
     so there is governance"

WHY IT GOES THROUGH THE BOARD AND NOT STRAIGHT TO REDIS
    The instance has a private address inside the VPC. No agent outside it - not Grok's box, not
    Cursor's cloud, not the Codex desktop, not the Gemini endpoint - can open a socket to it at all.
    Every agent CAN append a board row. So the access path is: an agent posts a request row, this
    module decides whether that agent may do that thing, does it, and writes the decision down.

THE THREE THINGS HE ASKED FOR, AND WHERE EACH LIVES
    Everyone gets read/write    PRINCIPALS in scripts/redis_acl.json: every fleet agent may read
                                everything and write the governed namespaces.
    You control the rights      That file. One owner (claude-code-cli), reviewable in git, versioned
                                and digested - every audit entry names the ACL digest it was decided
                                under, so a past decision can always be traced to the rules in force.
    Auditable, for governance   EVERY operation, allowed or refused, is appended to the gov:audit
                                stream: who, through which board row, what, which key, the old value,
                                the new value, when, and why it was refused if it was.

HOW A WRITE AND ITS AUDIT ENTRY STAY TOGETHER - stated precisely, because the easy claim is false
    The easy claim is "they are one transaction, so they land together or not at all". Redis does
    not work that way: MULTI/EXEC never rolls back. If the connection drops before EXEC nothing
    lands, but once EXEC runs, a command that fails at runtime (WRONGTYPE, say) fails ALONE and the
    others still apply - so a write could land without its audit, or an audit could record a write
    that failed. What this module does about that:
      - WATCH the key, check its TYPE is one the op can write, then MULTI the write and the audit
        XADD together. A key changed in between aborts EXEC and nothing applies. The type check means
        the write cannot hit WRONGTYPE at runtime.
      - gov: is PROTECTED, so no agent can turn the audit stream into something an XADD would fail on.
      - EXEC's per-command results are inspected anyway. If either part ever failed, the result says
        WHICH, and reports `ok: False` - it never reports a success it cannot vouch for.

WHAT NOBODY MAY WRITE THROUGH THIS, WHATEVER THE ACL SAYS
    PROTECTED below, checked before the ACL and not configurable by it:
        v1:bus:     the board mirror. The comparison gate verifies it against the Sheet. If any agent
                    could write it, any agent could make the gate say AGREE about rows that differ.
        v1:agent:   the agent roster (PR 324's protocol keys)
        v1:conf:    the chair's own projection of a live call
        v1:synth: / probe:    the probe namespaces
        gov:        this module's own audit stream. An audit an agent can edit is not an audit.
    Reads of all of these are allowed - including gov:audit, because governance that cannot be
    inspected is not governance.

IDENTITY, STATED HONESTLY
    The actor is the row's Source_Tag. Appending a row needs the board secret, so the writer is a
    fleet member - but WHICH fleet member is a claim, not a proof. This does not pretend otherwise:
    the audit records the raw tag as well as the canonical principal, and the board row id, so a
    forged claim is traceable to the exact row that made it. Per-agent proof of identity is the
    redis-tool-bridge's job (OIDC, PR 340), for the agents that have a Google identity at all.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import re
from pathlib import Path

ACL_FILE = Path(__file__).resolve().parent / "redis_acl.json"
AUDIT_STREAM = "gov:audit"
AUDIT_MAXLEN = 200000

PROTECTED = ("v1:bus:", "v1:agent:", "v1:conf:", "v1:synth:", "probe:", "gov:")

OPS_READ = ("get", "hgetall", "xrange")
OPS_WRITE = ("set", "del", "hset", "xadd")
OPS = OPS_READ + OPS_WRITE

# A key reaches the server verbatim, so its grammar is closed: no space, no glob character (* ? [ ]),
# no control character, bounded length. A colon is allowed - it is how a namespace is spelled.
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_FIELD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

PREVIEW_CHARS = 200          # how much of a value an audit entry or a RESULT row may quote
MAX_HASH_FIELDS = 50
MAX_RANGE = 20


class Refused(Exception):
    """An operation the access list or the grammar does not allow. Its message is the reason."""


def stamp(moment=None) -> str:
    moment = moment or datetime.datetime.now(datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha8(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8] if value is not None else ""


def preview(value) -> str:
    """A bounded, single-line quote of a value - for an audit entry or a board row."""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "/")
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "..."


# ------------------------------------------------------------------------------------------- the ACL

def load_acl(path=None) -> dict:
    """The access list, with its digest. A missing or malformed file grants NOTHING.

    That direction is deliberate and matches the ratified-duplicates file: an absent rule makes
    access stricter, never looser. The digest is over the file's canonical JSON, so every audit entry
    can say exactly which rules decided it."""
    try:
        data = json.loads(Path(path or ACL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"principals": {}, "aliases": {}, "ttl_seconds": {}, "digest": "", "readable": False}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return {
        "principals": {str(k).lower(): v for k, v in (data.get("principals") or {}).items()},
        "aliases": {str(k).lower(): str(v).lower() for k, v in (data.get("aliases") or {}).items()},
        "ttl_seconds": {str(k): int(v) for k, v in (data.get("ttl_seconds") or {}).items()},
        "max_value_bytes": int(data.get("max_value_bytes") or 2048),
        "controller": str(data.get("controller") or ""),
        "version": data.get("version"),
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "readable": True,
    }


def principal(acl: dict, raw_tag: str) -> str:
    """The canonical principal for a raw Source_Tag, or "" when the tag is nobody the ACL knows."""
    tag = str(raw_tag or "").strip().lower()
    tag = acl["aliases"].get(tag, tag)
    return tag if tag in acl["principals"] else ""


def _matches(key: str, prefixes) -> bool:
    return any(p == "*" or key.startswith(p) for p in (prefixes or ()))


def check(acl: dict, who: str, op: str, key: str, field: str = "") -> None:
    """Raise Refused unless `who` may perform `op` on `key`. Returns nothing when allowed.

    ORDER MATTERS AND IS FIXED: grammar, then PROTECTED, then the ACL. The protected list is checked
    before the ACL so that no edit to the ACL - including a careless one - can open the board mirror
    or the audit stream to writes."""
    if not acl.get("readable"):
        raise Refused("the access list is unreadable, so nothing is permitted")
    if not who:
        raise Refused("the sender is not a principal in the access list")
    if op not in OPS:
        raise Refused("unknown op; the ops are " + ", ".join(OPS))
    if not _KEY.match(key or ""):
        raise Refused("the key is missing or not a key (letters, digits, . _ : - only, at most 200)")
    if field and not _FIELD.match(field):
        raise Refused("the field is not a field name")
    rules = acl["principals"][who]
    if op in OPS_WRITE:
        if any(key.startswith(p) for p in PROTECTED):
            raise Refused("that namespace is protected from writes through this path")
        if not _matches(key, rules.get("write")):
            raise Refused("%s may not write under that key" % who)
    elif not _matches(key, rules.get("read")):
        raise Refused("%s may not read that key" % who)


def ttl_for(acl: dict, key: str):
    """The server's TTL for a key: the longest matching prefix in the ACL's ttl_seconds, else None.

    Longest match, so `conf:2026-10-07:` can be shorter-lived than `conf:` without reordering."""
    best = None
    for prefix, seconds in acl["ttl_seconds"].items():
        if key.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, seconds)
    return best[1] if best else None


def decode_value(raw: str, enc: str, acl: dict) -> str:
    """The value to store. `enc=b64` carries anything the board grammar cannot (a pipe, a newline)."""
    if enc:
        if enc != "b64":
            raise Refused("enc must be b64 or absent")
        try:
            value = base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise Refused("val is not valid base64 of UTF-8")
    else:
        value = raw
    if len(value.encode("utf-8")) > acl["max_value_bytes"]:
        raise Refused("the value is over %d bytes" % acl["max_value_bytes"])
    return value


# --------------------------------------------------------------------------------------- the audit

def audit_fields(acl, who, raw_tag, via, op, key, field, ok, reason, old=None, new=None, now=None):
    """One audit entry. EVERY VALUE A STRING - redis-py refuses bools and None, and an audit write
    that raised would take its operation down with it."""
    return {
        "at": stamp(now), "actor": who or "(unknown)", "raw_tag": str(raw_tag or "")[:64],
        "via": str(via or "")[:120], "op": op or "", "key": key or "", "field": field or "",
        "ok": "1" if ok else "0", "reason": reason or "",
        "old_sha": sha8(old), "old_len": str(len(str(old))) if old is not None else "",
        "old": preview(old),
        "new_sha": sha8(new), "new_len": str(len(str(new))) if new is not None else "",
        "new": preview(new),
        "acl": acl.get("digest", ""),
    }


def _audit_alone(conn, fields) -> None:
    conn.xadd(AUDIT_STREAM, fields, maxlen=AUDIT_MAXLEN, approximate=True)


# ------------------------------------------------------------------------------------------ the ops

def execute(conn, acl: dict, raw_tag: str, via: str, op: str, key: str, field: str = "",
            raw_value: str = "", enc: str = "", count: int = 10, now=None) -> dict:
    """Do one governed operation. Always returns a result dict; never raises into the caller.

    Every outcome - refused, read, written, or failed - leaves exactly one gov:audit entry, and a
    write's entry is committed in the SAME transaction as the write."""
    who = principal(acl, raw_tag)
    op = (op or "").strip().lower()
    key = (key or "").strip()
    field = (field or "").strip()
    try:
        check(acl, who, op, key, field)
        value = decode_value(raw_value, enc, acl) if op in ("set", "hset", "xadd") else None
        if op == "hset" and not field:
            raise Refused("hset needs field=")
        if op in OPS_READ:
            return _read(conn, acl, who, raw_tag, via, op, key, count, now)
        return _write(conn, acl, who, raw_tag, via, op, key, field, value, now)
    except Refused as why:
        try:
            _audit_alone(conn, audit_fields(acl, who, raw_tag, via, op, key, field, False, str(why),
                                            now=now))
        except Exception as error:
            return {"ok": False, "refused": str(why), "audit": "FAILED:" + type(error).__name__}
        return {"ok": False, "refused": str(why), "audit": "ok"}
    except Exception as error:
        # The TYPE, never the message: a client's message can quote what it was sent.
        reason = "failed:" + type(error).__name__
        try:
            _audit_alone(conn, audit_fields(acl, who, raw_tag, via, op, key, field, False, reason,
                                            now=now))
        except Exception:
            pass
        return {"ok": False, "error": type(error).__name__}


def _read(conn, acl, who, raw_tag, via, op, key, count, now) -> dict:
    if op == "get":
        value = conn.get(key)
        out = {"ok": True, "op": op, "key": key, "found": value is not None, "value": preview(value)}
    elif op == "hgetall":
        found = conn.hgetall(key) or {}
        items = sorted(found.items())[:MAX_HASH_FIELDS]
        out = {"ok": True, "op": op, "key": key, "fields": len(found),
               "value": {k: preview(v) for k, v in items}, "truncated": len(found) > MAX_HASH_FIELDS}
    else:                                                                       # xrange
        n = max(1, min(int(count or 10), MAX_RANGE))
        entries = conn.xrevrange(key, count=n) or []
        out = {"ok": True, "op": op, "key": key, "entries": len(entries),
               "value": [{"id": str(i), **{k: preview(v) for k, v in f.items()}}
                         for i, f in reversed(entries)]}
    # A read is audited too - "who looked at what" is half of governance - but cheaply: no value.
    _audit_alone(conn, audit_fields(acl, who, raw_tag, via, op, key, "", True, "", now=now))
    out["audit"] = "ok"
    return out


# The key types each write may touch. `del` may remove any type, so it has no entry.
WRITABLE_TYPES = {"set": ("none", "string"), "hset": ("none", "hash"), "xadd": ("none", "stream")}


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _write(conn, acl, who, raw_tag, via, op, key, field, value, now) -> dict:
    """WATCH, check the type, read the old value, then MULTI the write AND its audit entry together.

    Every Refused raised in here is audited by the caller. Every EXEC result is inspected: a success
    is reported only when both the write and its audit entry succeeded."""
    ttl = ttl_for(acl, key)
    pipe = conn.pipeline(transaction=True)
    try:
        pipe.watch(key)                              # immediate mode until multi()
        kind = _text(pipe.type(key))
        allowed = WRITABLE_TYPES.get(op)
        if allowed and kind not in allowed:
            raise Refused("the key holds a %s; %s writes a %s" % (kind, op, allowed[1]))
        # The OLD value, read under the WATCH, so the audit says exactly what this replaced.
        if op in ("set", "del"):
            old = pipe.get(key) if kind in ("none", "string") else "(%s)" % kind
        elif op == "hset":
            old = pipe.hget(key, field)
        else:
            old = None                               # xadd appends; nothing is replaced
        entry = audit_fields(acl, who, raw_tag, via, op, key, field, True, "", old=old,
                             new=(None if op == "del" else value), now=now)
        pipe.multi()
        if op == "set":
            if ttl:
                pipe.set(key, value, ex=ttl)
            else:
                pipe.set(key, value)
        elif op == "del":
            pipe.delete(key)
        elif op == "hset":
            pipe.hset(key, field, value)
            if ttl:
                pipe.expire(key, ttl)
        else:                                                                   # xadd
            pipe.xadd(key, {"text": value, "by": who, "at": stamp(now)}, maxlen=10000,
                      approximate=True)
            if ttl:
                pipe.expire(key, ttl)
        pipe.xadd(AUDIT_STREAM, entry, maxlen=AUDIT_MAXLEN, approximate=True)
        try:
            results = pipe.execute(raise_on_error=False)
        except Exception as error:
            if type(error).__name__ == "WatchError":
                # The key changed between the WATCH and the EXEC: NOTHING was applied, write or audit.
                raise Refused("the key changed while this was being applied; nothing was written")
            raise
    finally:
        pipe.reset()

    failed = [n for n, r in enumerate(results) if isinstance(r, Exception)]
    if failed:
        # Should be unreachable after the type check, and gov: being protected. If it ever happens,
        # say exactly which part failed instead of claiming either half.
        audit_failed = (len(results) - 1) in failed
        return {"ok": False, "op": op, "key": key, "partial": True,
                "error": "write failed" if not audit_failed else
                         ("AUDIT FAILED - the write may have landed unaudited"
                          if len(failed) == 1 else "write and audit both failed")}
    return {"ok": True, "op": op, "key": key, "field": field, "ttl": ttl,
            "old_sha": entry["old_sha"], "new_sha": entry["new_sha"], "audit": "ok"}


def summarise(result: dict) -> str:
    """One line for a board RESULT row: pipe-free, newline-free, bounded."""
    if not result.get("ok"):
        if "refused" in result:
            return "REFUSED: %s (audited: %s)" % (result["refused"], result.get("audit", "?"))
        return "FAILED: %s" % result.get("error", "unknown")
    if result["op"] in OPS_READ:
        body = json.dumps(result.get("value"), sort_keys=True, ensure_ascii=False)
        body = body.replace("|", "/")
        if len(body) > 1500:
            body = body[:1500] + "..."
        return "OK %s %s: %s" % (result["op"], result["key"], body)
    return "OK %s %s%s ttl=%s old=%s new=%s (audited)" % (
        result["op"], result["key"], (" field=" + result["field"]) if result.get("field") else "",
        result.get("ttl"), result.get("old_sha") or "-", result.get("new_sha") or "-")
