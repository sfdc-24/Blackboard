#!/usr/bin/env python3
r"""Durable fleet state with compare-and-swap. Stdlib only.

WHAT THIS CLOSES
    Dependency 3 of docs/OPENAI-CLOUD-MIGRATION.md: "waker watermarks are local
    JSON files". A cursor that lives on one laptop's disk is a cursor the fleet
    loses when that laptop is rebuilt, and it is the last thing pinning the
    unattended lane to this machine now that credentials (#187) and the trigger
    (#189) have moved.

WHY COMPARE-AND-SWAP IS THE POINT, NOT DURABILITY
    Durability alone would be a file in a bucket. The reason this is a store with
    tokens is that a watermark is READ-MODIFY-WRITTEN, and this fleet has already
    paid for unsynchronised writers more than once - two surfaces under one tag is
    the collision class behind three incidents, and /listen/ and /stream/ happened
    because two agents edited one thing the same night.

    Every save carries the token the load returned. A stale token is REFUSED, not
    merged and not overwritten, so a cloud waker and a laptop waker running at the
    same moment cannot silently erase each other's progress - one of them is told
    it lost and can re-read.

    GCS object generations give this natively: `ifGenerationMatch=<generation>`
    for an update, and `ifGenerationMatch=0` for "only if it does not exist yet".
    That is a real CAS, not a lock with a timeout.

NEVER BACKWARDS
    `advance()` exists so the rule the wakers learned the hard way lives in ONE
    place: a watermark may move forward or stay, never back. A cursor that moves
    backwards re-answers rows; one that jumps forward past unread work loses them
    silently, which is the worse of the two and the reason #168 was written.

STDLIB ONLY, DELIBERATELY
    No google-cloud-storage. An import that is absent in a sandbox is the
    dependency this whole migration exists to remove. Auth comes from the GCE /
    Cloud Run metadata server, which is present wherever this is meant to run.

    from state_store import open_store
    store = open_store()                       # BLACKBOARD_STATE_URI, or a file
    state, token = store.load("board_waker")
    state["watermark"] = "2026-09-23T21:00:00Z"
    store.save("board_waker", state, token)    # Conflict if someone else wrote
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token")


class Conflict(Exception):
    """Someone else wrote since the load. The caller must re-read, not retry blind.

    Deliberately not a retry loop inside save(): the correct response depends on
    what the caller was doing. A watermark advance should re-read and re-decide;
    a blind retry would re-apply a decision made against stale state, which is
    the bug CAS exists to prevent.
    """


# --------------------------------------------------------------- file backend

class FileStore:
    """The laptop path. Token is the file's mtime_ns plus size.

    NOT as strong as the GCS generation - two writers inside the same mtime tick
    could both believe they hold the token. That is acceptable here because the
    laptop lane has exactly one writer per tag by construction (one scheduled
    task, and it refuses to run alongside a live session), and it is written down
    rather than glossed so nobody later assumes this is a distributed lock.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "._-")
        if not safe:
            raise ValueError("state name %r has no usable characters" % name)
        return self.root / (safe + ".json")

    def describe(self) -> str:
        return "file:%s" % self.root

    def load(self, name: str):
        p = self._path(name)
        if not p.is_file():
            return {}, None                      # None means "did not exist"
        st = p.stat()
        try:
            return json.loads(p.read_text(encoding="utf-8")), "%d:%d" % (
                st.st_mtime_ns, st.st_size)
        except json.JSONDecodeError:
            # A corrupt cursor is not an empty cursor. Refusing here beats
            # starting from the beginning and re-answering the whole board.
            raise Conflict("%s is not valid JSON; refusing to treat it as empty"
                           % p)

    def save(self, name: str, state: dict, token):
        p = self._path(name)
        _, current = self.load(name) if p.is_file() else ({}, None)
        if current != token:
            raise Conflict("state changed since it was read (%r != %r)"
                           % (current, token))
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, p)                       # atomic on both platforms
        _, new = self.load(name)
        return new


# ---------------------------------------------------------------- GCS backend

