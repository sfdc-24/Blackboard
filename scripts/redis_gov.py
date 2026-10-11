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
import os
import re
import unicodedata
from pathlib import Path

ACL_FILE = Path(__file__).resolve().parent / "redis_acl.json"
AUDIT_STREAM = "gov:audit"
AUDIT_MAXLEN = 200000

PROTECTED = ("v1:bus:", "v1:agent:", "v1:conf:", "v1:synth:", "probe:", "gov:")

OPS_READ = ("get", "hgetall", "xrange")
# hput: REPLACE a whole hash from one b64 JSON object {field: string}, in one transaction with one
# audit entry. Mr. Salam, 2026-10-08 18:05Z, directly: "keep the repo copy, Redis as the live copy" -
# for the milestone chart Grok assembles: ten hashes of up to ~11 fields would otherwise be ~110
# one-field requests and ~110 result rows on the board (a row is money he pays). Replace, not merge,
# so a field dropped from a new version does not survive from the old one.
OPS_WRITE = ("set", "del", "hset", "hput", "xadd")
MAX_PUT_FIELDS = 50
OPS = OPS_READ + OPS_WRITE

# Generation audit. Off unless REDIS_GOV_VERIFY is explicitly on, so a deploy of this module does
# not answer haudit and does not change get, hgetall, xrange, or any write. The op compares
# SHA-256 and byte length of a canonical UTF-8 hash payload. It does not return the payload.
VERIFY_OP = "haudit"
VERIFY_FLAG = "REDIS_GOV_VERIFY"
# bus_request.HEARTBEAT_KEY. Repeated here so this module does not import the worker.
HEARTBEAT_READ_KEY = "gov:worker:heartbeat"
MAX_AUDIT_KEYS = 128          # manifest cap. The verifier does not list or scan the keyspace.
MAX_AUDIT_MANIFEST_BYTES = 65536
_VERIFY_ON = {"1", "true", "on", "yes"}

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
        "value_bytes_by_prefix": {str(k): int(v) for k, v in
                                  (data.get("value_bytes_by_prefix") or {}).items()},
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


def verifier_enabled(env=None) -> bool:
    """True only when REDIS_GOV_VERIFY is explicitly on. Unset, empty, and anything else are off."""
    source = os.environ if env is None else env
    return str(source.get(VERIFY_FLAG) or "").strip().lower() in _VERIFY_ON


def active_ops(env=None):
    """The ops check() will accept. haudit is absent until the flag is on, so the refusal for an
    unknown op stays the list this module shipped with."""
    if verifier_enabled(env):
        return OPS + (VERIFY_OP,)
    return OPS


def check(acl: dict, who: str, op: str, key: str, field: str = "") -> None:
    """Raise Refused unless `who` may perform `op` on `key`. Returns nothing when allowed.

    ORDER MATTERS AND IS FIXED: grammar, then PROTECTED, then the ACL. The protected list is checked
    before the ACL so that no edit to the ACL - including a careless one - can open the board mirror
    or the audit stream to writes."""
    if not acl.get("readable"):
        raise Refused("the access list is unreadable, so nothing is permitted")
    if not who:
        raise Refused("the sender is not a principal in the access list")
    if op not in active_ops():
        raise Refused("unknown op; the ops are " + ", ".join(active_ops()))
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


def value_cap(acl: dict, key: str) -> int:
    """The byte cap for a value under `key`: the longest matching prefix in value_bytes_by_prefix,
    else max_value_bytes. A per-prefix cap can only be set in the reviewed access list."""
    best = None
    for prefix, cap in (acl.get("value_bytes_by_prefix") or {}).items():
        if key.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, cap)
    return best[1] if best else acl["max_value_bytes"]


