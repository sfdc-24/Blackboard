"""Print what is actually in redis-central, read-only, so a person can see the work in progress.

WHY THIS EXISTS AT ALL
    `redis-central` has a PRIVATE address inside the VPC. The Google Cloud console shows the
    instance - memory, connections, operations per second, whether it is up - and shows NOTHING
    about its contents. There is no key browser. Memorystore has no equivalent of the Firestore
    data tab, and nothing on the owner's laptop can reach the address at all.

    So a viewer has to run INSIDE the VPC and print. This is that viewer. It is a Cloud Run job on
    the same image and the same least-privilege identity as `bus-reconciler`, and its whole output
    is its log.

NAMED keyspace_view AND NOT inspect: a module called inspect.py in the image's WORKDIR
    shadows the standard library's `inspect`, which the redis client and its dependencies import.

READ-ONLY BY CONSTRUCTION, not by intention
    Every command it issues is in READ_ONLY below, and `run()` refuses anything else before it is
    sent. That is the guard rather than a convention, because this is the one piece of Redis code a
    person will reach for while something is going wrong, which is exactly when a stray DEL is most
    expensive and least noticed.

WHAT IT WILL NOT PRINT
    A value whose KEY looks like a credential is shown as its type and length only. The roster and
    the progress keys are fleet reference data and are printed in full; nothing in the current
    keyspace is sensitive, and this stays true only if it is enforced rather than assumed.

    It also never prints more than SAMPLE_LIMIT members of any one collection. A viewer that dumps
    a stream with 40,000 entries into a log is not a viewer.

    python keyspace_view.py            # the whole keyspace, grouped by prefix
    python keyspace_view.py v1:agent:  # only keys under one prefix
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redis_dual  # noqa: E402

SAMPLE_LIMIT = int(os.environ.get("INSPECT_SAMPLE_LIMIT", "25"))
SCAN_LIMIT = int(os.environ.get("INSPECT_SCAN_LIMIT", "5000"))

READ_ONLY = frozenset((
    "ping", "dbsize", "info", "scan", "type", "ttl", "memory", "object",
    "get", "strlen", "hgetall", "hlen", "smembers", "scard",
    "lrange", "llen", "zrange", "zcard", "xlen", "xrange", "xinfo",
))

# A key name that suggests a credential. The VALUE is never printed for these - only type and size.
SECRETISH = re.compile(r"(secret|token|auth|password|passwd|credential|api[_-]?key|bearer)", re.I)


def run(conn, command, *args):
    """Issue one command, after refusing anything that is not in the read-only set.

    The check is on the command this function was asked for, not on what the caller believes it
    asked for, so a typo or a future edit that reaches for DEL fails here rather than on the server.
    """
    name = str(command).strip().lower()
    if name not in READ_ONLY:
        raise RuntimeError("keyspace_view.py is read-only and refuses %r" % command)
    return getattr(conn, name)(*args)


def describe(conn, key):
    """One key: its type, TTL, size and a bounded, redacted sample of what is in it."""
    kind = run(conn, "type", key)
    kind = kind.decode() if isinstance(kind, bytes) else str(kind)
    ttl = run(conn, "ttl", key)
    expiry = "no expiry" if ttl == -1 else ("missing" if ttl == -2 else "%ss left" % ttl)

    if SECRETISH.search(str(key)):
        return kind, expiry, "<redacted: the key name looks like a credential>"

    if kind == "hash":
        fields = run(conn, "hgetall", key) or {}
        items = sorted(fields.items())[:SAMPLE_LIMIT]
        more = "" if len(fields) <= SAMPLE_LIMIT else "  ... %d more field(s)" % (len(fields) - SAMPLE_LIMIT)
        body = "\n".join("      %-18s %s" % (f, _short(v)) for f, v in items) + more
        return kind, expiry, "%d field(s)\n%s" % (len(fields), body)
    if kind == "set":
        members = sorted(run(conn, "smembers", key) or [])
        head = ", ".join(str(m) for m in members[:SAMPLE_LIMIT])
        more = "" if len(members) <= SAMPLE_LIMIT else " ... and %d more" % (len(members) - SAMPLE_LIMIT)
        return kind, expiry, "%d member(s): %s%s" % (len(members), head, more)
    if kind == "stream":
        length = run(conn, "xlen", key)
        entries = run(conn, "xrange", key, "-", "+", SAMPLE_LIMIT) or []
        body = "\n".join("      %s  %s" % (eid, _short(dict(fields))) for eid, fields in entries)
        return kind, expiry, "%d entr(ies), newest %d shown\n%s" % (length, len(entries), body)
    if kind == "string":
        return kind, expiry, _short(run(conn, "get", key))
    if kind == "list":
        return kind, expiry, "%d item(s): %s" % (run(conn, "llen", key),
                                                 _short(run(conn, "lrange", key, 0, SAMPLE_LIMIT - 1)))
    return kind, expiry, "(no reader for type %s)" % kind


def _short(value, width=96):
    text = str(value)
    return text if len(text) <= width else text[:width - 3] + "..."


def keyspace(conn, pattern="*"):
    """Every key matching the pattern, bounded. SCAN, never KEYS: KEYS blocks the server."""
    found, truncated = [], False
    for key in conn.scan_iter(match=pattern, count=500):
        found.append(key.decode() if isinstance(key, bytes) else str(key))
        if len(found) >= SCAN_LIMIT:
            truncated = True
            break
    return sorted(found), truncated


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pattern = (argv[0] if argv else "*")
    if not pattern.endswith("*"):
        pattern += "*"

    settings = redis_dual.Settings()
    conn = redis_dual.client(settings, precheck=False)
    if conn is None:
        # An empty read and an unreachable store are different answers, and only one of them means
        # "there is nothing there". Never collapse them.
        print("UNKNOWN: no Redis connection. This is NOT a report that the keyspace is empty.")
        return 2

    print("redis-central, read-only inspection")
    print("pattern: %s" % pattern)
    print("dbsize : %s key(s) in the whole keyspace" % run(conn, "dbsize"))
    print()

    keys, truncated = keyspace(conn, pattern)
    if not keys:
        print("No key matches %r. The connection worked, so this is a real empty, not an UNKNOWN."
              % pattern)
        return 0

    groups = {}
    for key in keys:
        parts = key.split(":")
        prefix = ":".join(parts[:3]) if len(parts) > 3 else ":".join(parts[:-1]) or key
        groups.setdefault(prefix, []).append(key)

    print("%d key(s) matched, in %d group(s)%s" % (
        len(keys), len(groups), "  [TRUNCATED at %d]" % SCAN_LIMIT if truncated else ""))
    for prefix in sorted(groups):
        print()
        print("=" * 78)
        print("%s      %d key(s)" % (prefix, len(groups[prefix])))
        print("=" * 78)
        for key in groups[prefix]:
            kind, expiry, body = describe(conn, key)
            print("  %s   [%s, %s]" % (key, kind, expiry))
            print("    %s" % body)
    print()
    print("Read-only: %d command name(s) are permitted and every other is refused before it is sent."
          % len(READ_ONLY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
