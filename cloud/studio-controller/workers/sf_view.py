"""The LIVE ORG VIEW seam: what the conference screen should see of the scratch org.

Mr. Salam (2026-10-09): "may be use iframe to show what's built so the user has
to watch the conference screen as things are modeled and demonstrated".

Lightning refuses to be framed by another site, a framed Salesforce page has no
login under third-party cookie partitioning, and a session id or frontdoor sid
must never reach a browser. So the recommended path is a SERVER-SIDE MIRROR: a
separate render worker (headless Chromium, logged into the SCRATCH org on the
server) captures the page after each build/display/test step and the canvas
shows the picture. This module is the seam for it, not the browser:

    url = view_url(host, "record", "Agent_Spend__c", record_id)   # what to capture
    req = view_request(target, kind, label, url)                   # what the lane asks for
    evt = org_view_event(target, kind, label, image, at)           # what a capture becomes

`org.view` is a NEW optional event type ({target, kind, label, image, at}); the
site contract does not accept it yet (see the PR), so the lane hands requests to
an injected viewer and emits nothing on the session stream. Requests carry the
Lightning URL only - never a token, a sid or a frontdoor link; the render worker
holds its own login. At most one capture per session every MIN_INTERVAL seconds.
"""
from __future__ import annotations

import re
import threading
import time

KINDS = ("object", "record", "list")
TARGETS = ("scratch", "devorg")
MIN_INTERVAL = 3.0
API_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,42}")
RECORD_RE = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:develop|scratch)\.my\.salesforce\.com")
IMAGE_RE = re.compile(r"data:image/png;base64,[A-Za-z0-9+/=]{16,}|https://[^\s?#]+\?[A-Za-z0-9=&%._-]{1,400}")


def view_url(host: str, kind: str, api_name: str, record_id: str = "") -> str:
    """The Lightning page to capture for each kind. Pure; no token can be in it."""
    if not HOST_RE.fullmatch(host or ""):
        raise ValueError("not a developer or scratch org My Domain host")
    if kind not in KINDS or not API_RE.fullmatch(api_name or ""):
        raise ValueError("unknown view kind or object")
    base = "https://" + host[: -len(".my.salesforce.com")] + ".lightning.force.com"
    if kind == "object":
        return "%s/lightning/setup/ObjectManager/%s/Details/view" % (base, api_name)
    if kind == "list":
        return "%s/lightning/o/%s/list?filterName=All" % (base, api_name)
    if not RECORD_RE.fullmatch(record_id or ""):
        raise ValueError("a record view needs a record id")
    return "%s/lightning/r/%s/%s/view" % (base, api_name, record_id)


def view_request(target: str, kind: str, label: str, url: str) -> dict:
    if target not in TARGETS or kind not in KINDS:
        raise ValueError("unknown target or kind")
    if any(m.lower() in url.lower() for m in ("sid=", "frontdoor", "access_token")):
        raise ValueError("a view request never carries a session")
    return {"target": target, "kind": kind, "label": str(label)[:120], "url": url}


def org_view_event(target: str, kind: str, label: str, image: str, at: str) -> dict:
    """The optional `org.view` payload: a PNG data URI or a short-lived signed URL, never a session."""
    if target not in TARGETS or kind not in KINDS:
        raise ValueError("unknown target or kind")
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image) or "sid=" in image or "frontdoor" in image:
        raise ValueError("image must be a PNG data URI or a signed https URL without a session")
    return {"target": target, "kind": kind, "label": str(label)[:120], "image": image, "at": str(at)}


class NullViewer:
    """The default: nothing is captured (no render worker in this service)."""

    def request(self, session_id: str, req: dict) -> bool:
        return False


class ThrottledViewer:
    """Hands requests to `deliver(session_id, req)` at most once per MIN_INTERVAL per session."""

    def __init__(self, deliver, clock=time.monotonic, interval: float = MIN_INTERVAL):
        self.deliver, self.clock, self.interval = deliver, clock, interval
        self._last: dict = {}
        self._lock = threading.Lock()

    def request(self, session_id: str, req: dict) -> bool:
        with self._lock:
            now = self.clock()
            if now - self._last.get(session_id, -1e9) < self.interval:
                return False
            self._last[session_id] = now
        self.deliver(session_id, req)
        return True


__all__ = ["view_url", "view_request", "org_view_event", "NullViewer", "ThrottledViewer", "KINDS", "TARGETS",
           "MIN_INTERVAL"]
