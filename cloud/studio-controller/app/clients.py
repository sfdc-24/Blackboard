"""Client workspaces: named clients who sign in on sfdc24.com and see their own
projects (only when STUDIO_CLIENT_WORKSPACES is on).

The registry is one state-store object, ``studio_clients``, beside the sessions:

    {"version": 1, "clients": [
        {"name": "Nav", "emails": ["client@example.com"],
         "projects": [{"id": "steelworkson", "name": "steelworkson.ca",
                       "url": "https://steelworkson.ca/"}]}]}

It is written by an operator script, never by the controller, which only reads
it (with a short cache). Addresses are compared exactly, lowercased, against the
address the sign-in code was verified for; no address is ever returned to a
page or written to a log. A malformed entry is skipped as a whole, never half
used.
"""
from __future__ import annotations

import copy
import hmac
import ipaddress
import re
import threading
import time
from urllib.parse import urlsplit

REGISTRY = "studio_clients"
CACHE_SECONDS = 30.0
MAX_CLIENTS = 200
MAX_PROJECTS = 20

_EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}$")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HOST_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


def _plain(value, cap: int) -> bool:
    return (isinstance(value, str) and value.strip() == value and 0 < len(value) <= cap
            and all(ch.isprintable() for ch in value) and "<" not in value and ">" not in value)


def project_url_ok(url) -> bool:
    """An https URL on a named host (no IP literal, no port, no credentials)."""
    if not isinstance(url, str) or len(url) > 500:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "")
    try:
        ipaddress.ip_address(host)
        return False                         # never an address, only a named site
    except ValueError:
        pass
    return (parts.scheme == "https" and parts.netloc == host and bool(_HOST_RE.fullmatch(host))
            and not parts.username and not parts.password and parts.port is None)


def _clean_client(raw) -> dict | None:
    if not isinstance(raw, dict) or set(raw) != {"name", "emails", "projects"}:
        return None
    name, emails, projects = raw["name"], raw["emails"], raw["projects"]
    if not _plain(name, 80) or not isinstance(emails, list) or not isinstance(projects, list):
        return None
    if not emails or len(emails) > 10 or len(projects) > MAX_PROJECTS:
        return None
    for email in emails:
        if not isinstance(email, str) or email != email.strip().lower() or not _EMAIL_RE.fullmatch(email):
            return None
    clean_projects, seen = [], set()
    for project in projects:
        if not isinstance(project, dict) or set(project) != {"id", "name", "url"}:
            return None
        if (not isinstance(project["id"], str) or not PROJECT_ID_RE.fullmatch(project["id"])
                or project["id"] in seen or not _plain(project["name"], 80)
                or not project_url_ok(project["url"])):
            return None
        seen.add(project["id"])
        clean_projects.append({"id": project["id"], "name": project["name"], "url": project["url"]})
    return {"name": name, "emails": list(emails), "projects": clean_projects}


class ClientRegistry:
    def __init__(self, store, clock=time.monotonic, cache_seconds: float = CACHE_SECONDS):
        self.store = store
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: list | None = None
        self._loaded_at = 0.0

    def clients(self) -> list:
        with self._lock:
            now = self.clock()
            if self._cached is not None and now - self._loaded_at < self.cache_seconds:
                return self._cached
        try:
            raw, _ = self.store.load(REGISTRY)
        except Exception:  # an unreadable registry admits nobody
            raw = {}
        clients = []
        entries = raw.get("clients") if isinstance(raw, dict) and raw.get("version") == 1 else []
        for entry in (entries if isinstance(entries, list) else [])[:MAX_CLIENTS]:
            clean = _clean_client(entry)
            if clean is not None:
                clients.append(clean)
        # One address belongs to one client; an address listed twice admits neither.
        counts: dict = {}
        for client in clients:
            for email in client["emails"]:
                counts[email] = counts.get(email, 0) + 1
        clients = [c for c in clients if all(counts[e] == 1 for e in c["emails"])]
        with self._lock:
            self._cached, self._loaded_at = clients, self.clock()
        return clients

    def emails(self) -> frozenset:
        return frozenset(email for client in self.clients() for email in client["emails"])

    def for_email(self, email) -> dict | None:
        if not isinstance(email, str):
            return None
        for client in self.clients():
            if email in client["emails"]:
                return copy.deepcopy(client)
        return None

    def for_subject(self, subject: str, subject_hash) -> dict | None:
        """The client whose verified address hashes to this sign-in subject."""
        if not subject:
            return None
        for client in self.clients():
            if any(hmac.compare_digest(subject_hash(email), subject) for email in client["emails"]):
                return copy.deepcopy(client)
        return None

    @staticmethod
    def project(client: dict | None, project_id) -> dict | None:
        if not client or not isinstance(project_id, str):
            return None
        for project in client["projects"]:
            if project["id"] == project_id:
                return dict(project)
        return None


def public_view(client: dict) -> dict:
    """What the page may see of a client: the name and the projects - never an address."""
    return {"name": client["name"],
            "projects": [{"id": p["id"], "name": p["name"], "url": p["url"]} for p in client["projects"]]}


__all__ = ["ClientRegistry", "REGISTRY", "project_url_ok", "public_view", "PROJECT_ID_RE"]
