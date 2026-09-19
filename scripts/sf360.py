#!/usr/bin/env python3
"""Live read-only rail into the SFDC24 Developer Edition org (00Dbm00000wK2ibEAC).

Why this exists, and why it is not JWT
--------------------------------------
The `omnistudio-jwt` rail in `sf org list` is dead: the certificate-backed
connected app is no longer installed in this org, so `sf org login jwt` fails
with "External client app is not installed in this org". Retrying the CLI will
never fix that; the app has to be recreated in the org UI.

It does not need to be. The same org already answers an OAuth 2.0
**client credentials** grant, whose credentials live in the gitignored `.env`
as the `Headless_*` block. That grant is strictly better for a dashboard: no
certificate to rotate, no browser, no user session to expire overnight -- the
failure that killed the Headless 360 Playground twice in one evening
(`docs/SERVICENOW_SETUP.md`).

Why there is a server in here at all
------------------------------------
www.sfdc24.com serves from GitHub Pages, which is static. A static page cannot
hold a client secret, so the browser can never call Salesforce directly. The
`serve` subcommand is the read-only proxy that closes that gap: it holds the
secret server-side and hands the page curated, allowlisted datasets over CORS.

It deliberately does NOT accept SOQL from the browser. An endpoint that runs
caller-supplied SOQL is an exfiltration hole wearing a dashboard costume: every
field of every object the run-as user can see, one query away, from anywhere
the CORS policy allows.

Usage
-----
    python scripts/sf360.py check                 # prove WHICH org answers
    python scripts/sf360.py datasets              # list the allowlist
    python scripts/sf360.py dataset opportunities # one curated payload
    python scripts/sf360.py query "SELECT ..."    # ad hoc, local/trusted only
    python scripts/sf360.py serve --port 8787     # the dashboard proxy

Configuration is read from real environment variables first, then `.env`
(or `$BLACKBOARD_ENV`), so a deployed host needs no file on disk. Credential
values are never printed, logged, or written to the token cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The org this rail is wired to. `check` refuses to look healthy while pointed
# somewhere else, because a green check against the WRONG org is the exact
# failure this project keeps paying for: an instrument that answers fluently
# about something it never observed.
EXPECTED_ORG_PREFIX = "00Dbm00000wK2ib"

DEFAULT_API_VERSION = "67.0"

# Salesforce does not return `expires_in` for a client credentials grant; the
# token lives until the org's session timeout (commonly 2h). Refresh well
# before that, and treat any 401 as authoritative regardless of this clock.
DEFAULT_TOKEN_TTL_SECONDS = 75 * 60

# The org's ceiling is 15,000 API calls/day (measured 2026-09-17). A dashboard
# that polls unthrottled will eat that before lunch, so served responses are
# cached. At 120s and 8 datasets the worst case is ~5,760 calls/day even with
# every panel refreshing constantly and any number of viewers.
DEFAULT_CACHE_TTL_SECONDS = 120

DEFAULT_MAX_RECORDS = 2000

# Browser origins allowed to read the proxy. Both apex and www are listed
# because the site answers on both (docs/HANDOVER.md ISS-015).
DEFAULT_ALLOWED_ORIGINS = (
    "https://www.sfdc24.com",
    "https://sfdc24.com",
)

_LOCALHOST_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:[0-9]+)?$")


class ConfigError(RuntimeError):
    """Configuration is missing or malformed. Never carries a secret value."""


class SalesforceError(RuntimeError):
    """The org refused or failed a request."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

# Preferred name first, then the name the credentials already live under in
# `.env`. Renaming the .env keys is not required to deploy this.
_CONFIG_KEYS = {
    "client_id": ("SF360_CLIENT_ID", "Headless_consumer_key"),
    "client_secret": ("SF360_CLIENT_SECRET", "Headless_consumer_secret"),
    "domain": ("SF360_DOMAIN", "Headless_domain"),
}


