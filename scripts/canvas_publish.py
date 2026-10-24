"""The live canvas's read path: agents' public canvas items, filtered, published as one static file per room.

THE SHAPE (spec v0.2, CLAUDE-MOBILE-CANVAS-SPEC-V02-20261020T2145Z; the owner approved "a small read path",
2026-10-20 ~17:30 ET). No new public server and no new credential holder:

    an agent      hset conf:canvas:<room>:<agent> <item id> <JSON>      (redis-op, the governed route, today)
    a board ask   do=canvas-publish room=<room> minutes=<1..120>         (opens a time-boxed window)
    this worker   every PUBLISH_SECONDS while a window is open: read the partitions of the allowlisted
                  agents, keep only public items, only allowlisted fields, types and hosts, and write
                  {"format": "proto-canvas/1", ...} to gs://<CANVAS_BUCKET>/rooms/<room>.json
    the page      sfdc24-site #353 polls that file and draws only items whose version is newer

WHAT THE FILE IS. Everything in it is public by design: the bucket is world-readable. So the boundary is the
writer's explicit "public": true, never a pattern filter, and nothing here tries to tell a private sentence
from a public one. An item without public:true never leaves Redis.

WHO AN ITEM BELONGS TO is the <agent> segment of the key it was read from, written into the item by this
module as "agent". Nothing inside the value can name its owner. The board tag a redis-op is posted under is
not authenticated, so one fleet agent can still write another's partition: that is the stated phase-1 limit,
closed in phase 2 by moving canvas writes to the bridge's IAM-authenticated /op with an own-partition rule.

AN ITEM'S PUBLISHED ID is "<agent>/<field>". The page keys items by id, and two agents both naming an item
"1" would otherwise collide, and the page would silently keep only the one with the higher version.

THE WINDOW lives in Redis (gov:canvas:publish:<room>, under gov:, which no agent may write) with the window's
own TTL, not in this process. So whichever live worker holds gov:worker:lock publishes it, and a window
survives the hand-over between two live runs. While no live run is up, nothing is published and the file
keeps its last contents; its "generated" time says how old it is.

THE UPLOAD is the Cloud Storage JSON API over HTTPS with the job's own token from the metadata server: stdlib
only, so the image gains no dependency. Overwriting an object needs storage.objects.delete as well as create,
so roles/storage.objectCreator alone is NOT enough for this: the bucket grant is roles/storage.objectUser,
on that one bucket only (cloud/bus-reconciler/canvas-bucket.sh, which nobody has run).

MERMAID IS CHECKED HERE, BEFORE THE FILE (Gemini, CLAUDE-MOBILE-GEMINI-SPILL-CHAIR-20261021T0135Z: guest speech
reaches the chair's diagrams, and Mermaid can carry script depending on the renderer). The page's own sanitizer
cleans the SVG that Mermaid RETURNS; some of Mermaid's published flaws run while it renders, before that
(the KaTeX label and architecture icon advisories fixed in 11.10.0), and #353 pins 11.4.1. So mermaid_ok parses
every mermaid body line by line and DROPS the item unless all of it is plain diagram text:
    the first line    one of MERMAID_KINDS, the diagrams with no published render-time flaw we know of; so
                      --- front matter, which must come first, is refused here
    nothing that      configures Mermaid (%%{init}%% directives), styles it (classDef, style, linkStyle,
                      url(, @import), makes it act (click, with its href, call and callback), links (a scheme),
                      or is HTML or becomes HTML (a tag, an &entity;, Mermaid's own #60; codes,
                      markdown strings in backticks, KaTeX's $$, icons)
    and no more than  MERMAID_LINES lines
A dropped item is not repaired, as everywhere here. The rule is the same for every agent's slot.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request

FORMAT = "proto-canvas/1"
PARTITION = "conf:canvas:%s:%s"
WINDOW_PREFIX = "gov:canvas:publish:"
MAX_MINUTES = 120
PUBLISH_SECONDS = 5
# A file whose items have not changed is rewritten this often anyway, so "generated" shows the publisher is
# alive. Otherwise a write happens only when something a viewer would see has changed.
KEEPALIVE_SECONDS = 60

# The agents whose partitions are read. Anything else under conf:canvas:<room>: is ignored, not published.
AGENTS = ("grok", "gemini", "claude-code-cli", "chatgpt-codex-desktop", "claude-mobile", "claude-api", "chair")
# THE CHAIR'S SLOT is held to the owner's terms (2026-10-20, CLAUDE-MOBILE-OWNER-CHAIR-CANVAS-20261020T2252Z):
# text and diagrams only, no images or links, short. The bridge's door refuses anything else before it is
# written (Blackboard cloud/redis-tool-bridge/op.py, canvas_refusal); this is the same rule again, here.
CHAIR = "chair"
CHAIR_TYPES = ("note", "mermaid")
CHAIR_BYTES = 512
# A relative Markdown link or image is a link with no scheme: inline (`[label](notes.md)`, `![alt](pic.png)`),
# reference-style (`[label][ref]`, `[label][]`), or a reference definition line (`[ref]: notes.md`).
_LINKISH = re.compile(
    r"(?im)://|\bwww\.|\b(?:javascript|data|mailto|vbscript):|\bhref\b|\bclick\s"
    r"|!\[[^\]\n]*\]"
    r"|\[[^\]\n]*\]\s*\("
    r"|\[[^\]\n]*\]\s*\["
    r"|^[ \t]*\[[^\]\n]+\]:")
# MERMAID (mermaid_ok). The diagram kinds a body may open with: sequenceDiagram (KaTeX labels), architecture
# (icon text), gantt and xychart (render loops) are left out on purpose, after their advisories.
MERMAID_KINDS = ("flowchart", "graph", "erDiagram", "classDiagram", "stateDiagram", "stateDiagram-v2",
                 "mindmap", "timeline", "pie")
MERMAID_LINES = 60
_MERMAID_KIND = re.compile(r"([A-Za-z][A-Za-z0-9-]*)(?:[ \t].*)?")
_MERMAID_REFUSED = re.compile(
    r"(?i)%%\{"                                             # an init directive: Mermaid's configuration
    r"|^\s*(?:classDef|style|linkStyle)\b|url\(|@import|expression\("        # CSS
    r"|\bclick\b|://|\b(?:javascript|vbscript):"           # what acts (click ... href, call), or links
    r"|<\s*[a-z/!?]"                                        # an HTML tag (an arrow is <-- <| <= <.)
    r"|&#?[a-z0-9]+;|#[a-z0-9]+;"                           # an entity, and Mermaid's own #60; codes
    r"|`|\$\$|::icon|\bicon\s*\(|\bfa:fa-",                 # markdown strings, KaTeX, icons
    re.MULTILINE)
# The same five types #353 draws (LIVE_TYPES in assets/proto-canvas.js).
TYPES = ("mermaid", "note", "image_url", "datamodel", "wireframe")
STATUSES = ("claimed", "working", "review", "done")
# image_url bodies: https only, and only on these hosts. A short list on purpose; a host is one reviewed line.
IMAGE_HOSTS = ("storage.googleapis.com", "sfdc24.com", "www.sfdc24.com")

ROOM = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
TITLE_MAX = 120
BODY_MAX = 1800
TS_MAX = 40
MAX_ITEMS = 200
# A browser's edge copy is at most this old. "public" lets Google's edge serve repeat polls, so many viewers
# do not each cost a read of the bucket (Gemini on v0.2: 5 s polling inflates Class B operations). Without an
# explicit header a public object is served with max-age=3600, and the page would see an hour-old canvas.
CACHE_CONTROL = "public, max-age=5"
# Tab, LF and CR pass here (a Mermaid body has lines); a title refuses all three, in clean_item.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Refused(Exception):
    """A canvas-publish request this worker will not act on. The message is published, so it is ours."""


class Aborted(Exception):
    """The upload's fence failed. Nothing new is public."""


