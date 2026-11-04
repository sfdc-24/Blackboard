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

A SCOPED WINDOW publishes named items only (Aya's source hold on d2d1618: a window the call bridge opened by
itself published every public:true item already sitting in the room's partitions). A window opened with a scope
carries "scoped": true in its own value, fixed at open, and its items are listed in gov:canvas:scope:<room>:<gen>
(under gov:, no agent may write it; it lives as long as the window can). While it is open only these leave
Redis: an item whose published id "<agent>/<field>" is listed, and that same agent's item whose "ask" value is
the listed field (the agent's own card for that ask). Nothing older and nothing else, whatever its public flag.
A scoped window with no list publishes nothing. A window opened by a board ask (do=canvas-publish) is not
scoped, as before: its opener chose to publish the room.
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
SCOPE_PREFIX = "gov:canvas:scope:"
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
# A relative Markdown link or image (`[label](notes.md)`, `![alt](pic.png)`) is a link with no scheme.
_LINKISH = re.compile(
    r"(?i)://|\bwww\.|\b(?:javascript|data|mailto|vbscript):|\bhref\b|\bclick\s"
    r"|!\[[^\]\n]*\]"
    r"|\[[^\]\n]*\]\s*\(")
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
TYPES = ("mermaid", "note", "image_url", "datamodel", "wireframe", "board")
# BOARD CARDS (conference docs/CARDS-ARCHITECTURE.md, decision a, Gemini 2026-11-04): one item made of typed
# blocks, each with its author, so several agents' work (four logos side by side, a pick, build-on stages, a
# discussion's synthesis) is ONE card. The body is JSON:
#   {"layout": "stack" | "grid", "chosen": <block index> | null,
#    "blocks": [{"kind": BLOCK_KINDS, "by": <agent tag>, "title": "...", "body": "..."}]}
# Every block is checked with its kind's own rule; the item is dropped whole if any block fails (never repaired).
BLOCK_KINDS = ("text", "mermaid", "svg", "wireframe", "slide", "datamodel", "image_url")
BOARD_BODY_MAX = 16000
BLOCK_BODY_MAX = 6000
BLOCK_TITLE_MAX = 80
BLOCKS_MAX = 8
# A card's stable short name, said aloud ("the logo card"; decision c): plain words only.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-]{0,39}$")
# SVG, checked server-side BEFORE the page's own safeSvg: shapes, text, gradients and clips only. No script,
# style element, foreignObject, link, image or use (nothing that runs, styles globally or reaches outside).
SVG_TAGS = frozenset(("svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text",
                      "tspan", "title", "desc", "defs", "lineargradient", "radialgradient", "stop", "clippath",
                      "mask", "pattern", "symbol", "marker"))
_SVG_URL = re.compile(r"(?i)url\(\s*(?!#)|javascript:|vbscript:|data:|expression\(|@import|://")
_SVG_DECL = re.compile(r"(?i)<!(?:doctype|entity|\[cdata)|<\?(?!xml\s)")
STATUSES = ("claimed", "working", "review", "done")
# image_url bodies: https only, and only on these hosts. A short list on purpose; a host is one reviewed line.
IMAGE_HOSTS = ("storage.googleapis.com", "sfdc24.com", "www.sfdc24.com")

ROOM = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
TITLE_MAX = 120
BODY_MAX = 1800
TS_MAX = 40
ACTIVITY_MAX = 80
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


_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")     # projects.PROJECT_ID


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
    body_max = BOARD_BODY_MAX if item["type"] == "board" else BODY_MAX
    if len(title) > TITLE_MAX or len(body) > body_max or len(ts) > TS_MAX:
        return None
    if _CONTROL.search(title + ts + body) or any(ch in title + ts for ch in "\t\r\n"):
        return None
    activity = item.get("activity")
    if activity is not None and not activity_ok(activity):
        return None
    if item["type"] == "image_url" and not image_ok(body):
        return None
    # A data model is drawn by the page with Mermaid too, so it is checked as Mermaid (Codex's review of PR 462).
    if item["type"] in ("mermaid", "datamodel") and not mermaid_ok(body):
        return None
    if item["type"] == "board" and not board_ok(body):
        return None
    name = item.get("name")
    if name is not None and not (isinstance(name, str) and NAME.fullmatch(name)):
        return None
    if agent == CHAIR and (item["type"] not in CHAIR_TYPES or len(_text(raw).encode("utf-8")) > CHAIR_BYTES
                           or _LINKISH.search(title + "\n" + body + "\n" + (activity or ""))):
        return None
    out = {"v": 1, "id": "%s/%s" % (agent, _text(item_id)), "type": item["type"], "title": title,
           "body": body, "status": item["status"], "version": version, "public": True, "ts": ts,
           "agent": agent}
    if activity:
        out["activity"] = activity
    if name:
        out["name"] = name
    project = item.get("project")
    if project is not None:
        # R6a: the project the card was made in (projects.PROJECT_ID). A malformed tag drops the item, like any field.
        if not (isinstance(project, str) and _PROJECT.fullmatch(project)):
            return None
        out["project"] = project
    return out


def activity_ok(activity) -> bool:
    """The optional activity phrase (GROK-CANVAS-RULE-ACTIVITY-20261024T0405Z): a short line of plain words for the
    page's activity strip. Text, at most ACTIVITY_MAX characters, one line, no control character and no pipe."""
    return (isinstance(activity, str) and len(activity) <= ACTIVITY_MAX and not _CONTROL.search(activity)
            and not any(ch in activity for ch in "\t\r\n|"))


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


def svg_ok(svg: str) -> bool:
    """One <svg> document of SVG_TAGS only: no event attribute, no link or href except a #fragment, no url()
    except url(#...), no DOCTYPE, entity or processing instruction. False drops the whole card."""
    import xml.etree.ElementTree as ET
    if not isinstance(svg, str) or not svg.strip().startswith(("<svg", "<?xml")) or _SVG_DECL.search(svg):
        return False
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return False

    def local(name):
        return name.rsplit("}", 1)[-1].lower()
    if local(root.tag) != "svg":
        return False
    for element in root.iter():
        if not isinstance(element.tag, str) or local(element.tag) not in SVG_TAGS:
            return False
        for attr, value in element.attrib.items():
            name = local(attr)
            if name.startswith("on") or name in ("style",) and _SVG_URL.search(value):
                return False
            if name == "href" and not value.startswith("#"):
                return False
            if _SVG_URL.search(value):
                return False
    return True


def html_ok(html: str) -> bool:
    """A wireframe or slide block: markup the page draws in a sandbox="" iframe through safeHtml. Refused here
    too: script, an event attribute, a URL scheme that runs, or a nested frame or object."""
    return (isinstance(html, str) and re.search(
        r"(?i)<\s*(?:script|iframe|object|embed|frame|base|meta|link)\b|\son[a-z]+\s*=|javascript:|vbscript:|"
        r"data:\s*text/html|expression\(", html) is None)


def board_ok(body: str) -> bool:
    """A board card's JSON body: the layout, an optional chosen block, and 1..BLOCKS_MAX blocks, each valid for its
    kind (BLOCK_KINDS), authored by an allowlisted agent."""
    try:
        board = json.loads(body)
    except (TypeError, ValueError):
        return False
    if not isinstance(board, dict) or set(board) - {"layout", "chosen", "blocks"}:
        return False
    blocks = board.get("blocks")
    if board.get("layout", "stack") not in ("stack", "grid") or not isinstance(blocks, list) \
            or not 1 <= len(blocks) <= BLOCKS_MAX:
        return False
    chosen = board.get("chosen")
    if chosen is not None and not (_whole(chosen) and 0 <= chosen < len(blocks)):
        return False
    for block in blocks:
        if not isinstance(block, dict) or set(block) - {"kind", "by", "title", "body"}:
            return False
        kind, by, title, text = block.get("kind"), block.get("by"), block.get("title", ""), block.get("body")
        if kind not in BLOCK_KINDS or by not in AGENTS or by == CHAIR:
            return False
        if not (isinstance(title, str) and isinstance(text, str)) or len(title) > BLOCK_TITLE_MAX \
                or len(text) > BLOCK_BODY_MAX or not text.strip():
            return False
        if _CONTROL.search(title + text) or any(ch in title for ch in "\t\r\n"):
            return False
        if kind in ("mermaid", "datamodel") and not mermaid_ok(text):
            return False
        if kind == "image_url" and not image_ok(text):
            return False
        if kind == "svg" and not svg_ok(text):
            return False
        if kind in ("wireframe", "slide") and not html_ok(text):
            return False
    return True


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


def snapshot(conn, room: str, generated, until=None, scope=None) -> dict:
    """The room file: every public item of every allowlisted agent, in a stable order. `scope` (a scoped
    window's list, scope_of): only the listed items and the cards bound to them (module docstring)."""
    import projects  # local import: the chair vendors this module without projects.py
    current = projects.current(conn, room)
    items = []
    for agent in AGENTS:
        stored = conn.hgetall(PARTITION % (room, agent)) or {}
        for item_id in sorted(_text(k) for k in stored):
            raw = stored.get(item_id)
            if raw is None:
                raw = stored.get(item_id.encode("utf-8"))
            published = clean_item(agent, item_id, raw)
            import call_lifecycle  # local import avoids publisher/lifecycle import cycle
            if (published and call_lifecycle.visible(conn, agent, item_id, raw, room=room, now=generated)
                    and (scope is None or in_scope(scope, agent, item_id, raw))
                    and published.get("project", "") == (current[0] if current else "")):
                # R6a (CARDS-ARCHITECTURE R6.3): only the current project's cards; untagged cards (made before
                # projects, or with none current) only while no project is current.
                items.append(published)
    doc = {"format": FORMAT, "v": 1, "room": room, "generated": stamp(generated), "items": items[:MAX_ITEMS]}
    if current:
        doc["project"] = {"id": current[0], "name": current[1]}
    if until is not None:
        doc["until"] = stamp(until)
    return doc


def in_scope(scope, agent: str, item_id: str, raw) -> bool:
    """Listed by id, or that agent's own card for a listed field (its "ask" value)."""
    if "%s/%s" % (agent, _text(item_id)) in scope:
        return True
    try:
        bound = json.loads(_text(raw)).get("ask")
    except (TypeError, ValueError, AttributeError):
        return False
    return isinstance(bound, str) and bool(bound) and "%s/%s" % (agent, bound) in scope


def scope_key(room: str, gen: str) -> str:
    return SCOPE_PREFIX + "%s:%s" % (room, gen)


def scope_of(conn, room: str, raw):
    """None for a window that publishes the room (opened by a board ask), else the frozenset of item ids a scoped
    window may publish. Fails closed: a scoped window whose list cannot be read publishes nothing."""
    try:
        held = json.loads(_text(raw)) if raw else None
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(held, dict) or "scoped" not in held:
        return None
    if held.get("scoped") is not True or not isinstance(held.get("gen"), str):
        return frozenset()
    return frozenset(_text(k) for k in (conn.hgetall(scope_key(room, held["gen"])) or {}))


def scope_add(conn, room: str, item: str, by: str, now) -> bool:
    """List one more item on a SCOPED window that `by` opened. Never on anyone else's window and never on an
    unscoped one: True only when it is listed."""
    agent, _, field = (item or "").partition("/")
    if agent not in AGENTS or not ITEM_ID.fullmatch(field):
        return False
    held = read_window(conn, room)
    if not held or held.get("scoped") is not True or held.get("by") != by or not isinstance(held.get("gen"), str):
        return False
    key = scope_key(room, held["gen"])
    conn.hset(key, item, stamp(now))
    conn.expire(key, MAX_MINUTES * 60)
    return True


def content_digest(doc) -> str:
    """What a viewer would see, without the clock: equal digests mean nothing needs writing."""
    seen = {k: v for k, v in doc.items() if k not in ("generated",)}
    return hashlib.sha256(json.dumps(seen, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------- windows

def window_key(room: str) -> str:
    return WINDOW_PREFIX + room


def open_window(conn, room, minutes, by: str, now, bucket: str, scope=None) -> dict:
    """Open (minutes 1..120) or close (minutes 0) a room's publishing window. Raises Refused.

    ONE WINDOW PER ROOM, set with SET NX: a second request while one is open is refused, never doubled and
    never extended, so an ask cannot keep a room publishing past the time its opener chose.

    `scope` (a list of "<agent>/<field>" ids): a SCOPED window, which publishes only those (module docstring)."""
    room = (room or "").strip()
    if not ROOM.fullmatch(room):
        raise Refused("room= must be 2 to 40 characters of a-z, 0-9 and hyphens")
    text = str(minutes if minutes is not None else "").strip()
    if not text.isdigit() or int(text) > MAX_MINUTES:
        raise Refused("minutes= must be a whole number from 0 (stop) to %d" % MAX_MINUTES)
    count = int(text)
    if count == 0:
        existed = bool(conn.delete(window_key(room)))
        if room == CALL_ROOM:
            # A STOP HOLDS FOR THE CALL (Aya on 0fde974: the next scan of the same call reopened it). The marker
            # is bound to the call live when it is next looked at, and kept until that call is over.
            conn.set(CALL_STOP_KEY, json.dumps({"by": by, "at": stamp(now)}, sort_keys=True), ex=CALL_STOP_SECONDS)
        return {"ok": True, "room": room, "stopped": existed}
    if not BUCKET.fullmatch(bucket or ""):
        raise Refused("no public canvas bucket is configured on this worker (CANVAS_BUCKET); nothing opened")
    until = now + datetime.timedelta(minutes=count)
    # `gen` makes a stop and a reopen a different window even when `until` is unchanged, so a snapshot
    # taken under the old window cannot be written into the new one.
    gen = secrets.token_hex(8)
    held = {"by": by, "gen": gen, "room": room, "until": stamp(until)}
    if scope is not None:
        held["scoped"] = True
    value = json.dumps(held, sort_keys=True)
    if not conn.set(window_key(room), value, nx=True, ex=count * 60):
        held = read_window(conn, room)
        raise Refused("room %s is already publishing until %s; send minutes=0 to stop it first"
                      % (room, held["until"] if held else "an unreadable time"))
    for item in scope or ():
        scope_add(conn, room, item, by, now)
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

    `allow`, when given, is asked again after the body is built and before the request is sent. An upload
    that has been entered still sends nothing once the fence has failed."""
    boundary = "canvas" + secrets.token_hex(8)
    meta = json.dumps({"name": name, "contentType": "application/json", "cacheControl": CACHE_CONTROL})
    body = (("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n%s\r\n--%s\r\n"
             "Content-Type: application/json\r\n\r\n") % (boundary, meta, boundary)).encode("utf-8")
    body += data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    if allow is not None and not allow():
        raise Aborted()
    url = ("https://storage.googleapis.com/upload/storage/v1/b/%s/o?uploadType=multipart"
           % urllib.parse.quote(bucket, safe=""))
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + token(), "Content-Type": "multipart/related; boundary=" + boundary})
    with opener(req, timeout=10) as resp:
        return resp.status


# ------------------------------------------------------------------------------------------- his calls

# THE OWNER'S CALLS KEEP THE PAGE'S ROOM PUBLISHING. The owner, directly to claude-code-cli, 2026-10-29 ~21:17Z:
# "yes, keep the publish window open during my calls", then "yes, move it to the cloud". So while one of his
# calls is under way, this worker opens CALL_ROOM itself, CALL_MINUTES at a time, and closes the window it
# opened once no call is. Nothing else changes: the same window key, the same filter, the same file.
#
# WHAT SAYS A CALL IS UNDER WAY is the chair's own record, v1:conf:<room>:state (conference
# chair/conf_chair/redis_state.py): `updated_at` on every write, `closed`="1" once the chair closes the call.
# Only the chair writes v1:conf: - redis_gov refuses that namespace to every agent - so an agent cannot fake a
# call to open the page. A record not written for CALL_IDLE_SECONDS counts as over, so a chair that died
# without closing does not keep the page publishing.
#
# WHOSE CALLS: rooms of the family in CANVAS_CALL_FAMILY ("conf-<16 hex>", the chair's
# CONF_REDIS_CHECK_OWNER), set on the job, never committed: this repository is public. Unset, this is off.
#
# NEVER ANOTHER OPENER'S WINDOW. A window someone asked for is left as it is, open or closed; the rule only
# opens when none is open (SET NX, as for an ask) and only closes one whose "by" is CALL_BY, compare-and-delete.
CALL_ROOM = "workshop"           # the room the conference page shows (the chair's CONF_CANVAS_URL)
CALL_MINUTES = 15                # reopened while the call lasts; lapses on its own if this worker stops
CALL_IDLE_SECONDS = 600
CALL_SCAN_SECONDS = 15
CALL_BY = "owner-call"
# A stop of CALL_ROOM (minutes=0, anyone's) holds until the call it was made in is over.
CALL_STOP_KEY = "gov:canvas:call-stop:" + CALL_ROOM
CALL_STOP_SECONDS = 4 * 60 * 60
# The live worker's lease (cloud/bus-reconciler/serve_requests.py WORKER_LOCK_KEY): WATCHed by every write here.
LEASE_KEY = "gov:worker:lock"
CALL_FAMILY = re.compile(r"^conf-[0-9a-f]{16}$")
STATE_PREFIX = "v1:conf:"
STATE_SUFFIX = ":state"


def call_family(environ) -> str:
    """CANVAS_CALL_FAMILY when it is a room family, else "" (off)."""
    value = str((environ or {}).get("CANVAS_CALL_FAMILY", "") or "").strip()
    return value if CALL_FAMILY.fullmatch(value) else ""


def live_call(conn, family: str, now) -> str:
    """The most recently written of his rooms whose chair record says a call is under way, or ""."""
    if not CALL_FAMILY.fullmatch(family or ""):
        return ""
    room_rule = re.compile(r"^%s-[0-9a-f]{8}$" % re.escape(family))
    best = (None, "")
    for key in conn.scan_iter(match="%s%s-*%s" % (STATE_PREFIX, family, STATE_SUFFIX), count=500):
        name = _text(key)
        room = name[len(STATE_PREFIX):-len(STATE_SUFFIX)]
        if not room_rule.fullmatch(room):
            continue
        state = conn.hgetall(name) or {}
        state = {_text(k): _text(v) for k, v in state.items()}
        if state.get("closed") == "1":
            continue
        try:
            at = int(state.get("updated_at", ""))
        except ValueError:
            continue
        age = now.timestamp() - at
        if 0 <= age <= CALL_IDLE_SECONDS and (best[0] is None or at > best[0]):
            best = (at, room)
    return best[1]


def keep_call_window(conn, family: str, now, bucket: str, fence) -> str:
    """One round: "opened", "open", "closed", "stopped" or "idle". Raises only what Redis raises.

    `fence()` says this worker may still write governance state: the publisher is not stopped and the lease
    is provably its own. Every write below is made in MULTI with gov:worker:lock WATCHed after fence() said
    yes, so a lease that moves before EXEC writes nothing (Aya on 0fde974: a lease lost during the scan, and
    a stopped publisher, still opened the window)."""
    live = live_call(conn, family, now)
    seen = conn.get(CALL_STOP_KEY)
    stop = _json_of(seen)
    if stop is not None:
        # THE MARKER IS CHANGED ONLY AS IT WAS READ (Aya on 73c049f: a newer stop landing between this read and
        # the WATCH was deleted, and the same round opened). Under the WATCH it must still be `seen`; a newer
        # one is kept as it is, and this round neither opens nor closes.
        def unchanged(pipe):
            return pipe.get(CALL_STOP_KEY) == seen
        if not live or (stop.get("call") and stop["call"] != live):
            spent = _fenced(conn, fence, (CALL_STOP_KEY,), lambda pipe: (
                [("delete", CALL_STOP_KEY)] if unchanged(pipe) else None))
            if not spent:
                return "stopped" if live else "idle"
            stop = None                      # removed, confirmed: the old stop is spent
        elif not stop.get("call"):
            bound = json.dumps(dict(stop, call=live), sort_keys=True)
            _fenced(conn, fence, (CALL_STOP_KEY,), lambda pipe: (
                [("set", CALL_STOP_KEY, bound, CALL_STOP_SECONDS, False)] if unchanged(pipe) else None))
    if live and stop is not None:
        _drop_ours(conn, fence)              # never ours to keep open past someone's stop
        return "stopped"
    held = read_window(conn, CALL_ROOM)
    is_open = _until_of(json.dumps(held), now) is not None if held else False
    if live:
        if is_open:
            return "open"
        if held is not None:                 # an expired value the TTL has not removed yet: ours, or nobody's
            _drop_ours(conn, fence)
        if not BUCKET.fullmatch(bucket or ""):
            return "idle"
        until = now + datetime.timedelta(minutes=CALL_MINUTES)
        value = json.dumps({"by": CALL_BY, "gen": secrets.token_hex(8), "room": CALL_ROOM, "until": stamp(until)},
                           sort_keys=True)
        done = _fenced(conn, fence, (window_key(CALL_ROOM), CALL_STOP_KEY), lambda pipe: (
            None if pipe.get(CALL_STOP_KEY) else
            [("set", window_key(CALL_ROOM), value, CALL_MINUTES * 60, True)]))
        if done is None:
            return "idle"                    # not ours to write now: no lease, stopped, raced or a stop landed
        return "opened" if done and done[0] else "open"
    if held is not None:                     # closed only if it is still the one this rule opened
        return "closed" if _drop_ours(conn, fence) else "idle"
    return "idle"


def _json_of(raw):
    try:
        value = json.loads(_text(raw)) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _drop_ours(conn, fence) -> bool:
    """Delete the window only while it is still the one this rule opened, and only while fenced."""
    key = window_key(CALL_ROOM)
    done = _fenced(conn, fence, (key,), lambda pipe: (
        [("delete", key)] if (_json_of(pipe.get(key)) or {}).get("by") == CALL_BY else None))
    return bool(done)


def _fenced(conn, fence, keys, plan):
    """Run `plan(pipe)`'s writes in one MULTI while the lease, `keys` and fence() all hold. The results, or None.

    WATCH first (the lease and `keys`), then `plan` (reads only, through the pipe; returns the writes, or None
    to write nothing), then fence(), then EXEC: a lease or key that moved after the WATCH fails the EXEC
    (WatchError), and nothing is written."""
    with conn.pipeline() as pipe:
        try:
            pipe.watch(LEASE_KEY, *keys)
            writes = plan(pipe)
            if not writes or not fence():
                pipe.unwatch()
                return None
            pipe.multi()
            for op in writes:
                if op[0] == "set":
                    _, key, value, ex, nx = op
                    pipe.set(key, value, ex=ex, nx=nx)
                else:
                    pipe.delete(op[1])
            return pipe.execute()
        except WatchError:
            return None


try:                                          # redis-py's, where it is installed; a stand-in for the tests
    from redis.exceptions import WatchError
except ImportError:                           # pragma: no cover - the image installs redis
    class WatchError(Exception):
        pass


# -------------------------------------------------------------------------------------------- publisher

class Publisher:
    """Publishes every open window. tick() is one round; run() is the loop the live worker starts.

    `holds()` says whether this worker still holds gov:worker:lock. NOTHING IS WRITTEN AFTER THE WINDOW OR THE
    LEASE ENDS, and a snapshot is not sent once its window or its bytes have changed (Aya on ed411d2: a captured
    upload must not commit after a stop, a deletion, a lost lease or an expiry, and a reopen or a privacy
    change must not publish the old bytes). The send asks again after it has been entered."""

    def __init__(self, conn, bucket, upload, log, clock=None, holds=None, family=""):
        self.conn, self.bucket, self.upload, self.log = conn, bucket, upload, log
        self.clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.holds = holds
        self.last = {}          # room -> (digest, time of the last write)
        self.stop = threading.Event()
        self.family = family if CALL_FAMILY.fullmatch(family or "") else ""
        self.call_at = None     # when his calls were last looked at
        self.call_said = None   # the last round's answer, so the log says each change once

    def may_govern(self) -> bool:
        """This worker may still write a window: not stopped, and the lease provably its own."""
        return not self.stop.is_set() and self.holds is not None and bool(self.holds())

    def calls(self) -> str:
        """His calls' round, every CALL_SCAN_SECONDS, only while this worker provably holds the lease."""
        now = self.clock()
        if not self.family or (self.call_at and (now - self.call_at).total_seconds() < CALL_SCAN_SECONDS):
            return ""
        self.call_at = now
        try:
            if self.holds is None or not self.holds():
                return ""
            said = keep_call_window(self.conn, self.family, now, self.bucket, self.may_govern)
        except Exception as error:                                       # noqa: BLE001 - the TYPE only
            self.log.warning("canvas %s: his calls could not be read (%s)", CALL_ROOM, type(error).__name__)
            return ""
        if said in ("opened", "closed") or said != self.call_said:
            self.log.info("canvas %s: his call window %s", CALL_ROOM, said)
        self.call_said = said
        return said

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
            fresh = snapshot(self.conn, room, self.clock(), opened, scope_of(self.conn, room, raw))
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
            prior_doc = snapshot(self.conn, room, self.clock(), opened, scope_of(self.conn, room, raw))
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
                doc = snapshot(self.conn, room, self.clock(), opened, scope_of(self.conn, room, raw))
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
        self.calls()
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


def start(conn, bucket, log, upload=None, holds=None, family=None):
    """The publisher on its own thread, or None when no bucket is configured.

    ITS OWN CLOCK, NOT THE LIVE LOOP'S. One pass of the request loop begins with a board read that can take
    minutes, so publishing between passes would stall the canvas for as long as the board does.

    `holds` is required: without a way to ask whether the lease is still this worker's, nothing starts.
    `family` defaults to CANVAS_CALL_FAMILY from the environment (his calls' window; "" is off)."""
    if not BUCKET.fullmatch(bucket or "") or holds is None:
        return None
    if upload is None:
        token = Token()
        upload = lambda b, n, d, allow=None: gcs_upload(b, n, d, token, allow=allow)  # noqa: E731
    if family is None:
        import os                                                        # noqa: PLC0415
        family = call_family(os.environ)
    publisher = Publisher(conn, bucket, upload, log, holds=holds, family=family)
    thread = threading.Thread(target=publisher.run, name="canvas-publish", daemon=True)
    thread.start()
    publisher.thread = thread
    return publisher