def _parse_env_file(path):
    """Parse a KEY=VALUE file. utf-8-sig: the .env here carries a BOM."""
    values = {}
    try:
        handle = open(path, encoding="utf-8-sig")
    except OSError:
        return values
    with handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env=None, env_file=None):
    """Resolve credentials. Real environment wins over the file.

    Returns a dict with client_id, client_secret, domain, api_version. Raises
    ConfigError naming only the KEYS that are missing -- never their values.
    """
    env = os.environ if env is None else env
    if env_file is None:
        env_file = env.get("BLACKBOARD_ENV") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        )
    from_file = _parse_env_file(env_file)

    config = {}
    missing = []
    for field, names in _CONFIG_KEYS.items():
        for name in names:
            value = env.get(name) or from_file.get(name)
            if value:
                config[field] = value
                break
        else:
            missing.append(" or ".join(names))
    if missing:
        raise ConfigError(
            "missing configuration: " + "; ".join(missing) + ". Set them in the "
            "environment or in the gitignored .env (doctrine D-18: credentials "
            "never live in the repository)."
        )

    domain = config["domain"].strip()
    domain = re.sub(r"^https?://", "", domain).rstrip("/")
    if not domain:
        raise ConfigError("domain resolved to an empty string")
    config["domain"] = domain
    config["api_version"] = (
        env.get("SF360_API_VERSION") or from_file.get("SF360_API_VERSION")
        or DEFAULT_API_VERSION
    )
    return config


def _fingerprint(value):
    """Stable, non-reversing id for a credential, so a rotated key invalidates
    the cache. Truncated digest of a public client id -- not the secret."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def default_cache_path(env=None):
    env = os.environ if env is None else env
    override = env.get("SF360_TOKEN_CACHE")
    if override:
        return override
    home = env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".sf360", "token.json")


# --------------------------------------------------------------------------
# record shaping
# --------------------------------------------------------------------------

def clean_record(value):
    """Strip Salesforce `attributes` and flatten relationships to dotted keys.

    {"Name": "X", "attributes": {...}, "Account": {"attributes": {...},
     "Name": "Y"}}  ->  {"Name": "X", "Account.Name": "Y"}

    A null relationship stays null rather than vanishing, so a table column
    does not silently disappear for the rows that lack it.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "attributes":
                continue
            cleaned = clean_record(item)
            if isinstance(cleaned, dict):
                for sub_key, sub_value in cleaned.items():
                    out[key + "." + sub_key] = sub_value
            else:
                out[key] = cleaned
        return out
    if isinstance(value, list):
        return [clean_record(item) for item in value]
    return value


_SELECT_ONLY = re.compile(r"^\s*select\s", re.IGNORECASE)


def assert_read_only(soql):
    """SOQL cannot mutate, but refuse anything that is not a SELECT anyway.

    Cheap, and it stops a mistyped argument from being handed to a URL builder
    that expects a query.
    """
    if not isinstance(soql, str) or not soql.strip():
        raise ValueError("empty SOQL")
    if not _SELECT_ONLY.match(soql):
        raise ValueError("refusing non-SELECT SOQL: this rail is read-only")
    return soql.strip()


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------

