"""A read/write connectivity probe for redis-central. Proves the path, touches nothing that matters.

WHY THIS IS SEPARATE FROM THE RECONCILER, AND WHY IT DOES NOT BREAK THE HOLD
Grok's sequencing and Codex's hold both say the dual-run stays off until PR 167's holds clear and the
scaffold is verified. That hold is about PRODUCTION PATHS CONSULTING REDIS. This is not that: it
writes one key under its own `probe:` prefix, reads it back, deletes it, and exits. It never touches
a `bus:` key, never reads the board, and nothing anywhere reads what it writes. Proving the pipe
works and switching traffic onto it are two different acts, and only the second one is held.

WHAT A PASS ACTUALLY PROVES, stated so nobody reads more into it than it earns:
  - Direct VPC egress reaches the private address                        (the TCP connection)
  - TLS with SERVER_AUTHENTICATION verifies against the mounted CA       (the handshake)
  - The AUTH string in REDIS_AUTH_STRING matches THIS instance           (the AUTH command)
  - The service account may read the secrets it is mounted              (the mount)
  - A write lands and reads back byte-for-byte                           (SET then GET)
  - A delete removes it                                                  (DEL then GET is None)
It proves nothing about the board, nothing about divergence, and nothing about whether the dual-run
should be switched on.

EVERY KEY IT WRITES CARRIES A TTL AND IS DELETED ANYWAY. A probe that leaves state behind is a probe
that pollutes the thing it was measuring, and the reconciler's own extra-keys-in-Redis check would
later report its litter as a divergence.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import uuid

sys.path.insert(0, "/app")

import redis_dual                                                        # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("redis-probe")

PREFIX = redis_dual.KEY_VERSION + "probe:claude:"
TTL_SECONDS = 120

MOUNTED_CA = "/secrets/ca/redis-ca.pem"
MOUNTED_AUTH = "/secrets/auth/redis-auth"
for _name, _default in (("REDIS_CA_CERT_PATH", MOUNTED_CA), ("REDIS_AUTH_FILE", MOUNTED_AUTH)):
    if not os.environ.get(_name) and os.path.exists(_default):
        os.environ[_name] = _default


def main() -> int:
    settings = redis_dual.Settings()
    status = redis_dual.status(settings)
    log.info("status: %s", json.dumps({k: status[k] for k in redis_dual.STATUS_FOR_LOG}))

    # precheck=False on purpose: see redis_dual.client. A 1.5-second TCP pre-check from a cold
    # container said unreachable and made this probe report FAIL on a path that worked. A probe
    # whose job is to connect must let the connect be the answer.
    conn = redis_dual.client(settings, precheck=False)
    if conn is None:
        log.error("FAIL: no connection. Nothing was proved; this is not a working path.")
        return 1

    key = PREFIX + uuid.uuid4().hex[:12]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # A value with no meaning to anything, so a leftover cannot be mistaken for data.
    value = json.dumps({"probe": "claude-code-cli", "at": stamp, "purpose": "read/write proof only"})

    failures = []
    try:
        log.info("PING -> %s", conn.ping())

        conn.setex(key, TTL_SECONDS, value)
        log.info("SET %s (ttl %ds)", key, TTL_SECONDS)

        got = conn.get(key)
        if got == value:
            log.info("GET matched byte-for-byte: %d chars", len(got))
        else:
            failures.append("GET did not match what SET wrote")
            log.error("GET MISMATCH: wrote %d chars, read %s chars",
                      len(value), "None" if got is None else len(got))

        ttl = conn.ttl(key)
        log.info("TTL %s -> %ss", key, ttl)
        if not (isinstance(ttl, int) and 0 < ttl <= TTL_SECONDS):
            failures.append("the TTL was not set, so a leftover key would never expire")

        conn.delete(key)
        if conn.get(key) is None:
            log.info("DEL %s: gone", key)
        else:
            failures.append("DEL left the key behind")

        # Nothing of ours should be in the keyspace after this. Counted, never listed: a key name on
        # this board's fleet can carry a Row_ID.
        left = len(list(conn.scan_iter(match=PREFIX + "*", count=100)))
        log.info("probe keys left behind: %d", left)
        if left:
            failures.append("%d probe key(s) left in the keyspace" % left)

        # Read-only, and useful: is this the instance we think it is.
        info = conn.info("server")
        log.info("server: redis %s, mode %s, uptime %ss",
                 info.get("redis_version"), info.get("redis_mode"), info.get("uptime_in_seconds"))
        db = conn.info("keyspace")
        log.info("keyspace: %s", json.dumps(db) if db else "empty")
    except Exception as error:
        # The TYPE, never the message: a client's error text can quote what it was sent, and what it
        # was sent is an AUTH string.
        log.error("FAIL (%s): the path does not work. Nothing was proved.", type(error).__name__)
        return 1

    if failures:
        for line in failures:
            log.error("FAIL: %s", line)
        return 1
    log.info("PASS: egress, TLS against the mounted CA, AUTH for THIS instance, write, read-back and "
             "delete all work. This proves the PIPE. It proves nothing about divergence and is not "
             "authority to switch any traffic onto it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
