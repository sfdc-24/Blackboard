"""Client workspaces: named clients who sign in on sfdc24.com and see their own
projects (only when STUDIO_CLIENT_WORKSPACES is on).

The registry is one state-store object, ``studio_clients``, beside the sessions:

    {"version": 1, "clients": [
        {"id": "nav", "name": "Nav", "emails": ["client@example.com"],
         "projects": [{"id": "steelworkson", "name": "steelworkson.ca",
                       "url": "https://www.steelworkson.ca/"}]}]}

``id`` is the tenant: it goes in the client token and in every client session,
and a workspace is always ``{tenant, project}``. The registry is written by an
operator script, never by the controller, which only reads it. Sign-in address
lists may be cached briefly; every authorisation decision (``member``) reads it
fresh, so a client removed from the registry is refused on their next request.
Addresses are compared exactly, lowercased, against the address the sign-in
code was verified for; no address is ever returned to a page or logged. A
malformed entry is skipped as a whole, never half used.
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
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PROJECT_ID_RE = TENANT_RE
_HOST_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
# Names that are never a public site, refused before any DNS lookup (a belt on
# top of the resolver check in app/project_page.py).
_PRIVATE_SUFFIXES = (".internal", ".local", ".localdomain", ".localhost", ".home.arpa", ".arpa", ".lan",
                     ".intranet", ".invalid", ".onion")


def _plain(value, cap: int) -> bool:
    return (isinstance(value, str) and value.strip() == value and 0 < len(value) <= cap
            and all(ch.isprintable() for ch in value) and "<" not in value and ">" not in value)


def project_url_ok(url) -> bool:
    """A reviewed canonical page: https, a lowercase named host with a letter
    TLD, the default port, and a plain path - no IP literal, userinfo, port,
    query, fragment, percent-encoding, backslash or trailing dot."""
    if not isinstance(url, str) or not 0 < len(url) <= 300 or not url.isascii():
        return False
    if any(ch.isspace() or ch in "\\%@#?" or not ch.isprintable() for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        return False                         # never an address, only a named site
    except ValueError:
        pass
    if host.endswith(_PRIVATE_SUFFIXES) or host.split(".")[0] in ("localhost", "metadata"):
        return False
    return (parts.scheme == "https" and parts.netloc == host and bool(_HOST_RE.fullmatch(host))
            and port is None and not parts.username and not parts.password
            and not parts.query and not parts.fragment and bool(_PATH_RE.fullmatch(parts.path or "/")))


def _clean_client(raw) -> dict | None:
    if not isinstance(raw, dict) or set(raw) != {"id", "name", "emails", "projects"}:
        return None
    tenant, name, emails, projects = raw["id"], raw["name"], raw["emails"], raw["projects"]
    if not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant):
        return None
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
    return {"id": tenant, "name": name, "emails": list(emails), "projects": clean_projects}


class ClientRegistry:
    def __init__(self, store, clock=time.monotonic, cache_seconds: float = CACHE_SECONDS):
        self.store = store
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: list | None = None
        self._loaded_at = 0.0

    def _read(self) -> list:
        try:
            raw, _ = self.store.load(REGISTRY)
        except Exception:                    # an unreadable registry admits nobody
            return []
        clients = []
        entries = raw.get("clients") if isinstance(raw, dict) and raw.get("version") == 1 else []
        for entry in (entries if isinstance(entries, list) else [])[:MAX_CLIENTS]:
            clean = _clean_client(entry)
            if clean is not None:
                clients.append(clean)
        # One address, and one tenant id, belong to one client; a repeat admits neither.
        emails: dict = {}
        tenants: dict = {}
        for client in clients:
            tenants[client["id"]] = tenants.get(client["id"], 0) + 1
            for email in client["emails"]:
                emails[email] = emails.get(email, 0) + 1
        return [c for c in clients
                if tenants[c["id"]] == 1 and all(emails[e] == 1 for e in c["emails"])]

    def clients(self, fresh: bool = False) -> list:
        with self._lock:
            now = self.clock()
            if not fresh and self._cached is not None and now - self._loaded_at < self.cache_seconds:
                return self._cached
        clients = self._read()
        with self._lock:
            self._cached, self._loaded_at = clients, self.clock()
        return clients

    def emails(self) -> frozenset:
        """Addresses that may be sent a sign-in code (cached briefly)."""
        return frozenset(email for client in self.clients() for email in client["emails"])

    def for_subject(self, subject: str, subject_hash) -> dict | None:
        """The client whose verified address hashes to this sign-in subject (fresh)."""
        if not subject:
            return None
        for client in self.clients(fresh=True):
            if any(hmac.compare_digest(subject_hash(email), subject) for email in client["emails"]):
                return copy.deepcopy(client)
        return None

    def member(self, tenant: str, subject: str, subject_hash) -> dict | None:
        """The tenant's entry, read fresh, if it still exists and still lists an
        address whose hash is this subject; otherwise None. Every client-scope
        request is authorised by this, never by the token alone."""
        if not tenant or not subject:
            return None
        for client in self.clients(fresh=True):
            if client["id"] == tenant:
                if any(hmac.compare_digest(subject_hash(email), subject) for email in client["emails"]):
                    return copy.deepcopy(client)
                return None
        return None

    @staticmethod
    def project(client: dict | None, project_id) -> dict | None:
        if not client or not isinstance(project_id, str):
            return None
        for project in client["projects"]:
            if project["id"] == project_id:
                return dict(project)
        return None


def public_view(client: dict, allowed=None) -> dict:
    """What the page may see of a client: the name and the projects - never an
    address. ``allowed`` (a client token's project ids) keeps the view to the
    projects the token was issued for, even if more were added since."""
    return {"name": client["name"],
            "projects": [{"id": p["id"], "name": p["name"], "url": p["url"]} for p in client["projects"]
                         if allowed is None or p["id"] in allowed]}


__all__ = ["ClientRegistry", "REGISTRY", "TENANT_RE", "PROJECT_ID_RE", "project_url_ok", "public_view"]
