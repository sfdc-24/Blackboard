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
MAX_AUDIT_MANIFEST_BYTES = 65536
# Truly paged. Page size 8. A read is at most one batch of 128. The manifest cap is the measured
# UTF-8 size of the submitted manifest, keys form or pages form, and it does not rise.
# The honest maximum for the fixture's document shape replaces the old 512-document ceiling.
# That shape is one item shard and one action shard per conference pull request, plus the eight
# rollup documents (three repos, three turn rows, meta, closed). 233 such pull requests are 474
# documents: pages-form manifest 65400 bytes, keys-form manifest 64089 bytes. 234 pull requests
# are 476 documents and 65671 pages-form bytes, over the cap. 252 pull requests are the old 512
# documents and 69238 keys-form bytes, which also does not fit. The 326-document inventory is
# 44928 pages-form bytes and stays inside the cap. 2048 does not rise.
PAGE_SIZE = 8
MAX_AUDIT_BATCH = 128
MAX_GENERATION_DOCS = 474
MAX_AUDIT_KEYS = MAX_AUDIT_BATCH
MAX_AUDIT_PAGES = (MAX_GENERATION_DOCS + PAGE_SIZE - 1) // PAGE_SIZE
# Per-document canonical cap. Checked with HLEN/HSTRLEN before HGETALL. Shared with the writer.
# The manifest-root preimage is not a stored hash value. A 41-page preimage is 2787 bytes and
# the head stores only its SHA-256, so this cap is not applied to that preimage.
MAX_AUDIT_DOCUMENT_BYTES = 2048
MAX_AUDIT_FIELDS = 64
_VERIFY_ON = {"1", "true", "on", "yes"}

# Key encoding, shared with sfdc24-site #296 head 9a0d8f85 (C14). repo_key is owner:name,
# for example "sfdc-24:conference". A colon is legal and a GitHub owner or name has none,
# so the first colon splits. Plus is outside the grammar. No percent-encoding.
KEY_ENCODING = "owner:name"
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


def complete_receipt(conn, acl, raw_tag, via, prefix, raw_value, enc, generation, correlation,
                     head, now=None) -> dict:
    """The backend complete receipt. The site calls this, not a local writer receipt.

    Signature: complete_receipt(conn, acl, raw_tag, via, prefix, raw_value, enc, generation,
    correlation, head, now=None) -> dict.

    Publish and ACTION both require ``backend_receipt_accepts`` of this dict. That is true
    only when ``ok`` and ``page_ok`` are both true, the union is complete, ``manifest_root``
    equals ``head_manifest_root``, and the head is unchanged (``moved`` is false and
    ``head_generation`` is the generation id). ``page_ok`` means every page in this receipt
    verified, so a full pass keeps it true. One page returns ``page_ok`` true and ``ok`` false.

    REDIS_GOV_VERIFY off returns a refusal with ``ok`` false and does not read documents.
    A missing receipt is not this function's return; ``backend_receipt_accepts(None)`` is false.
    The receipt carries counts and digests only. It has no listed key names.
    """
    return execute(conn, acl, raw_tag, via, VERIFY_OP, prefix, raw_value=raw_value, enc=enc,
                   generation=generation, correlation=correlation, head=head, now=now)


def backend_receipt_accepts(receipt) -> bool:
    """True only for a complete backend receipt the site may publish or mark ACTION ready.

    None, a flag-off refusal, a single page, a moved head, and a root mismatch are false.
    A full pass has page_ok true. The gate accepts that.
    """
    if not isinstance(receipt, dict):
        return False
    if receipt.get("ok") is not True or receipt.get("page_ok") is not True:
        return False
    if receipt.get("partial") or receipt.get("moved") or receipt.get("timeout"):
        return False
    if receipt.get("refused") or receipt.get("error"):
        return False
    coverage = receipt.get("coverage") or {}
    if coverage.get("covered") is not True:
        return False
    root = receipt.get("manifest_root") or ""
    head_root = receipt.get("head_manifest_root") or ""
    if not re.fullmatch(r"[0-9a-f]{64}", root) or root != head_root:
        return False
    if int(receipt.get("page_count") or 0) < 2:
        return False
    generation = receipt.get("generation") or ""
    if not re.fullmatch(r"[0-9a-f]{64}", generation):
        return False
    if receipt.get("head_generation") != generation:
        return False
    return True


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