class GcsStore:
    """gs://bucket/prefix, with generation-based compare-and-swap."""

    def __init__(self, bucket: str, prefix: str = "", token_provider=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._token_provider = token_provider or metadata_token
        self._cached = (None, 0.0)

    def describe(self) -> str:
        return "gs://%s/%s" % (self.bucket, self.prefix)

    def _object(self, name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in "._-")
        if not safe:
            raise ValueError("state name %r has no usable characters" % name)
        return "%s/%s.json" % (self.prefix, safe) if self.prefix else safe + ".json"

    def _token(self) -> str:
        tok, expires = self._cached
        if tok and time.time() < expires - 60:
            return tok
        tok, ttl = self._token_provider()
        self._cached = (tok, time.time() + ttl)
        return tok

    def _request(self, url: str, method="GET", body=None, ctype=None):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", "Bearer " + self._token())
        if ctype:
            req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()

    def load(self, name: str):
        # Metadata first, then the bytes of THAT generation. The other order
        # is two requests against a moving object: the body can be the old
        # version while the generation token is already the new one, and the
        # next save is then told it holds the current token and silently
        # erases the writer it never read.
        obj = urllib.parse.quote(self._object(name), safe="")
        meta_url = ("https://storage.googleapis.com/storage/v1/b/%s/o/%s"
                    % (self.bucket, obj))
        # Bind the body to the same immutable generation used as the CAS token.
        # A body GET followed by a metadata GET can otherwise pair stale JSON
        # with a fresh generation and let a later save overwrite a concurrent
        # writer. Metadata first + generation-qualified media either returns
        # the exact version or races with deletion/overwrite and is retried.
        for _ in range(4):
            try:
                _, metaraw = self._request(meta_url)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {}, None              # None means "does not exist"
                raise
            generation = str(json.loads(metaraw.decode("utf-8"))["generation"])
            media_url = (meta_url + "?alt=media&generation="
                         + urllib.parse.quote(generation, safe=""))
            try:
                _, raw = self._request(media_url)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
            try:
                return json.loads(raw.decode("utf-8")), generation
            except json.JSONDecodeError:
                raise Conflict("gs://%s/%s is not valid JSON; refusing to treat it "
                               "as empty" % (self.bucket, self._object(name)))
        raise Conflict("gs://%s/%s changed repeatedly while being read"
                       % (self.bucket, self._object(name)))

    def save(self, name: str, state: dict, token):
        # ifGenerationMatch=0 means "only if this object does not exist", which is
        # how a first write is made safe against two processes both creating it.
        generation = "0" if token is None else str(token)
        url = ("https://storage.googleapis.com/upload/storage/v1/b/%s/o"
               "?uploadType=media&name=%s&ifGenerationMatch=%s"
               % (self.bucket,
                  urllib.parse.quote(self._object(name), safe=""), generation))
        body = json.dumps(state, indent=1, sort_keys=True).encode("utf-8")
        try:
            _, raw = self._request(url, method="POST", body=body,
                                   ctype="application/json")
        except urllib.error.HTTPError as e:
            if e.code == 412:                    # precondition failed: lost CAS
                raise Conflict(
                    "another writer changed %s since it was read (generation %s "
                    "no longer current)" % (self._object(name), generation))
            raise
        return str(json.loads(raw.decode("utf-8"))["generation"])


def metadata_token():
    """An access token from the GCE / Cloud Run metadata server.

    Present wherever this is meant to run and absent on the laptop, which is
    correct: the laptop lane uses the file backend. Returns (token, ttl_seconds).
    """
    req = urllib.request.Request(METADATA_TOKEN_URL)
    req.add_header("Metadata-Flavor", "Google")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "no metadata server, so no GCS credentials: %s\n"
            "This backend is for a cloud runtime. On a workstation leave "
            "BLACKBOARD_STATE_URI unset and the file backend is used." % exc)
    return d["access_token"], int(d.get("expires_in") or 3600)


# ------------------------------------------------------------------- frontend

def open_store(uri: str | None = None, token_provider=None):
    """file path or gs://bucket/prefix, from BLACKBOARD_STATE_URI by default."""
    uri = uri or os.environ.get("BLACKBOARD_STATE_URI") or ""
    uri = uri.strip()
    if uri.startswith("gs://"):
        rest = uri[len("gs://"):]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise ValueError("gs:// URI has no bucket: %r" % uri)
        return GcsStore(bucket, prefix, token_provider)
    if uri.startswith("file://"):
        uri = uri[len("file://"):]
    if not uri:
        # The existing laptop contract: BLACKBOARD_STATE_DIR, else the repo root.
        uri = (os.environ.get("BLACKBOARD_STATE_DIR")
               or str(Path(__file__).resolve().parents[1]))
    return FileStore(Path(uri))


def advance(store, name: str, key: str, value: str, attempts: int = 5) -> dict:
    """Move a cursor forward, never back, under compare-and-swap.

    The rule lives here so it is stated once. A cursor that moves BACKWARDS
    re-answers rows already handled; one that jumps FORWARD past unread work
    loses them silently, and the second is the worse failure - it is what #168
    was written to prevent.

    Retries the read-modify-write on Conflict, which is safe precisely because it
    RE-READS each time: the comparison is redone against whatever the other
    writer left, so the "never backwards" rule holds against their value too.
    """
    last = None
    for attempt in range(attempts):
        state, token = store.load(name)
        current = str(state.get(key) or "")
        if current and str(value) <= current:
            # Not an error: two writers reaching the same point is normal, and a
            # later run seeing an older row must not drag the cursor back.
            return {"changed": False, "value": current, "reason": "not newer"}
        state[key] = value
        try:
            store.save(name, state, token)
            return {"changed": True, "value": value, "previous": current or None,
                    "attempts": attempt + 1}
        except Conflict as exc:
            last = exc
            time.sleep(0.2 * (attempt + 1))
    raise Conflict("could not advance %s after %d attempts: %s"
                   % (key, attempts, last))
