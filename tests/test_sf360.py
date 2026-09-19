#!/usr/bin/env python3
"""Tests for scripts/sf360.py, the read-only rail into the dev org.

WHAT IS ON TRIAL
  sf360.py holds a live client secret and hands data to a browser. Three things
  therefore have to be true, and none of them can be taken on faith:

    1. It points at the org it says it points at. A green dashboard built on
       the WRONG org is this project's recurring failure - an instrument that
       answers fluently about something it never observed.
    2. It is read-only at every layer: the SOQL guard, the absent mutation
       routes, and the absence of any route that runs caller-supplied SOQL.
       The proxy's whole reason to exist is that a static page cannot hold a
       secret; an endpoint taking arbitrary SOQL would hand the secret's
       PRIVILEGE to anyone the CORS policy allows, which is worse.
    3. No credential reaches a log, an error message, or the token cache.

  The CORS allowlist is tested for what it REFUSES, not only what it allows.
  An allowlist that matches by prefix would accept www.sfdc24.com.evil.com,
  and nothing about a passing happy-path test would say so.

WHAT THIS SUITE DOES NOT PROVE
  Not one assertion here touches Salesforce. Every session is driven by a fake
  opener, so this suite proves the CLIENT's behaviour - token reuse, the single
  401 re-auth, pagination, truncation, refusals - and says nothing about
  whether the org is reachable or whether a field in DATASETS still exists.
  That is what `python scripts/sf360.py check` and `dataset` are for, and their
  real output belongs in the board row, not in a comment here.

  The HTTP section binds a real socket on 127.0.0.1. It needs loopback and
  nothing else; if loopback is down it FAILS rather than skipping, because a
  skip that reports green is the thing this repo keeps paying for.

RUN
  python3 tests/test_sf360.py
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")

spec = importlib.util.spec_from_file_location(
    "sf360_mod", os.path.join(SCRIPTS, "sf360.py"))
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)

PASS = 0
FAIL = 0
FAILURES = []

# Values that must never leak. Distinctive on purpose: a substring search for
# them is only meaningful if they cannot occur by accident.
FAKE_ID = "3MVG9fake0client0id0for0tests"
FAKE_SECRET = "SECRETvalue0must0never0appear0anywhere"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL " + name + ("  --> " + str(detail) if detail else ""))


def raises(exc_type, fn, *a, **kw):
    """Return the exception instance, or None if the call did not raise it."""
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return exc
    except Exception:  # noqa: BLE001 - wrong type is a failure, not an error
        return None
    return None


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body="{}"):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "boom", {},
        io.BytesIO(body.encode("utf-8")))


class FakeOpener:
    """Programmable stand-in for urllib.request.urlopen.

    `script` maps a URL substring to a list of responses consumed in order; a
    response may be an exception instance, which is raised. Every request is
    recorded so the tests can count authentications rather than infer them.
    """

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.bodies = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(url)
        if request.data:
            self.bodies.append(request.data.decode("utf-8"))
        for fragment, queue in self.script.items():
            if fragment in url:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return FakeResponse(item)
        raise AssertionError("no scripted response for " + url)

    def count(self, fragment):
        return len([u for u in self.requests if fragment in u])


TOKEN_OK = {"access_token": "tok-1", "instance_url": "https://example.my.salesforce.com"}
CONFIG = {
    "client_id": FAKE_ID,
    "client_secret": FAKE_SECRET,
    "domain": "example.my.salesforce.com",
    "api_version": "67.0",
}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def session_with(script, cache_path, ttl=600, clock=None):
    opener = FakeOpener(script)
    sess = sf.SalesforceSession(
        config=dict(CONFIG), cache_path=cache_path, ttl_seconds=ttl,
        opener=opener, clock=clock or Clock())
    return sess, opener


TMP = tempfile.mkdtemp(prefix="sf360-test-")

try:
    # ----------------------------------------------------------------------
    print("== configuration: the environment wins, and no value is ever named ==")

    env_file = os.path.join(TMP, "dotenv")
    # utf-8-sig on purpose: the real .env on this box carries a BOM, and a
    # parser that reads it as utf-8 silently loses the FIRST key only.
    with open(env_file, "w", encoding="utf-8-sig") as fh:
        fh.write("Headless_consumer_key=file-id\n")
        fh.write("Headless_consumer_secret=" + FAKE_SECRET + "\n")
        fh.write("Headless_domain=https://file-domain.my.salesforce.com/\n")
        fh.write("# a comment\n")
        fh.write("\n")
        fh.write('QUOTED="quoted-value"\n')

    cfg = sf.load_config(env={}, env_file=env_file)
    check("the first key survives a BOM", cfg["client_id"] == "file-id", cfg["client_id"])
    check("the .env fallback names are read",
          cfg["client_secret"] == FAKE_SECRET)
    check("scheme and trailing slash are stripped from the domain",
          cfg["domain"] == "file-domain.my.salesforce.com", cfg["domain"])
    check("the api version defaults when unset",
          cfg["api_version"] == sf.DEFAULT_API_VERSION, cfg["api_version"])
    check("quotes are stripped from a value",
          sf._parse_env_file(env_file)["QUOTED"] == "quoted-value")
    check("a comment line is not a key",
          "# a comment" not in sf._parse_env_file(env_file))

    cfg2 = sf.load_config(
        env={"SF360_CLIENT_ID": "env-id", "SF360_API_VERSION": "64.0"},
        env_file=env_file)
    check("a real environment variable beats the file", cfg2["client_id"] == "env-id")
    check("an explicit api version beats the default", cfg2["api_version"] == "64.0")
    check("the file still fills what the environment omits",
          cfg2["domain"] == "file-domain.my.salesforce.com")

    missing = raises(sf.ConfigError, sf.load_config, env={},
                     env_file=os.path.join(TMP, "does-not-exist"))
    check("missing configuration raises ConfigError", missing is not None)
    check("the error names the KEYS that are missing",
          missing is not None and "SF360_CLIENT_SECRET" in str(missing), missing)
    check("the error carries no credential VALUE",
          missing is not None and FAKE_SECRET not in str(missing))

    empty_domain = os.path.join(TMP, "empty-domain")
    with open(empty_domain, "w", encoding="utf-8") as fh:
        fh.write("Headless_consumer_key=a\nHeadless_consumer_secret=b\n"
                 "Headless_domain=https://\n")
    blank = raises(sf.ConfigError, sf.load_config, env={}, env_file=empty_domain)
    check("a domain that is only a scheme is refused", blank is not None, blank)

    # ----------------------------------------------------------------------
    print("== the read-only guard, including what it must REFUSE ==")

    check("a plain SELECT passes",
          sf.assert_read_only("SELECT Id FROM Account") == "SELECT Id FROM Account")
    check("lowercase and leading space pass",
          sf.assert_read_only("   select id from account").startswith("select"))
    for bad in ("DELETE FROM Account", "UPDATE Account SET Name='x'",
                "INSERT INTO Account", "drop table Account",
                "; SELECT Id FROM Account", "", "   "):
        check("refused: " + (bad or "<empty>"),
              raises(ValueError, sf.assert_read_only, bad) is not None)
    check("a non-string is refused", raises(ValueError, sf.assert_read_only, None) is not None)
    check("the refusal says why",
          "read-only" in str(raises(ValueError, sf.assert_read_only, "DELETE x")))

    # ----------------------------------------------------------------------
    print("== record shaping: a null relationship must not vanish ==")

    raw = {
        "attributes": {"type": "Opportunity", "url": "/x"},
        "Name": "Deal",
        "Amount": 1000.0,
        "Account": {"attributes": {"type": "Account"}, "Name": "Acme",
                    "Owner": {"attributes": {}, "Name": "Ada"}},
        "Contact": None,
    }
    clean = sf.clean_record(raw)
    check("attributes are stripped", "attributes" not in clean)
    check("a relationship flattens to a dotted key", clean.get("Account.Name") == "Acme")
    check("a nested relationship flattens twice",
          clean.get("Account.Owner.Name") == "Ada", clean)
    check("a null relationship stays as a key", "Contact" in clean and clean["Contact"] is None)
    check("scalars are untouched", clean["Amount"] == 1000.0)
    check("a list of records is cleaned elementwise",
          sf.clean_record([raw])[0].get("Account.Name") == "Acme")
    check("a bare scalar passes through", sf.clean_record(7) == 7)

    # ----------------------------------------------------------------------
    print("== the token: reused within its TTL, re-fetched after it ==")

    cache_a = os.path.join(TMP, "cache-a.json")
    clock = Clock()
    sess, opener = session_with(
        {"/services/oauth2/token": [TOKEN_OK],
         "/limits/": [{"DailyApiRequests": {"Max": 15000, "Remaining": 14900}}],
         "/services/oauth2/userinfo": [{"organization_id": "00Dbm00000wK2ibEAC",
                                        "user_id": "005bm00000VUzNZAA1",
                                        "preferred_username": "a@b.c"}]},
        cache_a, ttl=600, clock=clock)

    first = sess.token()
    second = sess.token()
    check("the token is fetched once", opener.count("/services/oauth2/token") == 1,
          opener.requests)
    check("the second call reuses it", first == second == "tok-1")

    clock.now += 601
    sess.token()
    check("an expired token is re-fetched",
          opener.count("/services/oauth2/token") == 2, opener.requests)

    check("the grant is a client_credentials POST",
          "grant_type=client_credentials" in opener.bodies[0])

    # a SECOND session, same credentials and cache file: no new authentication
    sess_b, opener_b = session_with({"/services/oauth2/token": [TOKEN_OK]},
                                    cache_a, ttl=600, clock=clock)
    cached_token = sess_b.token()
    check("a fresh session reuses the cached token",
          opener_b.count("/services/oauth2/token") == 0 and cached_token == "tok-1",
          opener_b.requests)

    # rotate the client id: the cache is keyed by a fingerprint, so it must miss
    rotated = dict(CONFIG, client_id="3MVG9rotated0client0id")
    sess_c = sf.SalesforceSession(config=rotated, cache_path=cache_a, ttl_seconds=600,
                                  opener=FakeOpener({"/services/oauth2/token": [TOKEN_OK]}),
                                  clock=clock)
    sess_c.token()
    check("rotating the client id invalidates the cache",
          sess_c._read_cache() is not None and
          sess_c._cache_key() != sess_b._cache_key())

    with open(cache_a, encoding="utf-8") as fh:
        cache_text = fh.read()
    check("the token cache holds no client secret", FAKE_SECRET not in cache_text)
    check("the token cache holds no client id", FAKE_ID not in cache_text, cache_text[:120])
    check("the token cache does hold the access token", "tok-1" in cache_text)

    # ----------------------------------------------------------------------
    print("== one silent re-auth on 401, and no second one ==")

    sess_d, opener_d = session_with(
        {"/services/oauth2/token": [TOKEN_OK, TOKEN_OK],
         "/services/data/": [http_error(401, '{"message":"expired"}'),
                             {"records": [], "totalSize": 0, "done": True}]},
        os.path.join(TMP, "cache-d.json"), ttl=600)
    # Caught rather than allowed to propagate: a mutation that DELETES the
    # re-auth must make this named assertion fail, not crash the file before
    # the remaining sections run. A traceback is not a test result.
    try:
        body = sess_d.get("/services/data/v67.0/query/?q=x")
    except Exception as exc:  # noqa: BLE001
        body = {"raised": type(exc).__name__ + ": " + str(exc)}
    check("a 401 costs one transparent re-auth, not a failure",
          body == {"records": [], "totalSize": 0, "done": True}, body)
    check("it authenticated exactly twice",
          opener_d.count("/services/oauth2/token") == 2, opener_d.requests)

    sess_e, opener_e = session_with(
        {"/services/oauth2/token": [TOKEN_OK],
         "/services/data/": [http_error(401), http_error(401)]},
        os.path.join(TMP, "cache-e.json"), ttl=600)
    err = raises(sf.SalesforceError, sess_e.get, "/services/data/v67.0/query/?q=x")
    check("a second 401 is raised, not retried forever", err is not None, err)

    unreachable = session_with(
        {"/services/oauth2/token": [urllib.error.URLError("no route")]},
        os.path.join(TMP, "cache-f.json"))
    net = raises(sf.SalesforceError, unreachable[0].token)
    check("an unreachable org raises SalesforceError", net is not None)
    check("the unreachable message names the domain, not the secret",
          net is not None and "example.my.salesforce.com" in str(net)
          and FAKE_SECRET not in str(net), net)

    refused = session_with(
        {"/services/oauth2/token": [http_error(400, '{"error":"invalid_client"}')]},
        os.path.join(TMP, "cache-g.json"))
    bad = raises(sf.SalesforceError, refused[0].token)
    check("a refused grant explains itself without the credentials",
          bad is not None and "invalid_client" in str(bad) and FAKE_SECRET not in str(bad)
          and FAKE_ID not in str(bad), bad)

    # ----------------------------------------------------------------------
    print("== pagination, and truncation that says so ==")

    page1 = {"totalSize": 3, "done": False, "nextRecordsUrl": "/services/data/v67.0/query/next",
             "records": [{"attributes": {}, "Id": "1"}, {"attributes": {}, "Id": "2"}]}
    page2 = {"totalSize": 3, "done": True, "records": [{"attributes": {}, "Id": "3"}]}
    sess_p, opener_p = session_with(
        {"/services/oauth2/token": [TOKEN_OK], "/query/?q=": [page1], "/query/next": [page2]},
        os.path.join(TMP, "cache-p.json"))
    result = sess_p.query("SELECT Id FROM Account")
    check("every page is followed", [r["Id"] for r in result["records"]] == ["1", "2", "3"],
          result["records"])
    check("total_size comes from the org", result["total_size"] == 3)
    check("a complete read is not marked truncated", result["truncated"] is False)
    check("records come back cleaned", "attributes" not in result["records"][0])

    sess_t, opener_t = session_with(
        {"/services/oauth2/token": [TOKEN_OK], "/query/?q=": [page1], "/query/next": [page2]},
        os.path.join(TMP, "cache-t.json"))
    capped = sess_t.query("SELECT Id FROM Account", max_records=1)
    check("the cap is honoured", len(capped["records"]) == 1)
    check("a capped read is MARKED truncated", capped["truncated"] is True)
    check("a capped read stops fetching pages", opener_t.count("/query/next") == 0,
          opener_t.requests)
    check("query refuses non-SELECT before any request",
          raises(ValueError, sess_t.query, "DELETE FROM Account") is not None)

    # ----------------------------------------------------------------------
    print("== identity: WHICH org, not merely 'a' org ==")

    sess_i, opener_i = session_with(
        {"/services/oauth2/token": [TOKEN_OK],
         "/services/oauth2/userinfo": [{"organization_id": "00Dbm00000wK2ibEAC",
                                        "user_id": "005bm00000VUzNZAA1",
                                        "preferred_username": "abdus@example.com"}],
         "/limits/": [{"DailyApiRequests": {"Max": 15000, "Remaining": 14900}}]},
        os.path.join(TMP, "cache-i.json"))
    ident = sess_i.identity()
    check("identity reports the org id", ident["organization_id"] == "00Dbm00000wK2ibEAC")
    check("identity reports the run-as user", ident["user_id"] == "005bm00000VUzNZAA1")
    check("identity reports the remaining daily budget",
          ident["daily_api_remaining"] == 14900 and ident["daily_api_max"] == 15000)
    check("the expected org prefix matches the org this rail is for",
          ident["organization_id"].startswith(sf.EXPECTED_ORG_PREFIX))
    check("a different org would NOT match the prefix",
          not "00D000000000000EAA".startswith(sf.EXPECTED_ORG_PREFIX))

    # ----------------------------------------------------------------------
    print("== the TTL cache that protects a 15k/day budget ==")

    cclock = Clock()
    cache = sf.TTLCache(120, clock=cclock)
    cache.put("k", {"v": 1})
    check("a fresh entry is returned", cache.get("k") == {"v": 1})
    cclock.now += 119
    check("an entry inside the TTL survives", cache.get("k") == {"v": 1})
    cclock.now += 2
    check("an expired entry is a miss", cache.get("k") is None)
    check("an unknown key is a miss", cache.get("nope") is None)

    # ----------------------------------------------------------------------
    print("== the CORS allowlist, judged by what it REFUSES ==")

    allowed = set(sf.DEFAULT_ALLOWED_ORIGINS)
    check("apex domain allowed", sf.origin_allowed("https://sfdc24.com", allowed))
    check("www allowed", sf.origin_allowed("https://www.sfdc24.com", allowed))
    check("localhost allowed for development",
          sf.origin_allowed("http://localhost:5173", allowed))
    check("127.0.0.1 allowed", sf.origin_allowed("http://127.0.0.1:8080", allowed))
    check("a suffix attacker is REFUSED",
          not sf.origin_allowed("https://www.sfdc24.com.evil.com", allowed))
    check("a prefix attacker is REFUSED",
          not sf.origin_allowed("https://evil.com/https://www.sfdc24.com", allowed))
    check("a lookalike host is REFUSED",
          not sf.origin_allowed("https://wwwXsfdc24.com", allowed))
    check("plain http on the real domain is REFUSED",
          not sf.origin_allowed("http://www.sfdc24.com", allowed))
    check("a localhost lookalike is REFUSED",
          not sf.origin_allowed("http://localhost.evil.com", allowed))
    check("no origin is REFUSED", not sf.origin_allowed(None, allowed))
    check("an empty origin is REFUSED", not sf.origin_allowed("", allowed))

    # ----------------------------------------------------------------------
    print("== the proxy over a real socket on loopback ==")

    class FakeSession:
        """Stands in for SalesforceSession at the handler boundary."""

        def __init__(self):
            self.queries = []

        def identity(self):
            return {"organization_id": "00Dbm00000wK2ibEAC", "user_id": "005x",
                    "username": "abdus@example.com"}

        def query(self, soql, max_records=sf.DEFAULT_MAX_RECORDS):
            self.queries.append(soql)
            return {"records": [{"Id": "006x", "Name": "Deal"}],
                    "total_size": 1, "truncated": False}

    fake_session = FakeSession()
    server = None
    try:
        handler = sf.make_handler(fake_session, sf.TTLCache(120),
                                  set(sf.DEFAULT_ALLOWED_ORIGINS), "test-token")
        server = sf.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        check("loopback is available for the HTTP section", False, exc)

    if server is not None:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]

        def call(path, token=None, origin=None, method="GET"):
            req = urllib.request.Request(base + path, method=method)
            if token:
                req.add_header("X-SF360-Token", token)
            if origin:
                req.add_header("Origin", origin)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("utf-8")
                    return resp.status, dict(resp.headers), (json.loads(raw) if raw else {})
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8")
                return exc.code, dict(exc.headers), (json.loads(raw) if raw else {})

        status, _, body = call("/healthz")
        check("healthz answers without a token", status == 200 and body.get("ok") is True,
              (status, body))

        status, _, body = call("/api/datasets")
        check("a tokenless data request is 401", status == 401, (status, body))
        check("the 401 says what is missing", "X-SF360-Token" in json.dumps(body))

        status, _, body = call("/api/datasets", token="wrong")
        check("a wrong token is 401", status == 401, status)

        status, _, body = call("/api/datasets", token="test-token")
        check("datasets lists the allowlist", status == 200 and "summary" in body["datasets"],
              body)
        check("every allowlisted dataset is described",
              set(body["detail"]) == set(sf.DATASETS), body.get("detail"))

        status, _, body = call("/api/dataset/opportunities", token="test-token")
        check("an allowlisted dataset is served", status == 200 and body["dataset"] == "opportunities",
              (status, body))
        check("the served payload is stamped with a fetch time", "fetched_at" in body)
        check("the org was queried once", len(fake_session.queries) == 1, fake_session.queries)

        status, _, body = call("/api/dataset/opportunities", token="test-token")
        check("the second read is served from cache, not the org",
              status == 200 and len(fake_session.queries) == 1, fake_session.queries)

        status, _, body = call("/api/dataset/Opportunities", token="test-token")
        check("dataset names are case-sensitive and an unknown one is 404",
              status == 404 and "known" in body, (status, body))

        status, _, body = call("/api/query?q=SELECT+Id+FROM+Account", token="test-token")
        check("there is NO route that runs caller-supplied SOQL", status == 404,
              (status, body))

        status, _, body = call("/api/dataset/opportunities", token="test-token", method="POST")
        check("POST is refused as read-only", status == 405, (status, body))
        status, _, body = call("/api/dataset/opportunities", token="test-token", method="DELETE")
        check("DELETE is refused as read-only", status == 405, status)

        status, headers, _ = call("/healthz", origin="https://www.sfdc24.com")
        check("an allowed origin is echoed back",
              headers.get("Access-Control-Allow-Origin") == "https://www.sfdc24.com", headers)
        check("the response varies on Origin", headers.get("Vary") == "Origin")

        status, headers, _ = call("/healthz", origin="https://www.sfdc24.com.evil.com")
        check("a refused origin gets NO allow-origin header",
              "Access-Control-Allow-Origin" not in headers, headers)

        status, headers, _ = call("/api/datasets", origin="https://www.sfdc24.com",
                                  method="OPTIONS")
        check("the preflight answers 204", status == 204, status)
        check("the preflight allows only GET and OPTIONS",
              headers.get("Access-Control-Allow-Methods") == "GET, OPTIONS", headers)

        status, headers, _ = call("/api/datasets", origin="https://evil.example",
                                  method="OPTIONS")
        check("a refused origin gets no preflight approval either",
              "Access-Control-Allow-Origin" not in headers, headers)

        # An unauthenticated proxy is the deployment mistake worth catching.
        open_handler = sf.make_handler(FakeSession(), sf.TTLCache(120),
                                       set(sf.DEFAULT_ALLOWED_ORIGINS), None)
        open_server = sf.ThreadingHTTPServer(("127.0.0.1", 0), open_handler)
        threading.Thread(target=open_server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/datasets" % open_server.server_address[1],
                    timeout=10) as resp:
                open_status = resp.status
        except urllib.error.HTTPError as exc:
            open_status = exc.code
        check("with no SF360_API_TOKEN set the proxy is OPEN, and the suite says so",
              open_status == 200, open_status)
        open_server.shutdown()
        open_server.server_close()

        server.shutdown()
        server.server_close()

    # ----------------------------------------------------------------------
    print("== the dataset allowlist itself ==")

    check("summary is offered and is not a SOQL entry",
          "summary" in sf.dataset_names() and "summary" not in sf.DATASETS)
    check("the names are sorted after summary",
          sf.dataset_names()[1:] == sorted(sf.DATASETS))
    for name, entry in sorted(sf.DATASETS.items()):
        check("allowlisted SOQL is a SELECT: " + name,
              sf.assert_read_only(entry["soql"]) is not None)
        check("allowlisted dataset is described: " + name,
              bool(entry.get("description", "").strip()))
    check("an unknown dataset is refused by name",
          raises(KeyError, sf.fetch_dataset, FakeSession(), "everything") is not None)

    summary = sf.build_summary(FakeSession())
    check("the summary counts every header object",
          set(summary["counts"]) == set(sf.SUMMARY_COUNTS), summary["counts"])
    check("the summary separates open pipeline from won",
          "open_pipeline_amount" in summary and "won_amount" in summary)

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("")
print("passed=%d failed=%d" % (PASS, FAIL))
if FAIL:
    # One per line, not a comma-joined list. Several assertion names contain a
    # comma themselves, and tests/mutate_sf360.py has to match a failure name
    # EXACTLY to prove it caught the defect it aimed at. The first version of
    # this pair joined on commas and the mutation control reported the re-auth
    # case as uncovered when it had in fact been caught - a harness bug that
    # produced a wrong result, in the direction that looks like diligence.
    for _name in FAILURES:
        print("failure: " + _name)
    sys.exit(1)
