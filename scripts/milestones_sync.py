"""Rewrite the live milestone chart in Redis from its durable copy in the repo, on a schedule.

WHY. Mr. Salam, 2026-10-08 ~18:05Z: "keep the repo copy, Redis as the live copy". redis-central has
persistence DISABLED and proj: keys expire 30 days after their last write, so without a rewrite the
conference screen's chart goes blank a month after the last edit - the one concrete finding of Gemini's
review the same evening. Then, directly: "set up the scheduled milestone rewrite".

WHAT IT DOES, once per run:
  1. Reads docs/milestones/milestones-v1.json from main on GitHub - the durable copy - and refuses the
     whole file if any hash's `source` does not match the `sha256` stored beside it.
  2. Reads the live chart's meta version from Redis and decides:
       - live missing (expired, flushed) or OLDER than the repo  -> write the repo copy
       - live the SAME version                                   -> write it again (refreshes the TTL)
       - live NEWER than the repo                                -> HOLD: write nothing, and say so.
     The last case matters: Grok regenerates the chart, and a newer chart that reached Redis but not
     the repo must not be overwritten by an older file. It is reported so it gets committed - the repo
     stays the record.
  3. Writes each hash with hput through redis_gov - the same access list, the same protected
     namespaces, one audit entry per hash in gov:audit - as its own principal, milestones-sync, which
     may write proj:milestones: and nothing else.
No model call, no board write except a NOTE when it holds or fails.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import urllib.request

import redis_gov

SOURCE_URL = "https://raw.githubusercontent.com/sfdc-24/Blackboard/main/docs/milestones/milestones-v1.json"
COMMIT_URL = ("https://api.github.com/repos/sfdc-24/Blackboard/commits"
              "?path=docs/milestones/milestones-v1.json&sha=main&per_page=1")
PREFIX = "proj:milestones:v1:"
META = PREFIX + "meta"
PRINCIPAL = "milestones-sync"
MAX_BYTES = 200_000
_VERSION = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(\d{1,4})$")


class BadSource(ValueError):
    pass


def version_of(value):
    """'2026-10-08.1' -> ('2026-10-08', 1); anything else -> None (never guessed)."""
    m = _VERSION.match(str(value or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def parse(raw: bytes) -> dict:
    """{key: fields} from the repo file, or BadSource. Every key under PREFIX, every field a string,
    every stored sha256 equal to its source's."""
    if len(raw) > MAX_BYTES:
        raise BadSource("the file is over %d bytes" % MAX_BYTES)
    try:
        keys = json.loads(raw.decode("utf-8"))["keys"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise BadSource("not the milestones file shape")
    out = {}
    for key, entry in keys.items():
        fields = (entry or {}).get("fields") if isinstance(entry, dict) else None
        if not key.startswith(PREFIX) or not isinstance(fields, dict) or not fields:
            raise BadSource("unexpected key or entry: %s" % str(key)[:80])
        if not all(isinstance(v, str) for v in fields.values()):
            raise BadSource("a non-string field in %s" % key)
        if "source" in fields and "sha256" in fields:
            got = hashlib.sha256(fields["source"].encode("utf-8")).hexdigest()
            if got != fields["sha256"]:
                raise BadSource("%s: source sha256 %s does not match the stored %s"
                                % (key, got[:16], fields["sha256"][:16]))
        out[key] = fields
    if META not in out or version_of(out[META].get("version")) is None:
        raise BadSource("no meta version in the file")
    return out


def decide(repo: dict, live_meta) -> str:
    """write | hold. `live_meta` is the live meta hash (may be empty)."""
    live = version_of((live_meta or {}).get("version"))
    if live is None:
        return "write"
    return "hold" if live > version_of(repo[META]["version"]) else "write"


def sync(conn, acl, repo: dict, commit: str, now=None) -> dict:
    """Apply the decision. Returns {decision, written, failed, repo_version, live_version}."""
    live_meta = {}
    try:
        live_meta = conn.hgetall(META) or {}
    except Exception as error:                                          # noqa: BLE001
        return {"decision": "error", "error": type(error).__name__, "written": [], "failed": []}
    live_meta = {redis_gov._text(k): redis_gov._text(v) for k, v in live_meta.items()}
    out = {"decision": decide(repo, live_meta), "written": [], "failed": [],
           "repo_version": repo[META]["version"], "live_version": live_meta.get("version", "")}
    if out["decision"] == "hold":
        return out
    # meta LAST: a reader that sees the new meta version sees every hash it describes already written.
    for key in sorted(repo, key=lambda k: (k == META, k)):
        value = json.dumps(repo[key], separators=(",", ":"), ensure_ascii=False)
        result = redis_gov.execute(conn, acl, PRINCIPAL, "sync/%s" % commit[:12], "hput", key,
                                   raw_value=base64.b64encode(value.encode("utf-8")).decode("ascii"),
                                   enc="b64", now=now)
        (out["written"] if result.get("ok") else out["failed"]).append(
            key if result.get("ok") else "%s: %s" % (key, result.get("refused") or result.get("error")))
    return out


def fetch(url, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sfdc24-milestones-sync"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_BYTES + 1)


def source_commit() -> str:
    try:
        got = json.loads(fetch(COMMIT_URL).decode("utf-8"))
        return str(got[0]["sha"])
    except Exception:                                                   # noqa: BLE001
        return "main"


def main() -> int:
    import logging
    import sys
    sys.path.insert(0, "/app")
    import redis_dual                                                   # noqa: PLC0415
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("milestones-sync")

    def note(gist, text):
        try:
            from bus import load_env                                    # noqa: PLC0415
            from serve_requests import appender                         # noqa: PLC0415
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%MZ")
            rid = "MILESTONES-SYNC-%s" % stamp
            appender(load_env())({
                "row_id": rid, "source_tag": PRINCIPAL, "target_surface": "grok;claude-code-cli",
                "action_type": "NOTE", "category": "OPEN", "project_tag": "Blackboard", "gist": gist[:100],
                "payload": "BCB|v=1|id=%s|phase=NOTE|from=%s|to=grok,claude-code-cli|evidence=MEASURED|text=%s"
                           % (rid, PRINCIPAL, " ".join(text.replace("|", "/").split()))})
        except Exception as error:                                      # noqa: BLE001
            log.error("could not post the board note: %s", type(error).__name__)

    try:
        repo = parse(fetch(SOURCE_URL))
    except Exception as error:                                          # noqa: BLE001
        log.error("repo copy unusable: %s", error)
        note("Milestones sync FAILED: repo copy unusable", "The repo copy could not be used: %s. Redis "
             "was not touched." % (error,))
        return 1
    commit = source_commit()
    conn = redis_dual.client(redis_dual.Settings(), precheck=False)
    if conn is None:
        log.error("no Redis connection")
        note("Milestones sync FAILED: no Redis connection", "No Redis connection; nothing written.")
        return 1
    out = sync(conn, redis_gov.load_acl(), repo, commit)
    log.info("milestones sync: %s", json.dumps(out, sort_keys=True))
    if out["decision"] == "hold":
        note("Milestones sync HELD: Redis is newer than the repo",
             "Redis holds milestones version %s, newer than the repo copy %s at %s. Nothing was "
             "overwritten. Commit the newer chart to docs/milestones/milestones-v1.json so the repo stays "
             "the record; until then the live copy is not refreshed and expires 30 days after its last "
             "write." % (out["live_version"], out["repo_version"], commit[:12]))
        return 0
    if out["failed"] or out["decision"] == "error":
        note("Milestones sync FAILED on %d hash(es)" % len(out["failed"]),
             "Written: %d. Failed: %s." % (len(out["written"]), "; ".join(out["failed"]) or out.get("error")))
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