def decode_mapping(raw: str, enc: str, acl: dict, key: str) -> dict:
    """hput's value: b64 of a JSON object of field -> string, every field name in the field grammar,
    at most MAX_PUT_FIELDS, the whole JSON within the key's cap. Anything else is refused whole."""
    if enc != "b64":
        raise Refused("hput needs enc=b64")
    text = decode_value(raw, enc, acl, key)
    try:
        mapping = json.loads(text)
    except ValueError:
        raise Refused("hput val is not JSON")
    if not isinstance(mapping, dict) or not mapping:
        raise Refused("hput val must be a non-empty JSON object")
    if len(mapping) > MAX_PUT_FIELDS:
        raise Refused("hput takes at most %d fields" % MAX_PUT_FIELDS)
    for name, value in mapping.items():
        if not _FIELD.match(name) or not isinstance(value, str):
            raise Refused("every hput field must be a field name with a string value")
    return mapping


def decode_value(raw: str, enc: str, acl: dict, key: str = "") -> str:
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
    cap = value_cap(acl, key)
    if len(value.encode("utf-8")) > cap:
        raise Refused("the value is over %d bytes" % cap)
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
            raw_value: str = "", enc: str = "", count: int = 10, now=None, generation: str = "",
            correlation: str = "", head: str = "") -> dict:
    """Do one governed operation. Always returns a result dict; never raises into the caller.

    Every outcome - refused, read, written, or failed - leaves exactly one gov:audit entry, and a
    write's entry is committed in the SAME transaction as the write.

    generation, correlation and head are read only by haudit. Other ops ignore them."""
    who = principal(acl, raw_tag)
    op = (op or "").strip().lower()
    key = (key or "").strip()
    field = (field or "").strip()
    echoed_generation = _id_echo(generation, "generation") if op == VERIFY_OP else ""
    echoed_correlation = _id_echo(correlation, "correlation") if op == VERIFY_OP else ""
    try:
        if op == VERIFY_OP and not verifier_enabled():
            raise Refused("the verifier is off")
        check(acl, who, op, key, field)
        value = decode_value(raw_value, enc, acl, key) if op in ("set", "hset", "xadd") else None
        if op == "hset" and not field:
            raise Refused("hset needs field=")
        if op == "hput":
            if field:
                raise Refused("hput replaces the whole hash; it takes no field=")
            value = decode_mapping(raw_value, enc, acl, key)
        if op == VERIFY_OP:
            return _audit_generation(conn, acl, who, raw_tag, via, key, field, raw_value, enc,
                                     echoed_generation, echoed_correlation, (head or "").strip(), now)
        if op in OPS_READ:
            return _read(conn, acl, who, raw_tag, via, op, key, count, now)
        return _write(conn, acl, who, raw_tag, via, op, key, field, value, now)
    except Refused as why:
        try:
            _audit_alone(conn, audit_fields(acl, who, raw_tag, via, op, key, field, False, str(why),
                                            now=now))
        except Exception as error:
            result = {"ok": False, "refused": str(why), "audit": "FAILED:" + type(error).__name__}
        else:
            result = {"ok": False, "refused": str(why), "audit": "ok"}
        return _attach_verify_ids(result, op, echoed_generation, echoed_correlation)
    except Exception as error:
        # The TYPE, never the message: a client's message can quote what it was sent.
        reason = "failed:" + type(error).__name__
        try:
            _audit_alone(conn, audit_fields(acl, who, raw_tag, via, op, key, field, False, reason,
                                            now=now))
        except Exception:
            pass
        result = {"ok": False, "error": type(error).__name__}
        if op == VERIFY_OP:
            result["partial"] = True
            result["timeout"] = _is_timeout(error)
        return _attach_verify_ids(result, op, echoed_generation, echoed_correlation)


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
WRITABLE_TYPES = {"set": ("none", "string"), "hset": ("none", "hash"), "hput": ("none", "hash"),
                  "xadd": ("none", "stream")}