def _parse_key_specs(keys) -> dict:
    """One page's {key: {sha256, len}}. Any other field is refused, unread."""
    if not isinstance(keys, dict) or not keys:
        raise Refused("haudit manifest needs a non-empty keys object")
    expected = {}
    for name, spec in keys.items():
        if not isinstance(name, str) or not _KEY.match(name):
            raise Refused("haudit key is not a key")
        if not isinstance(spec, dict) or set(spec) != {"sha256", "len"}:
            raise Refused("haudit key entry must be sha256 and len only")
        digest, length = spec["sha256"], spec["len"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Refused("haudit sha256 must be 64 hex characters")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise Refused("haudit len must be a byte length")
        if length > MAX_AUDIT_DOCUMENT_BYTES:
            raise Refused("haudit len is over 2048 bytes")
        expected[name] = {"sha256": digest, "len": length}
    return expected


def _pages_from_keys(specs) -> list:
    """Split a flat key map into pages of PAGE_SIZE, keys sorted. That sort is the index order."""
    names = sorted(specs)
    pages = []
    for index, start in enumerate(range(0, len(names), PAGE_SIZE)):
        chunk = names[start:start + PAGE_SIZE]
        pages.append({"index": index, "keys": {name: specs[name] for name in chunk}})
    return pages


def _parse_manifest(raw: str, enc: str) -> list:
    """b64 JSON of a paged manifest. Page size is 8. At most MAX_GENERATION_DOCS documents.

    ``{"keys": {...}}`` is split into pages of 8 in key order. ``{"pages": [{"index", "keys"}]}``
    is the explicit form. A gap, a repeated key, or a short page that is not the last of a
    multi-page submission is refused before any document read. One page is accepted and can be
    page_ok; it is never a full-generation pass. A stored string does not belong here."""
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
    if not isinstance(data, dict) or set(data) not in ({"keys"}, {"pages"}):
        raise Refused("haudit manifest must be one page of keys")
    if set(data) == {"keys"}:
        pages = _pages_from_keys(_parse_key_specs(data["keys"]))
    else:
        raw_pages = data["pages"]
        if not isinstance(raw_pages, list) or not raw_pages:
            raise Refused("haudit manifest needs pages")
        pages = []
        for page in raw_pages:
            if not isinstance(page, dict) or set(page) != {"index", "keys"}:
                raise Refused("haudit page must be index and keys only")
            index = page["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise Refused("haudit page index is not valid")
            pages.append({"index": index, "keys": _parse_key_specs(page["keys"])})
    reason = check_page_union(pages)
    if reason:
        raise Refused(reason)
    total = sum(len(page["keys"]) for page in pages)
    if total > MAX_GENERATION_DOCS:
        raise Refused("haudit manifest has too many keys")
    return pages


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


def _hex64(value) -> str:
    text = _text(value or "")
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _count_field(raw):
    """A head count, or None when the field is not a small integer. The raw text is not returned."""
    text = _text(raw or "").strip()
    if not re.fullmatch(r"[0-9]{1,3}", text):
        return None
    value = int(text)
    if value > MAX_GENERATION_DOCS:
        return None
    return value


def _hash_head(conn, key):
    """generation, manifest_root, page_count and listed, plus the raw fields for the movement compare.

    All four are read before any document. A string or stream is not read at all. Counts and the
    root digest are published only when they have the right shape, so a secret parked in a field
    cannot ride out on the result."""
    empty = {"generation": "", "manifest_root": "", "page_count": None, "listed": None,
             "raw_generation": "", "raw_manifest_root": "", "raw_page_count": "", "raw_listed": ""}
    kind = _text(conn.type(key))
    if kind != "hash":
        return empty
    raw_gen = _text(conn.hget(key, "generation") or "")
    raw_root = _text(conn.hget(key, "manifest_root") or "")
    raw_count = _text(conn.hget(key, "page_count") or "")
    raw_listed = _text(conn.hget(key, "listed") or "")
    return {
        "generation": _hex64(raw_gen),
        "manifest_root": _hex64(raw_root),
        "page_count": _count_field(raw_count),
        "listed": _count_field(raw_listed),
        "raw_generation": raw_gen,
        "raw_manifest_root": raw_root,
        "raw_page_count": raw_count,
        "raw_listed": raw_listed,
    }


def _head_raw(head) -> tuple:
    return (head.get("raw_generation"), head.get("raw_manifest_root"),
            head.get("raw_page_count"), head.get("raw_listed"))


def check_page_union(pages) -> str:
    """"" when the pages can be read. The reason does not contain a key name.

    One page of at most PAGE_SIZE keys is allowed and is only ever page_ok. Two or more pages
    must be indexes 0..N-1, each full page exactly PAGE_SIZE, no repeated key. More than
    MAX_GENERATION_DOCS documents is refused before any read."""
    if not pages:
        return "haudit needs a page"
    if len(pages) > MAX_AUDIT_PAGES:
        return "haudit manifest has too many pages"
    indexes = []
    seen = set()
    total = 0
    for page in pages:
        index = page.get("index")
        keys = page.get("keys") or {}
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= MAX_AUDIT_PAGES:
            return "haudit page index is not valid"
        if not keys:
            return "haudit needs a page"
        if len(keys) > PAGE_SIZE:
            return "haudit page is over the page size"
        indexes.append(index)
        total += len(keys)
        for name in keys:
            if name in seen:
                return "haudit key is listed on two pages"
            seen.add(name)
    if total > MAX_GENERATION_DOCS:
        return "haudit manifest has too many keys"
    if len(set(indexes)) != len(indexes):
        return "haudit page index is repeated"
    if len(pages) > 1:
        if sorted(indexes) != list(range(len(pages))):
            return "haudit pages are not contiguous"
        ordered = sorted(pages, key=lambda item: item["index"])
        for page in ordered[:-1]:
            if len(page["keys"]) != PAGE_SIZE:
                return "haudit page is short of the page size"
    return ""


def full_page_union(pages, page_count, listed) -> bool:
    """True when these pages are the whole generation: 0..N-1, N at least 2, counts match.

    A single page is never a full union. The head's page_count and listed have to name this set."""
    if not isinstance(page_count, int) or not isinstance(listed, int) or page_count < 2:
        return False
    if len(pages) != page_count or sum(len(page["keys"]) for page in pages) != listed:
        return False
    return check_page_union(pages) == "" and sorted(page["index"] for page in pages) == list(range(page_count))


def manifest_body(specs) -> bytes:
    """Canonical UTF-8 of the manifest object. The head key is not a member.

    ``{"keys": {name: {"len": n, "sha256": hex}}}`` with sorted keys and compact separators.
    ``len`` sorts before ``sha256``. This is the manifest digest's preimage, not the report."""
    keys = {name: {"len": int(specs[name]["len"]), "sha256": specs[name]["sha256"]}
            for name in specs}
    return json.dumps({"keys": keys}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def manifest_digest(specs) -> str:
    """SHA-256 hex of manifest_body(specs). This is not the page digest and not the root."""
    return hashlib.sha256(manifest_body(specs)).hexdigest()


def page_entries(specs) -> list:
    """One page's entries in index order: sorted by key, each ``{"key", "len", "sha256"}``."""
    return [{"key": name, "len": int(specs[name]["len"]), "sha256": specs[name]["sha256"]}
            for name in sorted(specs)]


def page_body(entries) -> bytes:
    """UTF-8 JSON of a page entries array. Object keys are sorted, separators are compact.

    ``[{"key": "...", "len": n, "sha256": "<64 hex>"}, ...]`` in the list's index order.
    ``ensure_ascii`` is false. This is the page digest's preimage. #296 head 45916666."""
    return json.dumps(list(entries), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def page_digest(entries) -> str:
    """SHA-256 hex of page_body(entries)."""
    return hashlib.sha256(page_body(entries)).hexdigest()


def root_body(page_digests, listed) -> bytes:
    """Canonical UTF-8 of the manifest root. Page digests stay in index order, 0..N-1.

    ``{"listed": n, "page_count": n, "pages": ["<page digest>", ...]}`` with sorted object keys,
    compact separators, and UTF-8 without ASCII escaping. ``page_count`` is the length of
    ``pages``. The head stores the SHA-256 of these bytes as ``manifest_root``, plus
    ``page_count`` and ``listed``. #296 head 45916666."""
    digests = [str(item) for item in page_digests]
    payload = {"listed": int(listed), "page_count": len(digests), "pages": digests}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def root_digest(page_digests, listed) -> str:
    """SHA-256 hex of root_body(page_digests, listed)."""
    return hashlib.sha256(root_body(page_digests, listed)).hexdigest()


def root_from_pages(pages) -> str:
    """Root digest of pages in index order. Entries within a page are sorted by key."""
    ordered = sorted(pages, key=lambda item: item["index"])
    digests = [page_digest(page_entries(page["keys"])) for page in ordered]
    listed = sum(len(page["keys"]) for page in ordered)
    return root_digest(digests, listed)


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
                   head_generation, audit="ok", refused="", error="", manifest_digest_hex="",
                   root_digest_hex="", head_root=""):
    """The receipt haudit returns. Counts and digests only. No listed key name, no stored string.

    ``page_ok`` is true when every page read in this receipt verified. A full receipt has
    ``page_ok`` and ``ok`` both true. One page has ``page_ok`` true and ``ok`` false. ``ok``
    also requires the full 0..N-1 union, the recomputed root, and an unchanged head.
    Partial or incomplete is never ok. ``extra`` is not a keyspace claim."""
    digest, length = _audit_report(entries)
    out = {
        "ok": False,
        "page_ok": False,
        "op": VERIFY_OP,
        "generation": generation,
        "correlation": correlation,
        "partial": bool(partial),
        "moved": bool(moved),
        "timeout": bool(timeout),
        "head_generation": head_generation if re.fullmatch(r"[0-9a-f]{64}", head_generation or "") else "",
        "head_manifest_root": head_root if re.fullmatch(r"[0-9a-f]{64}", head_root or "") else "",
        "coverage": {
            "expected": int(coverage.get("expected") or 0),
            "seen": int(coverage.get("seen") or 0),
            "missing": int(coverage.get("missing") or 0),
            "extra": int(coverage.get("extra") or 0),
            "changed": int(coverage.get("changed") or 0),
            "matched": int(coverage.get("matched") or 0),
            "covered": False,
        },
        "audit": audit or "",
    }
    out["digest"] = digest
    out["length"] = length
    out["listed"] = int(coverage.get("expected") or 0)
    out["pages"] = int(coverage.get("pages") or 0)
    out["manifest_digest"] = (manifest_digest_hex
                              if re.fullmatch(r"[0-9a-f]{64}", manifest_digest_hex or "") else "")
    out["manifest_root"] = (root_digest_hex
                            if re.fullmatch(r"[0-9a-f]{64}", root_digest_hex or "") else "")
    out["page_count"] = int(coverage.get("page_count") or coverage.get("pages") or 0)
    page_clean = (out["coverage"]["missing"] == 0 and out["coverage"]["extra"] == 0
                  and out["coverage"]["changed"] == 0
                  and out["coverage"]["matched"] == out["coverage"]["expected"]
                  and out["coverage"]["expected"] > 0 and not out["partial"] and not out["moved"]
                  and not out["timeout"] and not refused and not error and bool(coverage.get("page_read")))
    # A single page is page_ok only. Full ok needs the complete 0..N-1 union, N at least 2.
    full = page_clean and bool(coverage.get("complete"))
    out["coverage"]["covered"] = full
    out["page_ok"] = page_clean and audit == "ok"
    out["ok"] = full and audit == "ok"
    if refused:
        out["ok"] = False
        out["page_ok"] = False
        out["coverage"]["covered"] = False
        out["refused"] = str(refused)[:200]
    if error:
        out["ok"] = False
        out["page_ok"] = False
        out["coverage"]["covered"] = False
        out["error"] = str(error)[:80]
    return out


def _byte_len(value) -> int:
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8"))


def _document_too_big(pipe, name) -> bool:
    """True when the hash cannot be under 2048 bytes. Does not fetch field values.

    HLEN first, so a huge field set is refused before HKEYS. HSTRLEN then refuses a single
    oversized value before HGETALL. A 1.1M-character field stops here."""
    count = pipe.hlen(name)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > MAX_AUDIT_FIELDS:
        return True
    if count == 0:
        return False
    fields = pipe.hkeys(name) or []
    if len(fields) != count:
        return True
    total = 0
    for field in fields:
        if _byte_len(field) > MAX_AUDIT_DOCUMENT_BYTES:
            return True
        total += _byte_len(field)
        n = pipe.hstrlen(name, field)
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            return True
        if n > MAX_AUDIT_DOCUMENT_BYTES or total + n > MAX_AUDIT_DOCUMENT_BYTES:
            return True
        total += n
    return False


def _read_listed(pipe, names, records):
    """Digest each manifest key. Returns (partial, timed_out, hard_error, bound).

    Size is checked with HLEN and HSTRLEN before HGETALL. Over the bound does not fetch.
    Stops at the first failure so a partial read is not reported as a complete match.
    No key outside ``names`` is read."""
    partial = timed_out = bound = False
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
            if _document_too_big(pipe, name):
                bound = True
                partial = True
                break
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
            if length > MAX_AUDIT_DOCUMENT_BYTES:
                bound = True
                partial = True
                break
            records[name] = ("hash", digest, length)
        except Exception as error:
            partial = True
            if _is_timeout(error):
                timed_out = True
            else:
                hard_error = type(error).__name__
            break
    return partial, timed_out, hard_error, bound


def _read_in_batches(conn, head, names, before):
    """Read listed keys in batches of MAX_AUDIT_BATCH. Revalidate the head around every page.

    The head snapshot was taken before any document. Each batch watches the head and its keys,
    then reads at most 128 documents, one manifest page at a time. The head is read again after
    every page, including page 0, and TYPE is confirmed inside EXEC at the end of a clean batch.
    A head change at any of those points stops the read: later pages and later batches are not
    fetched, and the result is not ok. There is no KEYS and no SCAN."""
    records = {}
    partial = timed_out = bound = moved = False
    hard_error = ""
    snapshot = _head_raw(before)
    size = MAX_AUDIT_BATCH
    for start in range(0, len(names), size):
        batch = names[start:start + size]
        pipe = conn.pipeline(transaction=True)
        stop = False
        try:
            pipe.watch(head)
            try:
                now = _hash_head(pipe, head)
            except Exception as error:
                partial = True
                if _is_timeout(error):
                    timed_out = True
                else:
                    hard_error = type(error).__name__
                break
            if _head_raw(now) != snapshot:
                moved = True
                partial = True
                break
            for name in batch:
                pipe.watch(name)
            for page_start in range(0, len(batch), PAGE_SIZE):
                page = batch[page_start:page_start + PAGE_SIZE]
                read_partial, timed_out, hard_error, bound = _read_listed(pipe, page, records)
                partial = partial or read_partial
                if timed_out or hard_error or bound:
                    stop = True
                    break
                try:
                    after = _hash_head(pipe, head)
                except Exception as error:
                    partial = True
                    if _is_timeout(error):
                        timed_out = True
                    else:
                        hard_error = type(error).__name__
                    stop = True
                    break
                if _head_raw(after) != snapshot:
                    moved = True
                    partial = True
                    stop = True
                    break
            if stop:
                break
            pipe.multi()
            pipe.type(head)
            for name in batch:
                pipe.type(name)
            confirmed = pipe.execute(raise_on_error=False)
            failed = _exec_error(confirmed)
            if failed or len(confirmed) != 1 + len(batch):
                hard_error = failed or "ExecError"
                partial = True
                break
        except Exception as error:
            partial = True
            if type(error).__name__ == "WatchError":
                moved = True
            elif _is_timeout(error):
                timed_out = True
            else:
                hard_error = type(error).__name__
            break
        finally:
            pipe.reset()
    return records, partial, timed_out, hard_error, bound, moved


def _audit_generation(conn, acl, who, raw_tag, via, prefix, field, raw_value, enc,
                      generation, correlation, head, now) -> dict:
    """Receipt for a paged manifest. Root serialization is #296 head 45916666. Ceiling is 474.

    The head is bound from the generation namespace. generation, manifest_root, page_count and
    listed are read before any document and again after every manifest page and every batch.
    Only manifest-listed keys are read, each under WATCH. Pages are 8 keys. A generation may
    hold MAX_GENERATION_DOCS documents and is read in batches of MAX_AUDIT_BATCH. There is no
    KEYS, no SCAN, and no Lua walk. A document over 2048 bytes is refused before HGETALL. One
    page is page_ok and never ok. ok requires every page 0..N-1, no gaps or overlap, the
    recomputed root equal to manifest_root, and the head unchanged from the first read through
    the last page. A public route is refused before any read. The receipt has no key names."""
    if "public-sheet" in str(via or "").lower():
        raise Refused("haudit is not accepted from a public route")
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
    pages = _parse_manifest(raw_value, enc)
    expected = {}
    for page in pages:
        expected.update(page["keys"])
    for name in expected:
        if not name.startswith(prefix):
            raise Refused("a manifest key is outside the generation prefix")
        _guard_verify_key(acl, who, name)

    claimed = manifest_digest(expected)
    claimed_root = root_from_pages(pages)
    names = sorted(expected)
    pipe = conn.pipeline(transaction=True)
    records = {}
    partial = False
    moved = False
    timed_out = False
    hard_error = ""
    bound = False
    head_generation = ""
    head_root = ""
    head_match = False
    root_match = False
    complete = False
    started = False
    before = None
    try:
        pipe.watch(head)
        try:
            before = _hash_head(pipe, head)
        except Exception as error:
            if _is_timeout(error):
                timed_out = True
            else:
                hard_error = type(error).__name__
            partial = True
            before = None
        head_generation = (before or {}).get("generation") or ""
        head_root = (before or {}).get("manifest_root") or ""
        # Head -> generation id before any document read. A mismatch does not open the manifest keys.
        if not timed_out and not hard_error and before and before["raw_generation"] == generation:
            started = True
            (records, read_partial, timed_out, hard_error, bound, moved) = _read_in_batches(
                conn, head, names, before)
            partial = partial or read_partial
        head_match = bool(before) and before["raw_generation"] == generation and not moved
        root_match = (head_match and bool(head_root) and head_root == claimed_root
                      and before["page_count"] == len(pages) and before["listed"] == len(expected))
        complete = root_match and full_page_union(pages, before["page_count"] if before else None,
                                                   before["listed"] if before else None)
    finally:
        pipe.reset()

    entries = []
    missing = changed = matched = 0
    if started and not bound:
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
        "changed": changed, "matched": matched, "head_match": head_match, "root_match": root_match,
        "page_read": started and not bound, "pages": len(pages), "page_count": len(pages),
        "complete": complete,
    }
    refusal = "haudit document is over 2048 bytes" if bound else ""
    return _finish_audit(conn, acl, who, raw_tag, via, prefix, entries, generation, correlation,
                         coverage, partial=partial or bool(hard_error) or bound, moved=moved,
                         timeout=timed_out, head_generation=head_generation, now=now,
                         error=("TimeoutError" if timed_out else hard_error),
                         manifest_digest_hex=claimed, root_digest_hex=claimed_root,
                         head_root=head_root, refused=refusal)


def _finish_audit(conn, acl, who, raw_tag, via, key, entries, generation, correlation, coverage,
                  partial, moved, timeout, head_generation, now, error="", manifest_digest_hex="",
                  root_digest_hex="", head_root="", refused=""):
    result = _publish_audit(entries, generation, correlation, coverage, partial, moved, timeout,
                            head_generation, audit="ok", error=error, refused=refused,
                            manifest_digest_hex=manifest_digest_hex,
                            root_digest_hex=root_digest_hex, head_root=head_root)
    reason = "missing=%d extra=%d changed=%d partial=%d moved=%d timeout=%d page_ok=%d" % (
        result["coverage"]["missing"], result["coverage"]["extra"], result["coverage"]["changed"],
        int(result["partial"]), int(result["moved"]), int(result["timeout"]), int(result["page_ok"]))
    try:
        _audit_alone(conn, audit_fields(acl, who, raw_tag, via, VERIFY_OP, key, "", result["ok"],
                                        reason, now=now))
    except Exception as failure:
        result["audit"] = "FAILED:" + type(failure).__name__
        result["ok"] = False
        result["page_ok"] = False
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
    # Counts and digests only. Listed key names are not part of a board line.
    return ("HAUDIT ok=%d page_ok=%d gen=%s cid=%s listed=%d page_count=%d expected=%d seen=%d "
            "missing=%d extra=%d changed=%d matched=%d covered=%d partial=%d moved=%d timeout=%d "
            "manifest_root=%s manifest=%s digest=%s length=%d%s" % (
                int(bool(result.get("ok"))), int(bool(result.get("page_ok"))),
                result.get("generation") or "-", result.get("correlation") or "-",
                int(result.get("listed") or cov.get("expected") or 0),
                int(result.get("page_count") or result.get("pages") or 0),
                int(cov.get("expected") or 0),
                int(cov.get("seen") or 0), int(cov.get("missing") or 0), int(cov.get("extra") or 0),
                int(cov.get("changed") or 0), int(cov.get("matched") or 0),
                int(bool(cov.get("covered"))), int(bool(result.get("partial"))),
                int(bool(result.get("moved"))), int(bool(result.get("timeout"))),
                result.get("manifest_root") or "-", result.get("manifest_digest") or "-",
                result.get("digest") or "-", int(result.get("length") or 0),
                (" error=%s" % result["error"]) if result.get("error") else ""))


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