class SalesforceSession:
    """Client-credentials session with a cached token and one 401 retry.

    The 401 retry is the part that makes this "smooth": a token invalidated
    early -- by a session-timeout change, an admin reset, or simply sitting
    idle -- costs one transparent re-auth, not a failed dashboard panel.
    """

    def __init__(self, config=None, cache_path=None, ttl_seconds=None,
                 opener=None, clock=time.time):
        self.config = config or load_config()
        self.cache_path = cache_path if cache_path is not None else default_cache_path()
        self.ttl_seconds = ttl_seconds or int(
            os.environ.get("SF360_TOKEN_TTL", DEFAULT_TOKEN_TTL_SECONDS)
        )
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._lock = threading.Lock()
        self._token = None
        self._instance_url = None
        self._obtained_at = 0.0

    # -- token ------------------------------------------------------------

    def _cache_key(self):
        return _fingerprint(self.config["client_id"] + "@" + self.config["domain"])

    def _read_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, ValueError):
            return None
        if cached.get("key") != self._cache_key():
            return None
        return cached

    def _write_cache(self):
        """Persist the token 0600. The secret is never written here."""
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            payload = {
                "key": self._cache_key(),
                "access_token": self._token,
                "instance_url": self._instance_url,
                "obtained_at": self._obtained_at,
            }
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass  # best effort; Windows ACLs are not POSIX modes
            os.replace(tmp, self.cache_path)
        except OSError:
            pass  # a cache we cannot write is a slow rail, not a broken one

    def _authenticate(self):
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
        }).encode("utf-8")
        url = "https://" + self.config["domain"] + "/services/oauth2/token"
        request = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with self._opener(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise SalesforceError(
                "token grant refused (HTTP %s): %s. If this says "
                "inactive/invalid client, the connected app's client "
                "credentials flow or its run-as user needs attention in the "
                "org -- the credentials themselves are not printed here."
                % (exc.code, detail)
            ) from None
        except urllib.error.URLError as exc:
            raise SalesforceError("cannot reach %s: %s" % (self.config["domain"], exc.reason)) from None
        if "access_token" not in body:
            raise SalesforceError("token grant returned no access_token")
        self._token = body["access_token"]
        self._instance_url = (body.get("instance_url") or "https://" + self.config["domain"]).rstrip("/")
        self._obtained_at = self._clock()
        self._write_cache()
        return self._token

    def token(self, force=False):
        with self._lock:
            if not force:
                if self._token and (self._clock() - self._obtained_at) < self.ttl_seconds:
                    return self._token
                cached = self._read_cache()
                if cached and (self._clock() - cached.get("obtained_at", 0)) < self.ttl_seconds:
                    self._token = cached["access_token"]
                    self._instance_url = cached["instance_url"]
                    self._obtained_at = cached["obtained_at"]
                    return self._token
            return self._authenticate()

    @property
    def instance_url(self):
        if not self._instance_url:
            self.token()
        return self._instance_url

    # -- requests ---------------------------------------------------------

    def get(self, path):
        """GET an absolute-path REST resource, re-authenticating once on 401."""
        for attempt in (0, 1):
            token = self.token(force=bool(attempt))
            url = path if path.startswith("http") else self.instance_url + path
            request = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
            })
            try:
                with self._opener(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    continue  # token died early; one silent re-auth
                detail = exc.read().decode("utf-8", "replace")[:400]
                raise SalesforceError("HTTP %s from %s: %s" % (exc.code, path, detail)) from None
            except urllib.error.URLError as exc:
                raise SalesforceError("cannot reach the org: %s" % (exc.reason,)) from None
        raise SalesforceError("unreachable")

    def query(self, soql, max_records=DEFAULT_MAX_RECORDS):
        """Run SOQL and follow nextRecordsUrl to the cap.

        Returns {records, total_size, truncated}. `truncated` is explicit:
        a silently short list reads as "that is all there is", which is how a
        dashboard ends up quietly wrong about a pipeline.
        """
        assert_read_only(soql)
        path = "/services/data/v%s/query/?q=%s" % (
            self.config["api_version"], urllib.parse.quote(soql),
        )
        records = []
        total_size = 0
        truncated = False
        while path:
            body = self.get(path)
            total_size = body.get("totalSize", len(records))
            for record in body.get("records", []):
                if len(records) >= max_records:
                    truncated = True
                    break
                records.append(clean_record(record))
            if truncated or body.get("done", True):
                break
            path = body.get("nextRecordsUrl")
        return {"records": records, "total_size": total_size, "truncated": truncated}

    def identity(self):
        """Who and WHERE this rail actually points. Identity, not an answer."""
        info = self.get("/services/oauth2/userinfo")
        limits = self.get("/services/data/v%s/limits/" % self.config["api_version"])
        daily = limits.get("DailyApiRequests", {})
        return {
            "organization_id": info.get("organization_id"),
            "user_id": info.get("user_id"),
            "username": info.get("preferred_username") or info.get("username"),
            "instance_url": self.instance_url,
            "api_version": self.config["api_version"],
            "daily_api_max": daily.get("Max"),
            "daily_api_remaining": daily.get("Remaining"),
        }


# --------------------------------------------------------------------------
# the dataset allowlist -- the only SOQL the proxy will ever run
# --------------------------------------------------------------------------

DATASETS = {
    "accounts": {
        "description": "Accounts with owner, tier and telecom status.",
        "soql": (
            "SELECT Id, Name, Type, Industry, Phone, Website, BillingCity, "
            "BillingState, BillingCountry, AnnualRevenue, NumberOfEmployees, "
            "Customer_Number__c, Eligibility_Tier__c, Telecom_Status__c, "
            "Owner.Name, CreatedDate, LastModifiedDate "
            "FROM Account ORDER BY LastModifiedDate DESC"
        ),
    },
    "contacts": {
        "description": "Contacts joined to their account.",
        "soql": (
            "SELECT Id, Name, Title, Email, Phone, MobilePhone, "
            "Account.Name, AccountId, Level__c, Languages__c, "
            "CreatedDate, LastModifiedDate "
            "FROM Contact ORDER BY LastModifiedDate DESC"
        ),
    },
    "opportunities": {
        "description": "Open and closed opportunities with stage and amount.",
        "soql": (
            "SELECT Id, Name, Account.Name, AccountId, StageName, Amount, "
            "CloseDate, Probability, Type, LeadSource, IsClosed, IsWon, "
            "Owner.Name, CreatedDate, LastModifiedDate "
            "FROM Opportunity ORDER BY CloseDate DESC"
        ),
    },
    "leads": {
        "description": "Unconverted and converted leads.",
        "soql": (
            "SELECT Id, Name, Company, Title, Email, Phone, Status, "
            "LeadSource, Industry, Rating, IsConverted, ProductInterest__c, "
            "Owner.Name, CreatedDate, LastModifiedDate "
            "FROM Lead ORDER BY CreatedDate DESC"
        ),
    },
    "cases": {
        "description": "Cases with the complaint/SLA taxonomy this org carries.",
        "soql": (
            "SELECT Id, CaseNumber, Subject, Status, Priority, Origin, Type, "
            "Account.Name, AccountId, Contact.Name, ContactId, "
            "Category__c, Topic__c, Tag__c, SLA_Tier__c, Complaint_Type__c, "
            "AI_Suggested_Category__c, Regulatory_Deadline__c, "
            "IsClosed, CreatedDate, ClosedDate, LastModifiedDate "
            "FROM Case ORDER BY CreatedDate DESC"
        ),
    },
    "pipeline": {
        "description": "Opportunity count and amount grouped by stage.",
        "soql": (
            "SELECT StageName, COUNT(Id) opp_count, SUM(Amount) opp_amount "
            "FROM Opportunity GROUP BY StageName"
        ),
    },
    "cases_by_status": {
        "description": "Case count grouped by status and priority.",
        "soql": (
            "SELECT Status, Priority, COUNT(Id) case_count "
            "FROM Case GROUP BY Status, Priority"
        ),
    },
}

SUMMARY_COUNTS = ("Account", "Contact", "Opportunity", "Lead", "Case")


def build_summary(session):
    """Header tiles in one payload: record counts plus won/open pipeline."""
    counts = {}
    for obj in SUMMARY_COUNTS:
        counts[obj] = session.query("SELECT COUNT() FROM " + obj)["total_size"]
    stages = session.query(DATASETS["pipeline"]["soql"])["records"]
    open_amount = 0.0
    won_amount = 0.0
    for row in stages:
        amount = row.get("opp_amount") or 0
        if row.get("StageName") == "Closed Won":
            won_amount += amount
        elif not str(row.get("StageName", "")).startswith("Closed"):
            open_amount += amount
    return {
        "counts": counts,
        "open_pipeline_amount": open_amount,
        "won_amount": won_amount,
        "by_stage": stages,
    }


def fetch_dataset(session, name, max_records=DEFAULT_MAX_RECORDS):
    """Run one allowlisted dataset. Unknown names are refused by name."""
    if name == "summary":
        payload = build_summary(session)
        payload["dataset"] = "summary"
        return payload
    if name not in DATASETS:
        raise KeyError(name)
    result = session.query(DATASETS[name]["soql"], max_records=max_records)
    result["dataset"] = name
    result["description"] = DATASETS[name]["description"]
    return result


def dataset_names():
    return ["summary"] + sorted(DATASETS)


# --------------------------------------------------------------------------
# the proxy
# --------------------------------------------------------------------------

class TTLCache:
    """Tiny TTL cache. Protects the org's 15k/day budget from a polling UI."""

    def __init__(self, ttl_seconds, clock=time.time):
        self.ttl = ttl_seconds
        self._clock = clock
        self._entries = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry and (self._clock() - entry[0]) < self.ttl:
                return entry[1]
            return None

    def put(self, key, value):
        with self._lock:
            self._entries[key] = (self._clock(), value)


def origin_allowed(origin, allowed):
    """Exact-match allowlist, plus any localhost port for local development."""
    if not origin:
        return False
    if origin in allowed:
        return True
    return bool(_LOCALHOST_ORIGIN.match(origin))


def make_handler(session, cache, allowed_origins, api_token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sf360/1.0"

        def log_message(self, fmt, *args):  # keep the console readable
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        # -- helpers --
        def _cors(self, origin):
            if origin_allowed(origin, allowed_origins):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "X-SF360-Token")

        def _send(self, status, payload, origin):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors(origin)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, query):
            if not api_token:
                return True
            supplied = self.headers.get("X-SF360-Token") or (query.get("token") or [None])[0]
            return supplied == api_token

        # -- verbs --
        def do_OPTIONS(self):
            origin = self.headers.get("Origin")
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Max-Age", "86400")
            self._cors(origin)
            self.end_headers()

        def do_GET(self):
            origin = self.headers.get("Origin")
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            route = parsed.path.rstrip("/") or "/"

            if route == "/healthz":
                self._send(200, {"ok": True, "service": "sf360"}, origin)
                return
            if not self._authorized(query):
                self._send(401, {"error": "missing or wrong X-SF360-Token"}, origin)
                return
            if route in ("/", "/api/datasets"):
                self._send(200, {
                    "datasets": dataset_names(),
                    "detail": {k: v["description"] for k, v in DATASETS.items()},
                }, origin)
                return
            if route == "/api/identity":
                try:
                    self._send(200, session.identity(), origin)
                except SalesforceError as exc:
                    self._send(502, {"error": str(exc)}, origin)
                return
            if route.startswith("/api/dataset/"):
                name = route[len("/api/dataset/"):]
                if name not in dataset_names():
                    self._send(404, {
                        "error": "unknown dataset",
                        "known": dataset_names(),
                    }, origin)
                    return
                try:
                    limit = int((query.get("limit") or [DEFAULT_MAX_RECORDS])[0])
                except ValueError:
                    limit = DEFAULT_MAX_RECORDS
                limit = max(1, min(limit, DEFAULT_MAX_RECORDS))
                cache_key = name + ":" + str(limit)
                payload = cache.get(cache_key)
                if payload is None:
                    try:
                        payload = fetch_dataset(session, name, max_records=limit)
                    except SalesforceError as exc:
                        self._send(502, {"error": str(exc)}, origin)
                        return
                    payload["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    cache.put(cache_key, payload)
                self._send(200, payload, origin)
                return
            self._send(404, {"error": "no such route", "try": "/api/datasets"}, origin)

        # Read-only rail: every mutating verb is refused in one place.
        def _refuse(self):
            self._send(405, {"error": "this rail is read-only"}, self.headers.get("Origin"))

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

    return Handler


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_json(value):
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")


def cmd_check(args):
    session = SalesforceSession()
    identity = session.identity()
    _print_json(identity)
    org = str(identity.get("organization_id") or "")
    if not org.startswith(EXPECTED_ORG_PREFIX):
        sys.stderr.write(
            "\nWRONG ORG: expected one starting %s, got %s. The credentials "
            "point somewhere else; do not trust a dashboard built on this.\n"
            % (EXPECTED_ORG_PREFIX, org or "<none>")
        )
        return 2
    sys.stderr.write("\nOK: live on %s as %s\n" % (org, identity.get("username")))
    return 0


def cmd_datasets(args):
    _print_json({
        "datasets": dataset_names(),
        "detail": {k: v["description"] for k, v in DATASETS.items()},
    })
    return 0


def cmd_dataset(args):
    session = SalesforceSession()
    try:
        _print_json(fetch_dataset(session, args.name, max_records=args.limit))
    except KeyError:
        sys.stderr.write("unknown dataset %r; known: %s\n" % (args.name, ", ".join(dataset_names())))
        return 2
    return 0


def cmd_query(args):
    session = SalesforceSession()
    _print_json(session.query(args.soql, max_records=args.limit))
    return 0


def cmd_serve(args):
    session = SalesforceSession()
    identity = session.identity()
    org = str(identity.get("organization_id") or "")
    if not org.startswith(EXPECTED_ORG_PREFIX):
        sys.stderr.write("refusing to serve: credentials point at org %s, not %s\n"
                         % (org or "<none>", EXPECTED_ORG_PREFIX))
        return 2
    allowed = set(DEFAULT_ALLOWED_ORIGINS)
    if args.allow_origin:
        allowed.update(args.allow_origin)
    api_token = os.environ.get("SF360_API_TOKEN")
    cache = TTLCache(args.cache_ttl)
    handler = make_handler(session, cache, allowed, api_token)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    sys.stderr.write(
        "sf360 serving http://%s:%d on org %s as %s\n"
        "  origins: %s\n  cache: %ds   token required: %s\n"
        % (args.host, args.port, org, identity.get("username"),
           ", ".join(sorted(allowed)) + ", plus any localhost port",
           args.cache_ttl, "yes" if api_token else "NO (set SF360_API_TOKEN)")
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
    finally:
        httpd.server_close()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="sf360", description="Live read-only rail into the SFDC24 dev org.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="prove which org and user answer").set_defaults(func=cmd_check)
    sub.add_parser("datasets", help="list the dataset allowlist").set_defaults(func=cmd_datasets)

    one = sub.add_parser("dataset", help="fetch one allowlisted dataset")
    one.add_argument("name")
    one.add_argument("--limit", type=int, default=DEFAULT_MAX_RECORDS)
    one.set_defaults(func=cmd_dataset)

    adhoc = sub.add_parser("query", help="run ad hoc SOQL (local/trusted use)")
    adhoc.add_argument("soql")
    adhoc.add_argument("--limit", type=int, default=DEFAULT_MAX_RECORDS)
    adhoc.set_defaults(func=cmd_query)

    serve = sub.add_parser("serve", help="run the dashboard proxy")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--cache-ttl", type=int, default=DEFAULT_CACHE_TTL_SECONDS)
    serve.add_argument("--allow-origin", action="append",
                       help="extra allowed browser origin; repeatable")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        sys.stderr.write("configuration error: %s\n" % (exc,))
        return 2
    except SalesforceError as exc:
        sys.stderr.write("salesforce error: %s\n" % (exc,))
        return 1


if __name__ == "__main__":
    sys.exit(main())