def canonical(mapping) -> str:
    """A hash as one string, for the audit's old/new and a reader's checksum: sorted, compact JSON."""
    return json.dumps(mapping or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _strict_text(value) -> str:
    """UTF-8 text, or UnicodeDecodeError. Invalid bytes are never replaced.

    Replacement would make two different byte strings one payload, so a manifest of U+FFFD would
    match data that was never valid UTF-8. Callers that digest a hash use this. `_text` stays for
    paths that only need a type name or an audit quote."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _nfc(value) -> str:
    return unicodedata.normalize("NFC", _strict_text(value))


def canonical_utf8(mapping) -> bytes:
    """The hash as canonical UTF-8 bytes.

    Field names and values are decoded as strict UTF-8 and NFC-normalised before they are sorted,
    so a composed character and its decomposed form are one payload, and insertion order is not
    part of the payload. Invalid UTF-8 raises UnicodeDecodeError and is not a payload. The JSON is
    compact and not ASCII-escaped. Two fields whose names collapse to the same NFC string are a
    collision: this raises Refused rather than dropping one of them."""
    clean = {}
    for key, value in (mapping or {}).items():
        name = _nfc(key)
        if name in clean:
            raise Refused("two fields normalise to one name; nothing was treated as a match")
        clean[name] = _nfc(value)
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_digest(mapping) -> tuple:
    """(sha256 hex, byte length) of canonical_utf8(mapping)."""
    raw = canonical_utf8(mapping)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _id_echo(value, kind) -> str:
    """An id safe to put back on a board row, or "" when the raw text is not one.

    The raw text is never returned. A generation id is 64 hex characters. A correlation id is a
    short token from the same alphabet as a Redis key."""
    text = str(value or "").strip()
    if kind == "generation":
        return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}", text) else ""


def _is_timeout(error) -> bool:
    return isinstance(error, TimeoutError) or type(error).__name__ == "TimeoutError"


def _attach_verify_ids(result, op, generation, correlation):
    if op != VERIFY_OP:
        return result
    result["op"] = VERIFY_OP
    result["generation"] = generation
    result["correlation"] = correlation
    return result


def verifier_scope(who: str, key: str) -> bool:
    """The keys haudit may touch, which is narrower than a fleet agent's read '*'.

    Own inbox, the governed write prefixes, and the worker heartbeat. get and hgetall are unchanged
    and still follow the access list. A digest of a key the caller could not otherwise name is
    still a digest of that key, so this op does not become a second way to read the board mirror,
    another agent's inbox, or the audit stream."""
    own = "fleet:inbox:" + (who or "")
    if key == own or key.startswith(own + ":"):
        return True
    if key.startswith("conf:") or key.startswith("proj:"):
        return True
    return key == HEARTBEAT_READ_KEY


def _guard_verify_key(acl, who, key) -> None:
    if not verifier_scope(who, key):
        raise Refused("that key is outside this agent's verifier scope")
    check(acl, who, "hgetall", key)


def _parse_manifest(raw: str, enc: str) -> dict:
    """b64 JSON of {keys: {key: {sha256, len}}}. Any other field is refused, unread.

    The manifest is digests and lengths only. A stored string does not belong in it, and a field
    that is not sha256 or len is refused without being copied into the refusal."""
    if enc != "b64":
        raise Refused("haudit needs enc=b64")
    try:
        blob = base64.b64decode(str(raw or "").encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        raise Refused("haudit val is not valid base64")
    if len(blob) > MAX_AUDIT_MANIFEST_BYTES:
        raise Refused("haudit manifest is over the byte cap")
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused("haudit manifest is not UTF-8 JSON")
    if not isinstance(data, dict) or set(data) != {"keys"}:
        raise Refused("haudit manifest must be an object whose only member is keys")
    keys = data["keys"]
    if not isinstance(keys, dict) or not keys:
        raise Refused("haudit manifest needs a non-empty keys object")
    if len(keys) > MAX_AUDIT_KEYS:
        raise Refused("haudit manifest has too many keys")
    expected = {}
    for name, spec in keys.items():
        if not isinstance(name, str) or not _KEY.match(name):
            raise Refused("haudit key is not a key")
        if not isinstance(spec, dict) or set(spec) != {"sha256", "len"}:
            raise Refused("haudit key entry must be sha256 and len only")
        digest, length = spec["sha256"], spec["len"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Refused("haudit sha256 must be 64 hex characters")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0 or length > 1_000_000:
            raise Refused("haudit len must be a byte length")
        expected[name] = {"sha256": digest, "len": length}
    return expected


def _bound_head(prefix: str, generation: str) -> str:
    """The head key that belongs to `<namespace>g:<16 hex>:`.

    That key is `<namespace>head`. `proj:pr-board:v1:g:<gen16>:` binds to
    `proj:pr-board:v1:head`. The caller does not choose it: a head in another namespace can store
    the same generation id and must not stand in for this one."""
    marker = "g:" + (generation or "")[:16] + ":"
    if len(generation or "") != 64 or not str(prefix or "").endswith(marker):
        raise Refused("the prefix is not this generation")
    namespace = prefix[:-len(marker)]
    head = namespace + "head"
    if not namespace.endswith(":") or not _KEY.match(head):
        raise Refused("the prefix is not this generation")
    return head


def _exec_error(results) -> str:
    """The type name of the first failed command, or "" when every result is a value.

    Pipeline.execute(raise_on_error=False) returns an exception in the list and does not raise.
    A confirmation that does not read that list can report a pass for a command that failed."""
    if not isinstance(results, (list, tuple)) or not results:
        return "ExecError"
    for item in results:
        if isinstance(item, BaseException):
            return type(item).__name__
    return ""


def _hash_generation(conn, key):
    """The head hash's generation field, and the raw field for the movement compare.

    Only that one field is read. A string or stream is not read at all. The raw field is
    discarded unless it is itself a 64-hex generation id, so a secret parked in the field
    cannot ride out on the result."""
    kind = _text(conn.type(key))
    if kind != "hash":
        return "", ""
    raw = _text(conn.hget(key, "generation") or "")
    return (raw if re.fullmatch(r"[0-9a-f]{64}", raw) else ""), raw


def manifest_body(specs) -> bytes:
    """Canonical UTF-8 of the manifest object. The head key is not a member.

    ``{"keys": {name: {"len": n, "sha256": hex}}}`` with sorted keys and compact separators.
    ``len`` sorts before ``sha256``. This is the manifest digest's preimage, not the report."""
    keys = {name: {"len": int(specs[name]["len"]), "sha256": specs[name]["sha256"]}
            for name in specs}
    return json.dumps({"keys": keys}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def manifest_digest(specs) -> str:
    """SHA-256 hex of manifest_body(specs)."""
    return hashlib.sha256(manifest_body(specs)).hexdigest()


def report_body(specs) -> bytes:
    """One line per generation document, excluding the head: ``key<TAB>sha256<TAB>len``, sorted.

    No status column. The writer and the verifier hash these same bytes."""
    lines = ["%s\t%s\t%d" % (name, specs[name]["sha256"], int(specs[name]["len"]))
             for name in sorted(specs)]
    return "\n".join(lines).encode("utf-8")


def report_digest(specs) -> tuple:
    """(sha256 hex, byte length) of report_body(specs)."""
    raw = report_body(specs)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _audit_report(entries) -> tuple:
    specs = {entry["key"]: {"sha256": entry.get("sha256") or "", "len": int(entry.get("len") or 0)}
             for entry in entries}
    return report_digest(specs)


def _publish_audit(entries, generation, correlation, coverage, partial, moved, timeout,
                   head_generation, audit="ok", refused="", error="", manifest_digest_hex=""):
    """The receipt haudit returns. There is no field for a stored string.

    Partial or incomplete is never ok. ``extra`` is not a keyspace claim: unlisted keys are
    ignored, not scanned for."""
    out = {
        "ok": False,
        "op": VERIFY_OP,
        "generation": generation,
        "correlation": correlation,
        "partial": bool(partial),
        "moved": bool(moved),
        "timeout": bool(timeout),
        "head_generation": head_generation if re.fullmatch(r"[0-9a-f]{64}", head_generation or "") else "",
        "coverage": {
            "expected": int(coverage.get("expected") or 0),
            "seen": int(coverage.get("seen") or 0),
            "missing": int(coverage.get("missing") or 0),
            "extra": int(coverage.get("extra") or 0),
            "changed": int(coverage.get("changed") or 0),
            "matched": int(coverage.get("matched") or 0),
            "covered": bool(coverage.get("covered")),
        },
        "keys": [{
            "key": entry["key"],
            "sha256": entry["sha256"] if re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256") or "") else "",
            "len": int(entry.get("len") or 0),
            "status": entry["status"] if entry.get("status") in ("match", "missing", "extra", "changed")
            else "changed",
        } for entry in entries],
        "audit": audit or "",
    }
    digest, length = _audit_report(out["keys"])
    out["digest"] = digest
    out["length"] = length
    out["listed"] = int(coverage.get("expected") or 0)
    out["manifest_digest"] = (manifest_digest_hex
                              if re.fullmatch(r"[0-9a-f]{64}", manifest_digest_hex or "") else "")
    clean = (out["coverage"]["missing"] == 0 and out["coverage"]["extra"] == 0
             and out["coverage"]["changed"] == 0 and out["coverage"]["matched"] == out["coverage"]["expected"]
             and out["coverage"]["expected"] > 0 and not out["partial"] and not out["moved"]
             and not out["timeout"] and not refused and not error and bool(coverage.get("head_match")))
    out["coverage"]["covered"] = clean
    out["ok"] = clean and audit == "ok"
    if refused:
        out["ok"] = False
        out["refused"] = str(refused)[:200]
    if error:
        out["ok"] = False
        out["error"] = str(error)[:80]
    return out


def _read_listed(pipe, names, records):
    """Digest each manifest key. Returns (partial, timed_out, hard_error).

    Stops at the first failure so a partial read is not reported as a complete match.
    No key outside ``names`` is read."""
    partial = timed_out = False
    hard_error = ""
    for name in names:
        try:
            kind = _text(pipe.type(name))
            if kind == "none":
                records[name] = ("missing", "", 0)
                continue
            if kind != "hash":
                records[name] = ("changed", "", 0)
                continue
            raw_hash = pipe.hgetall(name) or {}
            try:
                digest, length = payload_digest(raw_hash)
            except UnicodeDecodeError:
                records[name] = ("changed", "", 0)
                continue
            except Refused:
                records[name] = ("changed", "", 0)
                partial = True
                continue
            records[name] = ("hash", digest, length)
        except Exception as error:
            partial = True
            if _is_timeout(error):
                timed_out = True
            else:
                hard_error = type(error).__name__
            break
    return partial, timed_out, hard_error


def _audit_generation(conn, acl, who, raw_tag, via, prefix, field, raw_value, enc,
                      generation, correlation, head, now) -> dict:
    """Receipt for one generation against the caller's manifest. Shared contract v1.

    The head is bound from the generation namespace and its generation id is read before any
    document. Only manifest-listed keys are read, each under WATCH, at most MAX_AUDIT_KEYS of
    them. There is no KEYS, no SCAN, and no Lua walk of the keyspace. An unlisted key is ignored.
    A listed key or the head that changes before EXEC is moved. A queued command error is not a
    pass. A read that stops early is partial, and partial is never ok. Invalid UTF-8 is not a
    match."""
    if field:
        raise Refused("haudit takes no field=")
    if not generation:
        raise Refused("haudit needs a 64-hex generation id")
    if not correlation:
        raise Refused("haudit needs a correlation id")
    if not head:
        raise Refused("haudit needs a head key")
    bound_head = _bound_head(prefix, generation)
    _guard_verify_key(acl, who, prefix)
    if head != bound_head:
        raise Refused("the head key is not this generation's head")
    _guard_verify_key(acl, who, head)
    expected = _parse_manifest(raw_value, enc)
    for name in expected:
        if not name.startswith(prefix):
            raise Refused("a manifest key is outside the generation prefix")
        _guard_verify_key(acl, who, name)

    claimed = manifest_digest(expected)
    names = sorted(expected)
    pipe = conn.pipeline(transaction=True)
    records = {}
    partial = False
    moved = False
    timed_out = False
    hard_error = ""
    head_generation = ""
    head_match = False
    started = False
    try:
        pipe.watch(head)
        try:
            head_generation, head_raw_before = _hash_generation(pipe, head)
        except Exception as error:
            if _is_timeout(error):
                timed_out = True
            else:
                hard_error = type(error).__name__
            partial = True
            head_raw_before = None
        # Head -> generation id before any document read. A mismatch does not open the manifest keys.
        if not timed_out and not hard_error and head_raw_before == generation:
            started = True
            for name in names:
                pipe.watch(name)
            read_partial, timed_out, hard_error = _read_listed(pipe, names, records)
            partial = partial or read_partial
            head_raw_after = head_raw_before
            if not timed_out and not hard_error:
                try:
                    _published_after, head_raw_after = _hash_generation(pipe, head)
                except Exception as error:
                    partial = True
                    if _is_timeout(error):
                        timed_out = True
                    else:
                        hard_error = type(error).__name__
            if head_raw_before is not None and head_raw_after != head_raw_before:
                moved = True
        head_match = bool(head_raw_before) and head_raw_before == generation and not moved
        if started and not timed_out and not hard_error:
            try:
                # TYPE each watched key inside the transaction. An error in any result is not a
                # pass. WATCH aborts EXEC when a listed key or the head changes. Nothing here
                # lists the keyspace.
                pipe.multi()
                pipe.type(head)
                for name in names:
                    pipe.type(name)
                confirmed = pipe.execute(raise_on_error=False)
                failed = _exec_error(confirmed)
                if failed or len(confirmed) != 1 + len(names):
                    hard_error = failed or "ExecError"
                    partial = True
                    head_match = False
            except Exception as error:
                if type(error).__name__ == "WatchError":
                    moved = True
                    head_match = False
                elif _is_timeout(error):
                    timed_out = True
                    partial = True
                    head_match = False
                else:
                    hard_error = type(error).__name__
                    partial = True
                    head_match = False
    finally:
        pipe.reset()

    entries = []
    missing = changed = matched = 0
    if started:
        for name in names:
            if name not in records:
                partial = True
                continue
            kind, digest, length = records[name]
            want = expected[name]
            if kind == "missing":
                status = "missing"
                missing += 1
                digest, length = "", 0
            elif kind != "hash" or digest != want["sha256"] or length != want["len"]:
                status = "changed"
                changed += 1
            else:
                status = "match"
                matched += 1
            entries.append({"key": name, "sha256": digest, "len": length, "status": status})
    coverage = {
        "expected": len(expected), "seen": len(records), "missing": missing, "extra": 0,
        "changed": changed, "matched": matched, "head_match": head_match,
    }
    return _finish_audit(conn, acl, who, raw_tag, via, prefix, entries, generation, correlation,
                         coverage, partial=partial or bool(hard_error), moved=moved,
                         timeout=timed_out, head_generation=head_generation, now=now,
                         error=("TimeoutError" if timed_out else hard_error),
                         manifest_digest_hex=claimed)


def _finish_audit(conn, acl, who, raw_tag, via, key, entries, generation, correlation, coverage,
                  partial, moved, timeout, head_generation, now, error="", manifest_digest_hex=""):
    result = _publish_audit(entries, generation, correlation, coverage, partial, moved, timeout,
                            head_generation, audit="ok", error=error,
                            manifest_digest_hex=manifest_digest_hex)
    reason = "missing=%d extra=%d changed=%d partial=%d moved=%d timeout=%d" % (
        result["coverage"]["missing"], result["coverage"]["extra"], result["coverage"]["changed"],
        int(result["partial"]), int(result["moved"]), int(result["timeout"]))
    try:
        _audit_alone(conn, audit_fields(acl, who, raw_tag, via, VERIFY_OP, key, "", result["ok"],
                                        reason, now=now))
    except Exception as failure:
        result["audit"] = "FAILED:" + type(failure).__name__
        result["ok"] = False
        result["coverage"]["covered"] = False
        result["partial"] = True
    return result


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
        elif op == "hput":
            found = pipe.hgetall(key) or {}
            old = canonical({_text(k): _text(v) for k, v in found.items()}) if found else None
        else:
            old = None                               # xadd appends; nothing is replaced
        entry = audit_fields(acl, who, raw_tag, via, op, key, field, True, "", old=old,
                             new=(None if op == "del" else
                                  canonical(value) if op == "hput" else value), now=now)
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
        elif op == "hput":
            pipe.delete(key)                         # replace, not merge: no field outlives its version
            pipe.hset(key, mapping=value)
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


def _summarise_verify(result: dict) -> str:
    """One line of ids, counts and digests. Stored strings are not in the result, and this does not
    go looking for them."""
    if result.get("refused"):
        return "REFUSED haudit gen=%s cid=%s: %s (audited: %s)" % (
            result.get("generation") or "-", result.get("correlation") or "-",
            result["refused"], result.get("audit", "?"))
    cov = result.get("coverage") or {}
    base = ("HAUDIT ok=%d gen=%s cid=%s listed=%d expected=%d seen=%d missing=%d extra=%d changed=%d "
            "matched=%d covered=%d partial=%d moved=%d timeout=%d manifest=%s digest=%s length=%d" % (
                int(bool(result.get("ok"))), result.get("generation") or "-",
                result.get("correlation") or "-", int(result.get("listed") or cov.get("expected") or 0),
                int(cov.get("expected") or 0),
                int(cov.get("seen") or 0), int(cov.get("missing") or 0), int(cov.get("extra") or 0),
                int(cov.get("changed") or 0), int(cov.get("matched") or 0),
                int(bool(cov.get("covered"))), int(bool(result.get("partial"))),
                int(bool(result.get("moved"))), int(bool(result.get("timeout"))),
                result.get("manifest_digest") or "-",
                result.get("digest") or "-", int(result.get("length") or 0)))
    if result.get("error"):
        base += " error=%s" % result["error"]
    detail = ",".join(
        "%s=%s:%d:%s" % (entry.get("key") or "", entry.get("sha256") or "",
                         int(entry.get("len") or 0), entry.get("status") or "")
        for entry in (result.get("keys") or []))
    if detail and len(base) + 1 + len(detail) <= 800:
        return base + " " + detail
    mismatches = [entry for entry in (result.get("keys") or []) if entry.get("status") != "match"]
    brief = ",".join(
        "%s=%s:%d:%s" % (entry.get("key") or "", entry.get("sha256") or "",
                         int(entry.get("len") or 0), entry.get("status") or "")
        for entry in mismatches)
    if brief and len(base) + 1 + len(brief) <= 800:
        return base + " " + brief
    if mismatches:
        return base + " detail=bounded"
    return base


def summarise(result: dict) -> str:
    """One line for a board RESULT row: pipe-free, newline-free, bounded."""
    if result.get("op") == VERIFY_OP:
        line = _summarise_verify(result).replace("|", "/").replace("\n", " ")
        return line
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