def stamp(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def clean_item(agent: str, item_id: str, raw):
    """The published form of one stored item, or None when it must not be published.

    Dropped, never repaired: an item that is not public, not JSON, or carries a type, status, host or length
    outside the rules does not appear at all. Altering what an agent wrote and publishing the result would
    put words on the page that nobody wrote."""
    if agent not in AGENTS or not ITEM_ID.fullmatch(_text(item_id) or ""):
        return None
    try:
        item = json.loads(_text(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(item, dict) or item.get("public") is not True:
        return None
    if not (_whole(item.get("v")) and item["v"] == 1):
        return None
    if item.get("type") not in TYPES or item.get("status") not in STATUSES:
        return None
    version = item.get("version")
    if not (_whole(version) and 0 <= version < 10 ** 9):
        return None
    title, body, ts = item.get("title"), item.get("body"), item.get("ts", "")
    if not (isinstance(title, str) and isinstance(body, str) and isinstance(ts, str)):
        return None
    if len(title) > TITLE_MAX or len(body) > BODY_MAX or len(ts) > TS_MAX:
        return None
    if _CONTROL.search(title + ts + body) or any(ch in title + ts for ch in "\t\r\n"):
        return None
    if item["type"] == "image_url" and not image_ok(body):
        return None
    if item["type"] == "mermaid" and not mermaid_ok(body):
        return None
    if agent == CHAIR and (item["type"] not in CHAIR_TYPES or len(_text(raw).encode("utf-8")) > CHAIR_BYTES
                           or _LINKISH.search(title + "\n" + body)):
        return None
    return {"v": 1, "id": "%s/%s" % (agent, _text(item_id)), "type": item["type"], "title": title,
            "body": body, "status": item["status"], "version": version, "public": True, "ts": ts,
            "agent": agent}


def mermaid_ok(body: str) -> bool:
    """Plain diagram text of a kind on MERMAID_KINDS, and nothing that configures, styles, links, acts or is
    HTML (the module's MERMAID IS CHECKED HERE). False drops the item."""
    lines = [line for line in body.splitlines() if line.strip() and not line.strip().startswith("%%")]
    if not lines or len(body.splitlines()) > MERMAID_LINES:
        return False
    head = _MERMAID_KIND.fullmatch(lines[0].strip())
    if head is None or head.group(1) not in MERMAID_KINDS:
        return False
    return _MERMAID_REFUSED.search(body) is None


def image_ok(url: str) -> bool:
    """https, no credentials, no whitespace, and a host on IMAGE_HOSTS."""
    if url != url.strip() or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme == "https" and not parts.username and not parts.password
            and (parts.hostname or "") in IMAGE_HOSTS and parts.port in (None, 443))


def snapshot(conn, room: str, generated, until=None) -> dict:
    """The room file: every public item of every allowlisted agent, in a stable order."""
    items = []
    for agent in AGENTS:
        stored = conn.hgetall(PARTITION % (room, agent)) or {}
        for item_id in sorted(_text(k) for k in stored):
            raw = stored.get(item_id)
            if raw is None:
                raw = stored.get(item_id.encode("utf-8"))
            published = clean_item(agent, item_id, raw)
            if published:
                items.append(published)
    doc = {"format": FORMAT, "v": 1, "room": room, "generated": stamp(generated), "items": items[:MAX_ITEMS]}
    if until is not None:
        doc["until"] = stamp(until)
    return doc


def content_digest(doc) -> str:
    """What a viewer would see, without the clock: equal digests mean nothing needs writing."""
    seen = {k: v for k, v in doc.items() if k not in ("generated",)}
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------- windows

def window_key(room: str) -> str:
    return WINDOW_PREFIX + room


def open_window(conn, room, minutes, by: str, now, bucket: str) -> dict:
    """Open (minutes 1..120) or close (minutes 0) a room's publishing window. Raises Refused.

    ONE WINDOW PER ROOM, set with SET NX: a second request while one is open is refused, never doubled and
    never extended, so an ask cannot keep a room publishing past the time its opener chose."""
    room = (room or "").strip()
    if not ROOM.fullmatch(room):
        raise Refused("room= must be 2 to 40 characters of a-z, 0-9 and hyphens")
    text = str(minutes if minutes is not None else "").strip()
    if not text.isdigit() or int(text) > MAX_MINUTES:
        raise Refused("minutes= must be a whole number from 0 (stop) to %d" % MAX_MINUTES)
    count = int(text)
    if count == 0:
        existed = bool(conn.delete(window_key(room)))
        return {"ok": True, "room": room, "stopped": existed}
    if not BUCKET.fullmatch(bucket or ""):
        raise Refused("no public canvas bucket is configured on this worker (CANVAS_BUCKET); nothing opened")
    until = now + datetime.timedelta(minutes=count)
    # `gen` makes a stop and a reopen a different window even when `until` is unchanged, so a snapshot
    # taken under the old window cannot be written into the new one.
    value = json.dumps({"by": by, "gen": secrets.token_hex(8), "room": room, "until": stamp(until)},
                       sort_keys=True)
    if not conn.set(window_key(room), value, nx=True, ex=count * 60):
        held = read_window(conn, room)
        raise Refused("room %s is already publishing until %s; send minutes=0 to stop it first"
                      % (room, held["until"] if held else "an unreadable time"))
    return {"ok": True, "room": room, "until": stamp(until), "minutes": count,
            "file": "gs://%s/rooms/%s.json" % (bucket, room)}


def _until_of(raw, now):
    """The window's `until` if `raw` is a window still open at `now`, else None."""
    try:
        held = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not isinstance(held, dict) or not isinstance(held.get("until"), str):
        return None
    try:
        until = datetime.datetime.fromisoformat(held["until"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return until if until > now else None


def read_window(conn, room):
    try:
        held = json.loads(_text(conn.get(window_key(room))) or "null")
    except (TypeError, ValueError):
        return None
    return held if isinstance(held, dict) and isinstance(held.get("until"), str) else None


def active_windows(conn, now) -> list:
    """[(room, until)] for every window still open at `now`. Its TTL ends it in Redis; `until` ends it here
    even when the TTL has not fired yet."""
    out = []
    for key in conn.scan_iter(match=WINDOW_PREFIX + "*", count=100):
        room = _text(key)[len(WINDOW_PREFIX):]
        if not ROOM.fullmatch(room):
            continue
        held = read_window(conn, room)
        if not held:
            continue
        try:
            until = datetime.datetime.fromisoformat(held["until"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if until > now:
            out.append((room, until))
    return sorted(out)


def summarise(result: dict) -> str:
    if result.get("stopped") is not None:
        return ("OK. Publishing for room %s is stopped. %s The last file stays as it was."
                % (result["room"], "A window was open and is closed." if result["stopped"]
                   else "No window was open."))
    return ("OK. Room %s publishes every %d s until %s (%d min) to %s. Only items with public:true, from the "
            "allowlisted agents' partitions conf:canvas:%s:<agent>, with allowlisted fields, types and hosts. "
            "Nothing is published while no live worker is running."
            % (result["room"], PUBLISH_SECONDS, result["until"], result["minutes"], result["file"],
               result["room"]))


# ----------------------------------------------------------------------------------------------- upload

METADATA_TOKEN = ("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token")


class Token:
    """The job's own access token from the metadata server, kept until a minute before it expires."""

    def __init__(self, opener=urllib.request.urlopen, clock=time.time):
        self.opener, self.clock = opener, clock
        self.value, self.expires = "", 0.0

    def __call__(self) -> str:
        if self.value and self.clock() < self.expires - 60:
            return self.value
        req = urllib.request.Request(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
        with self.opener(req, timeout=5) as resp:
            got = json.loads(resp.read().decode("utf-8"))
        self.value = got["access_token"]
        self.expires = self.clock() + int(got.get("expires_in", 0))
        return self.value


def gcs_upload(bucket: str, name: str, data: bytes, token, opener=urllib.request.urlopen, allow=None) -> int:
    """One multipart upload, so the object's Cache-Control is set in the same call as its bytes.

    `allow`, when given, is asked again after the body is built AND after the token has been fetched,
    right before the request is sent. The token call can block for seconds (it is an HTTP round trip to
    the metadata server), and a stop, a deletion, a lost lease, an expiry or a privacy revocation landing
    during it must still abort: once the bytes are sent they are committed, and no later check can
    retract them (Aya on 07fa54d)."""
    boundary = "canvas" + secrets.token_hex(8)
    meta = json.dumps({"name": name, "contentType": "application/json", "cacheControl": CACHE_CONTROL})
    body = (("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n%s\r\n--%s\r\n"
             "Content-Type: application/json\r\n\r\n") % (boundary, meta, boundary)).encode("utf-8")
    body += data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    if allow is not None and not allow():
        raise Aborted()
    auth = "Bearer " + token()
    if allow is not None and not allow():
        raise Aborted()
    url = ("https://storage.googleapis.com/upload/storage/v1/b/%s/o?uploadType=multipart"
           % urllib.parse.quote(bucket, safe=""))
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": auth, "Content-Type": "multipart/related; boundary=" + boundary})
    with opener(req, timeout=10) as resp:
        return resp.status


# -------------------------------------------------------------------------------------------- publisher

class Publisher:
    """Publishes every open window. tick() is one round; run() is the loop the live worker starts.

    `holds()` says whether this worker still holds gov:worker:lock. NOTHING IS WRITTEN AFTER THE WINDOW OR THE
    LEASE ENDS, and a snapshot is not sent once its window or its bytes have changed (Aya on ed411d2: a captured
    upload must not commit after a stop, a deletion, a lost lease or an expiry, and a reopen or a privacy
    change must not publish the old bytes). The send asks again after it has been entered."""

    def __init__(self, conn, bucket, upload, log, clock=None, holds=None):
        self.conn, self.bucket, self.upload, self.log = conn, bucket, upload, log
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.holds = holds
        self.last = {}          # room -> (digest, time of the last write)
        self.stop = threading.Event()

    def may_write(self, room) -> bool:
        """The publisher is running, the lease is this worker's, and the room's window is open now."""
        if self.stop.is_set():
            return False
        try:
            if self.holds is None or not self.holds():
                self.log.warning("canvas: gov:worker:lock is not provably this worker's; nothing published")
                return False
            held = read_window(self.conn, room)
            until = datetime.datetime.fromisoformat(held["until"].replace("Z", "+00:00")) if held else None
        except Exception as error:                                       # noqa: BLE001 - unknown is no
            self.log.warning("canvas %s: the window or the lease could not be read (%s)", room, type(error).__name__)
            return False
        return until is not None and until > self.clock()

    def _window_raw(self, room) -> str:
        try:
            return _text(self.conn.get(window_key(room)) or "") or ""
        except Exception as error:                                       # noqa: BLE001
            self.log.warning("canvas %s: the window or the lease could not be read (%s)", room, type(error).__name__)
            return ""

    def _unchanged(self, room, raw: str, digest: str, opened) -> bool:
        """The captured window and the captured bytes are still what Redis holds."""
        if not self.may_write(room) or self._window_raw(room) != raw:
            return False
        try:
            fresh = snapshot(self.conn, room, self.clock(), opened)
        except Exception as error:                                       # noqa: BLE001
            self.log.warning("canvas %s: the window or the lease could not be read (%s)", room, type(error).__name__)
            return False
        return content_digest(fresh) == digest

    def _dispatch(self, name: str, data: bytes, allow) -> None:
        """Send `data` only while `allow` holds, including once the upload has been entered."""
        if not allow():
            raise Aborted()
        try:
            self.upload(self.bucket, name, data, allow)
        except TypeError as error:
            if "argument" not in str(error).lower() and "positional" not in str(error).lower():
                raise
            if not allow():
                raise Aborted()
            self.upload(self.bucket, name, data)
        if not allow():
            raise Aborted()

    def _publish(self, room) -> bool:
        """One room. A snapshot is sent only with the window and the bytes it was built from.

        Checked again inside the send, so a stop, a deletion, a lost lease, an expiry, a reopen or a
        privacy change after the last precheck does not commit the captured upload. When the bytes
        changed and the window is still open, the fresh bytes are what get sent."""
        now = self.clock()
        prior_doc = None
        try:
            raw = self._window_raw(room)
            opened = _until_of(raw, now)
            if opened is None:
                return False
            prior_doc = snapshot(self.conn, room, self.clock(), opened)
        except Exception as error:                                       # noqa: BLE001
            self.log.warning("canvas %s: the window or the lease could not be read (%s)", room, type(error).__name__)
            return False
        digest = content_digest(prior_doc)
        prior = self.last.get(room)
        if (prior and prior[0] == digest and (self.clock() - prior[1]).total_seconds() < KEEPALIVE_SECONDS
                and self._unchanged(room, raw, digest, opened)):
            return False
        for _ in range(2):
            raw = self._window_raw(room)
            opened = _until_of(raw, self.clock())
            if opened is None or not self.may_write(room):
                return False
            try:
                doc = snapshot(self.conn, room, self.clock(), opened)
            except Exception as error:                                   # noqa: BLE001
                self.log.warning("canvas %s: the window or the lease could not be read (%s)", room, type(error).__name__)
                return False
            digest = content_digest(doc)
            data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")

            def allow(raw=raw, digest=digest, opened=opened):
                return self._unchanged(room, raw, digest, opened)

            if not allow():
                continue
            try:
                self._dispatch("rooms/%s.json" % room, data, allow)
            except Aborted:
                continue
            except Exception as error:                                   # noqa: BLE001
                # The TYPE only: an HTTP error's text can quote the request. Retried on the next tick.
                self.log.warning("canvas %s: upload failed (%s); retried next tick", room, type(error).__name__)
                return False
            self.last[room] = (digest, self.clock())
            return True
        return False

    def tick(self) -> list:
        """Write the rooms whose content changed, or whose keepalive is due. Returns the rooms written."""
        now = self.clock()
        written = []
        for room, _until in active_windows(self.conn, now):
            if self._publish(room):
                written.append(room)
        return written

    def run(self, interval=PUBLISH_SECONDS):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:                                   # noqa: BLE001
                # A Redis flap costs one tick, never the meeting's canvas.
                self.log.warning("canvas tick failed (%s)", type(error).__name__)
            self.stop.wait(interval)


def start(conn, bucket, log, upload=None, holds=None):
    """The publisher on its own thread, or None when no bucket is configured.

    ITS OWN CLOCK, NOT THE LIVE LOOP'S. One pass of the request loop begins with a board read that can take
    minutes, so publishing between passes would stall the canvas for as long as the board does.

    `holds` is required: without a way to ask whether the lease is still this worker's, nothing starts."""
    if not BUCKET.fullmatch(bucket or "") or holds is None:
        return None
    if upload is None:
        token = Token()
        upload = lambda b, n, d, allow=None: gcs_upload(b, n, d, token, allow=allow)  # noqa: E731
    publisher = Publisher(conn, bucket, upload, log, holds=holds)
    thread = threading.Thread(target=publisher.run, name="canvas-publish", daemon=True)
    thread.start()
    publisher.thread = thread
    return publisher
