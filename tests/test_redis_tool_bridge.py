#!/usr/bin/env python3
"""The bridge's auth is the whole product, so the tests are about auth and about absence.

Run directly: `python tests/test_redis_tool_bridge.py`. These suites are plain scripts that exit 1
on failure; `unittest discover` does not collect them and would report a green tick over zero
assertions.

WHAT IS BEING GUARDED, in the words of the review that found it
(CCC-BRIDGE-REVIEW-20261006T1330Z, verdict CHANGES-REQUESTED):

  1. "verifyIdToken is called with the token alone and NO AUDIENCE ... ANY Google-signed ID token
     minted for ANY service passes that middleware."
  2. "checkServerIdentity returning undefined DISABLES SERVER IDENTITY VERIFICATION."
  3. "authentication without authorisation: the middleware extracts payload.email and calls next,
     so any verified Google identity is in."
  4. "the secrets are bound as ENVIRONMENT VARIABLES. An env var is listable from anything that can
     read the process."

Each one gets a test that fails if the defect comes back, and three of the four are checked against
the SOURCE as well as the behaviour - because defect 2 is a line that can be deleted without any
test noticing, and defect 4 is a deploy argument, not a code path.
"""
import ast
import contextlib
import io
import os
import pathlib
import ssl
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "redis-tool-bridge"))
sys.path.insert(0, str(ROOT / "scripts"))

import main as bridge  # noqa: E402

AUD = "https://bridge.example.invalid"
CALLER = "caller@example.invalid"
SOURCE = (ROOT / "cloud" / "redis-tool-bridge" / "main.py").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "cloud" / "redis-tool-bridge" / "Dockerfile").read_text(encoding="utf-8")
DEPLOY = (ROOT / "cloud" / "redis-tool-bridge" / "deploy.sh").read_text(encoding="utf-8")
OP_SOURCE = (ROOT / "cloud" / "redis-tool-bridge" / "op.py").read_text(encoding="utf-8")


def statements(source: str) -> str:
    """The source with its PROSE removed: no docstrings, no comments, no blank lines.

    THIS EXISTS BECAUSE THE FIRST VERSION OF THESE TESTS FAILED ON ITS OWN DOCUMENTATION.
    `test_the_bridge_does_not_implement_its_own_tls` forbids the string "ssl_cert_reqs" in the
    bridge - and the module docstring NAMES ssl_cert_reqs while explaining that the bridge borrows
    it rather than setting it. The test read prose as if it were code and failed a file that was
    correct.

    That is the third time this exact shape has bitten: in tests/test_bus_verify.py the
    cross-row-digest guard first asserted "hashlib is not imported", then matched the module
    docstring describing the old collision, before it was finally made to scan statements. A guard
    that greps a whole file greps the explanation of the bug along with the bug.
    """
    import io
    import tokenize
    out = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:                      # pragma: no cover - a syntax error fails louder
        return source
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.line.strip().startswith(('"""', "'''")):
            continue                                 # a docstring, standing alone on its own line
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            continue
        out.append(tok.string)
    return "\n".join(out)


CODE = statements(SOURCE)


def redis_methods(source):
    """An absent audience must refuse every request, NOT verify without one."""
    return {node.func.attr for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "conn"}


EXPECTED_REDIS_METHODS = {"setex", "get", "ttl", "delete"}


class Defect1TheAudienceIsChecked(unittest.TestCase):
    """Two real deploy bugs in the first version of deploy.sh, both invisible to every auth test."""

    def test_no_configured_audience_refuses(self):
        who, why = bridge.verify_caller("any.token", "", frozenset({CALLER}))
        self.assertIsNone(who)
        self.assertIn("audience", why)

    def test_the_verifier_is_called_with_an_audience_argument(self):
        """The caller selects no key, no command, no namespace and no TTL. Absence, not refusal."""
        where = SOURCE.index("verify_oauth2_token(")
        call = SOURCE[where:where + 220]
        self.assertIn("audience=", call,
                      "verify_oauth2_token without audience= is defect 1, exactly")

    def test_the_audience_is_not_defaulted(self):
        """The missing-COPY/missing-allowlist bug, which has now cost four builds.

    tests/test_image_layout.py does this - for cloud/bus-reconciler/Dockerfile ONLY, hardcoded. Its
    green tick says nothing about this directory, so running it and feeling covered would be the
    same mistake as the guard that parsed one agenda section while the chair parsed every one. The
    assertion belongs beside the Dockerfile it is about.

    /.gcloudignore is an ALLOWLIST: it ignores `/*` and then re-admits, directory by directory. A
    COPY of a path nobody re-admitted builds fine locally and fails in Cloud Build, where the file
    simply is not there.
    """
        self.assertNotIn('BRIDGE_AUDIENCE", "http', SOURCE)
        self.assertNotIn("BRIDGE_AUDIENCE', 'http", SOURCE)


class Defect2ServerIdentityVerificationStaysOn(unittest.TestCase):
    def test_the_bridge_does_not_implement_its_own_tls(self):
        """Stand in for google.oauth2.id_token so the authorisation half can be tested without a real
    Google token. It does NOT stand in for the audience check - that is asserted against the source,
    because a fake verifier that ignores the audience would pass either way."""
        for forbidden in ("ssl_cert_reqs", "check_hostname", "checkServerIdentity",
                          "CERT_NONE", "SSLContext", "verify=False"):
            self.assertNotIn(forbidden, CODE,
                             "%s in the bridge means it is making its own TLS decisions" % forbidden)

    def test_it_connects_through_redis_dual(self):
        self.assertIn("redis_dual", CODE)
        self.assertIn("client", CODE)
        self.assertIn("redis_dual.client(", SOURCE)

    def test_the_image_copies_the_shared_client_and_its_settings(self):
        self.assertIn("COPY scripts/redis_dual.py", DOCKERFILE)
        self.assertIn("COPY scripts/redis_dual.settings.json", DOCKERFILE)


class Defect3AuthenticationIsNotAuthorisation(unittest.TestCase):
    def test_no_configured_allowlist_refuses(self):
        who, why = bridge.verify_caller("any.token", AUD, frozenset())
        self.assertIsNone(who)
        self.assertIn("allowlist", why)

    def test_a_verified_identity_off_the_allowlist_is_refused(self):
        """A default audience would be worse than none: it would look configured and check the
        wrong thing. os.environ.get must not supply a fallback URL."""
        claims = {"email": "other@example.invalid", "email_verified": True}
        with Verifier(claims):
            who, why = bridge.verify_caller("tok", AUD, frozenset({CALLER}))
        self.assertIsNone(who, "a verified identity is not an authorised one")
        self.assertEqual("not authorised", why)

    def test_the_allowlisted_caller_is_accepted_and_named(self):
        """It borrows scripts/redis_dual.py. A private client here is how one process ends up with
        verification off while the other has it on."""
        with Verifier({"email": CALLER, "email_verified": True}):
            who, why = bridge.verify_caller("tok", AUD, frozenset({CALLER}))
        self.assertEqual(CALLER, who)
        self.assertIsNone(why)

    def test_an_unverified_email_claim_is_refused(self):
        with Verifier({"email": CALLER, "email_verified": False}):
            who, why = bridge.verify_caller("tok", AUD, frozenset({CALLER}))
        self.assertIsNone(who)

    def test_the_allowlist_is_case_insensitive_but_not_substring(self):
        os.environ["BRIDGE_CALLERS"] = "CALLER@example.invalid, second@example.invalid"
        try:
            self.assertIn(CALLER, bridge.allowlist())
            self.assertNotIn("caller@example.invalid.evil.com",
                             bridge.allowlist())
        finally:
            del os.environ["BRIDGE_CALLERS"]

    def test_the_platform_gate_is_demanded_too(self):
        """The borrowed path is only a defence while it still verifies. Asserted against the module
        this image actually copies, not against a comment about it."""
        self.assertIn("--no-allow-unauthenticated", DEPLOY)
        self.assertNotIn("--allow-unauthenticated", DEPLOY.replace("--no-allow-unauthenticated", ""))

    def test_the_invoker_is_never_everyone(self):
        """THE DEFECT ITSELF. A real, verified, Google-signed token for a real service account that
        nobody authorised must not get in."""
        members = [line for line in DEPLOY.splitlines()
                   if "INVOKERS=(" in line or "--member=" in line]
        self.assertTrue(members, "the deploy script grants no invoker at all")
        for line in members:
            for bad in ("allUsers", "allAuthenticatedUsers", "domain:", "allauthenticated"):
                self.assertNotIn(bad, line, "an everyone-binding in %r" % line.strip()[:80])
            # A literal member must be a named service account; a VARIABLE member is fine, because
            # the only thing it can expand from is INVOKERS, which this same loop checks.
            self.assertTrue("serviceAccount:" in line or "${member}" in line or "$member" in line,
                            "a member that is neither a named SA nor the checked variable: %r"
                            % line.strip()[:90])
        self.assertTrue(any("INVOKERS=(" in line and "serviceAccount:" in line
                            for line in members),
                        "INVOKERS must list named service accounts literally")


class Defect4SecretsAreFilesNotEnvironmentVariables(unittest.TestCase):
    def test_the_deploy_mounts_both_secrets_as_files_in_separate_dirs(self):
        self.assertIn("/secrets/auth/redis-auth=REDIS_AUTH_STRING", DEPLOY)
        self.assertIn("/secrets/ca/redis-ca.pem=REDIS_CA_CERT", DEPLOY)

    def test_no_secret_is_bound_as_an_environment_variable(self):
        """The control. Without this the tests above would pass on a bridge that refuses everyone."""
        bindings = [line for line in DEPLOY.splitlines() if "--set-secrets=" in line]
        self.assertTrue(bindings, "the deploy script mounts no secrets at all")
        for line in bindings:
            for binding in line.split("--set-secrets=", 1)[1].split("\\")[0].split(","):
                binding = binding.strip()
                if binding:
                    self.assertTrue(binding.startswith("/"),
                                    "%r binds a secret to an env var, not a file" % binding)

    def test_no_secret_value_is_read_from_the_environment_by_the_bridge(self):
        for forbidden in ("REDIS_AUTH_STRING", "REDIS_PASSWORD", "AUTH_STRING"):
            self.assertNotIn(forbidden, SOURCE)


class TheDeployScriptIsCorrectNotJustSafe(unittest.TestCase):
    """Two real deploy bugs in the first version of deploy.sh, both invisible to every auth test."""

    def test_the_private_address_is_not_committed(self):
        """Cloud Run must refuse an unauthenticated request before it reaches the container. Two
        independent gates: --no-allow-unauthenticated is the one the deploy script owns."""
        import re
        self.assertIsNone(re.search(r"\b10\.\d+\.\d+\.\d+\b", DEPLOY),
                          "a private address is written into a public repository")
        self.assertIn("${REDIS_HOST}", DEPLOY)

    def test_set_env_vars_appears_ONCE(self):
        """ASSERTED AS A PROPERTY OF THE MEMBERS, not as the absence of a word.

        The first version forbade the string "allUsers" anywhere in deploy.sh - and failed, because
        the script's own readback checklist tells a human to look for exactly that. Grepping a file
        for a word finds the warning about the word. So: every principal this script would grant
        must be a NAMED service account."""
        flags = [line for line in DEPLOY.splitlines() if "--set-env-vars=" in line]
        self.assertEqual(1, len(flags), flags)
        for name in ("REDIS_HOST", "REDIS_PORT", "REDIS_AUTH_FILE", "REDIS_CA_CERT_PATH",
                     "BRIDGE_AUDIENCE", "BRIDGE_CALLERS"):
            self.assertIn(name + "=", flags[0], "%s is missing from the one env flag" % name)

    def test_a_deploy_without_REDIS_HOST_refuses(self):
        self.assertIn('if [ -z "${REDIS_HOST:-}" ]', DEPLOY)


class ItCannotDoAnythingButProbe(unittest.TestCase):
    """main.py's own Redis calls are the probe's and nothing else. On /probe the caller selects no
    key, no command, no namespace and no TTL. Absence, not refusal. What /op may do is decided in
    scripts/redis_gov.py, and TheOpEndpoint below holds op.py to making no Redis call of its own."""

    def test_there_is_no_arbitrary_command_surface(self):
        for forbidden in ("KEYS", "SCAN", "FLUSHDB", "FLUSHALL", "eval(", "exec(",
                          "execute_command", "getattr"):
            self.assertNotIn(forbidden, CODE,
                             "%s is a command surface this bridge must not have" % forbidden)
            self.assertNotIn(forbidden, statements(OP_SOURCE),
                             "%s is a command surface op.py must not have" % forbidden)

    def test_the_only_redis_calls_are_the_five_the_probe_makes(self):
        """set-secrets with a BARE NAME binds an env var; with a PATH it mounts a file. The
        difference is one character and it is the whole defect."""
        self.assertEqual(EXPECTED_REDIS_METHODS, redis_methods(SOURCE))

    def test_the_method_guard_detects_an_added_method(self):
        mutated = SOURCE + '\nconn.set("unexpected", "value")\n'
        with self.assertRaises(AssertionError):
            self.assertEqual(EXPECTED_REDIS_METHODS, redis_methods(mutated))

    def test_the_method_guard_cannot_pass_on_no_calls(self):
        with self.assertRaises(AssertionError):
            self.assertEqual(EXPECTED_REDIS_METHODS, redis_methods("pass"))

    def test_the_probe_key_is_the_servers(self):
        self.assertIn("PROBE_NAMESPACE + secrets.token_hex", SOURCE)

    def test_the_request_body_is_read_and_discarded(self):
        self.assertIn("DISCARD", SOURCE)

    def test_exactly_three_endpoints_exist(self):
        """Was "only two" until /op (2026-10-13). The guard is now exact in BOTH directions: every
        string main.py compares the path with, read from the syntax tree, is one of these three, so
        a fourth endpoint fails here whatever its spelling."""
        compared = set()
        for node in ast.walk(ast.parse(SOURCE)):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path":
                for right in node.comparators:
                    for leaf in ast.walk(right):
                        if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                            compared.add(leaf.value)
        self.assertEqual({"/healthz", "/probe", "/op"}, compared)

    def test_only_the_op_endpoint_keeps_its_body(self):
        flat = CODE.replace(chr(10), "")
        self.assertEqual(1, flat.count("raw=self.rfile.read(declared)"),
                         "exactly one read keeps the bytes, and it is /op's")
        self.assertEqual(2, flat.count("self.rfile.read("), "one kept read and one discarded read")

    def test_the_probe_cleans_up_and_says_whether_it_did(self):
        conn = FakeRedis()
        out = bridge.synthetic_probe(conn)
        self.assertEqual("OK", out["status"])
        self.assertEqual("verified", out["steps"]["cleanup"])
        self.assertEqual({}, conn.store, "the probe left a key behind")

    def test_a_probe_that_cannot_clean_up_is_not_OK(self):
        conn = FakeRedis(refuse_delete=True)
        out = bridge.synthetic_probe(conn)
        self.assertNotEqual("OK", out["status"])

    def test_a_store_error_reports_the_TYPE_and_not_the_message(self):
        conn = FakeRedis(raise_on_set=RuntimeError("auth string is hunter2"))
        out = bridge.synthetic_probe(conn)
        self.assertEqual("ERROR", out["status"])
        self.assertEqual("RuntimeError", out["failed_with"])
        self.assertNotIn("hunter2", repr(out))


class EveryFileTheImageCopiesIsUploadedToCloudBuild(unittest.TestCase):
    """The missing-COPY/missing-allowlist bug, which has now cost four builds.

    tests/test_image_layout.py does this - for cloud/bus-reconciler/Dockerfile ONLY, hardcoded. Its
    green tick says nothing about this directory, so running it and feeling covered would be the
    same mistake as the guard that parsed one agenda section while the chair parsed every one. The
    assertion belongs beside the Dockerfile it is about.

    /.gcloudignore is an ALLOWLIST: it ignores `/*` and then re-admits, directory by directory. A
    COPY of a path nobody re-admitted builds fine locally and fails in Cloud Build, where the file
    simply is not there.
    """

    def test_each_copy_source_is_admitted_by_the_allowlist(self):
        """cloud/bus-reconciler/main.py: the instance is 'never committed to this public repository'."""
        sys.path.insert(0, str(ROOT / "tests"))
        import test_image_layout as layout

        rules = layout.gcloudignore_rules((ROOT / ".gcloudignore").read_text(encoding="utf-8"))
        sources = [line.split()[1] for line in DOCKERFILE.splitlines()
                   if line.startswith("COPY ") and len(line.split()) >= 3]
        self.assertTrue(sources, "the Dockerfile COPYs nothing, which cannot be right")
        for src in sources:
            self.assertTrue(layout.in_build_context(src, rules),
                            "COPY %s is excluded by .gcloudignore, so Cloud Build will not receive "
                            "it and the build will fail on a file that exists locally" % src)

    def test_the_file_each_copy_names_actually_exists(self):
        for line in DOCKERFILE.splitlines():
            if line.startswith("COPY ") and len(line.split()) >= 3:
                src = line.split()[1]
                self.assertTrue((ROOT / src).exists(), "COPY %s does not exist in the repo" % src)


class TheLogsAndRepliesLeakNothing(unittest.TestCase):
    def test_request_logging_does_not_emit_query_tokens(self):
        handler = object.__new__(bridge.Handler)
        handler.requestline = "GET /healthz?token=FAKE_SECRET_MARKER HTTP/1.1"
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            handler.log_request(200, 99)
        self.assertEqual("", output.getvalue())

    def test_error_logging_does_not_emit_request_details(self):
        handler = object.__new__(bridge.Handler)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            handler.log_error("Bad request: %s", "FAKE_SECRET_MARKER")
        self.assertEqual("", output.getvalue())

    def test_the_default_request_logger_is_overridden(self):
        """--set-env-vars REPLACES the environment. Repeated, only the last survives - which would
        have shipped a bridge with no audience, no host and no CA path."""
        self.assertIn("def log_message", SOURCE)
        self.assertIn("NO REQUEST LINE", SOURCE)

    def test_healthz_touches_nothing(self):
        where = SOURCE.index('path == "/healthz"')
        block = SOURCE[where:where + 500]
        for forbidden in ("connect(", "synthetic_probe", "allowlist()", "audience()"):
            self.assertNotIn(forbidden, block,
                             "a health check that reveals state is a disclosure")


# ---------------------------------------------------------------- doubles


class RuntimeTransportIsDeclared(unittest.TestCase):
    def test_requests_extra_and_clean_image_import_smoke_are_present(self):
        requirements = (ROOT / "cloud" / "redis-tool-bridge" / "requirements.txt").read_text()
        self.assertIn("google-auth[requests]>=2.35,<3", requirements.splitlines())
        self.assertIn("from google.auth.transport.requests import Request", DOCKERFILE)


class Verifier:
    """Stand in for google.oauth2.id_token so the authorisation half can be tested without a real
    Google token. It does NOT stand in for the audience check - that is asserted against the source,
    because a fake verifier that ignores the audience would pass either way."""

    def __init__(self, claims):
        self.claims = claims
        self.saved = {}

    def __enter__(self):
        import types
        oauth2 = types.ModuleType("google.oauth2")
        id_token = types.ModuleType("google.oauth2.id_token")
        transport = types.ModuleType("google.auth.transport")
        requests_mod = types.ModuleType("google.auth.transport.requests")

        def verify_oauth2_token(token, request, audience=None):
            if not audience:
                raise AssertionError("called without an audience: that is defect 1")
            return self.claims

        id_token.verify_oauth2_token = verify_oauth2_token
        requests_mod.Request = lambda *a, **k: object()
        for name, mod in (("google.oauth2", oauth2), ("google.oauth2.id_token", id_token),
                          ("google.auth.transport", transport),
                          ("google.auth.transport.requests", requests_mod)):
            self.saved[name] = sys.modules.get(name)
            sys.modules[name] = mod
        sys.modules["google.oauth2"].id_token = id_token
        sys.modules["google.auth.transport"].requests = requests_mod
        return self

    def __exit__(self, *exc):
        for name, mod in self.saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        return False


class FakeRedis:
    def __init__(self, refuse_delete=False, raise_on_set=None):
        self.store = {}
        self.refuse_delete = refuse_delete
        self.raise_on_set = raise_on_set

    def setex(self, key, ttl, value):
        if self.raise_on_set:
            raise self.raise_on_set
        self.store[key] = (value, ttl)

    def get(self, key):
        found = self.store.get(key)
        return found[0] if found else None

    def ttl(self, key):
        found = self.store.get(key)
        return found[1] if found else -2

    def delete(self, key):
        if not self.refuse_delete:
            self.store.pop(key, None)



try:
    import redis as REDIS_LIB                         # installed in CI's bridge venv, pinned 5.3.1
except ImportError:                                   # pragma: no cover - this laptop has no redis
    REDIS_LIB = None


@unittest.skipUnless(REDIS_LIB, "the redis library is not installed here; CI's bridge venv has it")
class TheRealRedisClientTrustsOnlyTheMountedCA(unittest.TestCase):
    """Cursor, PR 340, FAIL on 36479378, finding 1, against the library the image actually pins.

    redis-py 5.3.1's SSLConnection builds ssl.create_default_context() - the public bundle - and
    then ADDS the mounted CA, with ssl_check_hostname defaulting to False. The previous test here
    only asserted that redis_dual.py contained the text '"ssl_cert_reqs": "required"', which stayed
    true while any publicly-chained certificate was accepted. This one drives the real pool to the
    point of the handshake and reads the context it would use."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import test_redis_dual as dual_tests
        self.t = dual_tests

    def test_the_handshake_trusts_the_mounted_ca_and_nothing_else(self):
        conn = self.t.connect_with(REDIS_LIB)
        self.assertIsInstance(conn.connection_pool.make_connection(),
                              REDIS_LIB.connection.SSLConnection)
        seen, ctx, server_hostname = self.t.first_handshake(self, conn)
        self.assertEqual([], seen.default_loads,
                         "the public CA bundle was loaded: the mounted CA must REPLACE it")
        self.assertEqual([self.t.TEST_CA_CN], self.t.trusted_subjects(ctx))
        self.assertEqual(ssl.CERT_REQUIRED, ctx.verify_mode)
        self.assertFalse(ctx.check_hostname, "hostname matching is off until the SAN is read")
        self.assertEqual("10.0.0.1", server_hostname)

    def test_the_switch_turns_hostname_matching_on_in_the_real_client(self):
        conn = self.t.connect_with(REDIS_LIB, environ={"REDIS_TLS_CHECK_HOSTNAME": "1"})
        seen, ctx, _ = self.t.first_handshake(self, conn)
        self.assertTrue(ctx.check_hostname)
        self.assertEqual([], seen.default_loads)


class EmailVerifiedMustBeBooleanTrue(unittest.TestCase):
    """Cursor, PR 340, finding 2: `claims.get("email_verified", True)` failed OPEN - a missing claim,
    the string "false", the string "true" and the integer 1 were all accepted. Only boolean True is
    what a Google service-account ID token carries, and only boolean True gets in."""

    def verdict(self, claims):
        with Verifier(claims):
            return bridge.verify_caller("tok", AUD, frozenset({CALLER}))

    def test_everything_but_boolean_true_is_refused(self):
        for label, claims in (("missing", {"email": CALLER}),
                              ("'false'", {"email": CALLER, "email_verified": "false"}),
                              ("'true'", {"email": CALLER, "email_verified": "true"}),
                              ("1", {"email": CALLER, "email_verified": 1}),
                              ("None", {"email": CALLER, "email_verified": None}),
                              ("False", {"email": CALLER, "email_verified": False})):
            who, why = self.verdict(claims)
            self.assertIsNone(who, "email_verified=%s was accepted" % label)
            self.assertEqual("unauthenticated", why, label)

    def test_boolean_true_is_accepted(self):
        self.assertEqual((CALLER, None), self.verdict({"email": CALLER, "email_verified": True}))


class AMisconfiguredBridgeSaysNothingAboutWhy(unittest.TestCase):
    """Cursor, PR 340, residual: the 503 body was `why if code == 503 else why` - `why` on both
    arms - so the caller was told which setting was missing. The detail belongs in our log."""

    def post(self, environ):
        handler = object.__new__(bridge.Handler)
        handler.path = "/probe"
        handler.headers = {"Content-Length": "0", "Authorization": "Bearer tok"}
        handler.rfile = io.BytesIO(b"")
        sent = []
        handler._send = lambda code, body: sent.append((code, body))
        saved = {k: os.environ.pop(k, None) for k in ("BRIDGE_AUDIENCE", "BRIDGE_CALLERS")}
        os.environ.update(environ)
        log = io.StringIO()
        try:
            with contextlib.redirect_stderr(log):
                handler.do_POST()
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(1, len(sent))
        return sent[0], log.getvalue()

    def test_no_audience_is_a_generic_503_and_a_specific_log_line(self):
        (code, body), log = self.post({"BRIDGE_CALLERS": CALLER})
        self.assertEqual(503, code)
        self.assertEqual({"error": bridge.UNAVAILABLE}, body)
        self.assertNotIn("audience", repr(body))
        self.assertIn("audience", log)

    def test_no_allowlist_is_a_generic_503_and_a_specific_log_line(self):
        (code, body), log = self.post({"BRIDGE_AUDIENCE": AUD})
        self.assertEqual(503, code)
        self.assertEqual({"error": bridge.UNAVAILABLE}, body)
        self.assertNotIn("allowlist", repr(body))
        self.assertIn("allowlist", log)

    def test_a_caller_failure_is_still_401_or_403_not_503(self):
        with Verifier({"email": CALLER, "email_verified": "true"}):
            (code, body), _ = self.post({"BRIDGE_AUDIENCE": AUD, "BRIDGE_CALLERS": CALLER})
        self.assertEqual((401, {"error": "unauthenticated"}), (code, body))
        with Verifier({"email": "other@example.invalid", "email_verified": True}):
            (code, body), _ = self.post({"BRIDGE_AUDIENCE": AUD, "BRIDGE_CALLERS": CALLER})
        self.assertEqual((403, {"error": "not authorised"}), (code, body))


sys.path.insert(0, str(ROOT / "tests"))
import op as bridge_op  # noqa: E402
import redis_gov  # noqa: E402
import test_redis_gov as gov_tests  # noqa: E402  (its FakeRedis has Redis's real semantics)

AYA = "aya-runtime@sfdc24.iam.gserviceaccount.com"


class TheOpEndpoint(unittest.TestCase):
    """POST /op, docs/REDIS-PRIMARY.md Phase 3a: every failure has its own status and says why,
    and the two layers the bridge adds - the HTTP surface and the caller-to-principal map - are
    the ones under test. What redis_gov allows is tests/test_redis_gov.py's business."""

    def post(self, body, claims=None, conn=None, connect=None, token="tok", path="/op",
             callers=None, length=None):
        handler = object.__new__(bridge.Handler)
        handler.path = path
        raw = body if isinstance(body, bytes) else __import__("json").dumps(body).encode("utf-8")
        handler.headers = {"Content-Length": str(len(raw) if length is None else length)}
        if token:
            handler.headers["Authorization"] = "Bearer " + token
        handler.rfile = io.BytesIO(raw)
        sent = []
        handler._send = lambda code, out: sent.append((code, out))
        self.conn = conn if conn is not None else gov_tests.FakeRedis()
        saved_env = {k: os.environ.get(k) for k in ("BRIDGE_AUDIENCE", "BRIDGE_CALLERS")}
        os.environ.update({"BRIDGE_AUDIENCE": AUD, "BRIDGE_CALLERS": AYA})
        saved_connect, saved_callers = bridge.connect, bridge_op.load_callers
        bridge.connect = connect or (lambda: self.conn)
        if callers is not None:
            bridge_op.load_callers = lambda path=None: callers
        log = io.StringIO()
        try:
            with Verifier(claims if claims is not None else {"email": AYA, "email_verified": True}), \
                    contextlib.redirect_stderr(log):
                handler.do_POST()
        finally:
            bridge.connect, bridge_op.load_callers = saved_connect, saved_callers
            for k, v in saved_env.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(1, len(sent), "exactly one response per request")
        self.log = log.getvalue()
        return sent[0]

    @staticmethod
    def b64(text):
        import base64
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    # --- the happy path, and whose name the audit carries
    def test_a_verified_caller_writes_and_reads_back_with_no_board_row(self):
        code, out = self.post({"op": "xadd", "key": "fleet:inbox:grok",
                               "val": self.b64("hello from the bridge"), "enc": "b64"})
        self.assertEqual(200, code, out)
        self.assertTrue(out["ok"])
        self.assertEqual((AYA, "chatgpt-codex-desktop"), (out["caller"], out["principal"]))
        conn = self.conn
        code, out = self.post({"op": "xrange", "key": "fleet:inbox:grok", "count": 5}, conn=conn)
        self.assertEqual(200, code, out)
        self.assertIn("hello from the bridge", repr(out))

    def test_the_audit_names_the_verified_caller_not_a_claimed_tag(self):
        self.post({"op": "set", "key": "fleet:test:bridge", "val": self.b64("v"), "enc": "b64"})
        entry = self.conn.audit()[-1]
        self.assertEqual("chatgpt-codex-desktop", entry["actor"])
        self.assertEqual("bridge:" + AYA, entry["via"])
        self.assertEqual("1", entry["ok"])

    def test_the_body_cannot_choose_who_it_acts_as(self):
        """There is no member for it: a caller that sends one is told the request is bad."""
        code, out = self.post({"op": "get", "key": "fleet:x", "tag": "owner"})
        self.assertEqual((400, "bad request"), (code, out["error"]))
        code, out = self.post({"op": "get", "key": "fleet:x", "principal": "owner"})
        self.assertEqual(400, code)
        self.assertEqual([], self.conn.audit(), "a malformed request reached Redis")

    # --- 401 and 403 from the HTTP surface: the same gate as the probe, on the new endpoint
    def test_no_token_is_401_and_nothing_is_touched(self):
        code, out = self.post({"op": "get", "key": "fleet:x"}, token="")
        self.assertEqual((401, {"error": "unauthenticated"}), (code, out))
        self.assertEqual([], self.conn.audit())

    def test_an_unverified_email_is_401(self):
        code, out = self.post({"op": "get", "key": "fleet:x"},
                              claims={"email": AYA, "email_verified": "true"})
        self.assertEqual(401, code)

    def test_a_verified_identity_off_the_allowlist_is_403_and_nothing_is_touched(self):
        code, out = self.post({"op": "get", "key": "fleet:x"},
                              claims={"email": "someone@example.invalid", "email_verified": True})
        self.assertEqual((403, {"error": "not authorised"}), (code, out))
        self.assertEqual([], self.conn.audit())

    def test_op_runs_the_same_verifier_as_the_probe(self):
        flat = CODE.replace(chr(10), "")
        self.assertEqual(1, flat.count("=verify_caller("), "one verification call, shared")
        self.assertLess(flat.index("=verify_caller("), flat.index("op.handle("),
                        "the caller is verified before op.handle is reached")

    # --- 403 refused: redis_gov's reason, and its audit entry
    def test_a_write_to_a_protected_namespace_is_403_refused_with_the_reason(self):
        code, out = self.post({"op": "set", "key": "gov:audit", "val": self.b64("x"), "enc": "b64"})
        self.assertEqual(403, code, out)
        self.assertEqual("refused", out["error"])
        self.assertTrue(out["reason"], "the refusal must say why")
        self.assertEqual("0", self.conn.audit()[-1]["ok"], "the refusal is audited")

    def test_an_op_outside_the_grammar_is_403_refused_not_executed(self):
        code, out = self.post({"op": "flushall", "key": "fleet:x"})
        self.assertEqual((403, "refused"), (code, out["error"]))

    # --- 503: Redis unreachable, before and during
    def test_no_connection_is_503_and_the_message_is_not_echoed(self):
        def down():
            raise ConnectionError("AUTH hunter2 failed")
        code, out = self.post({"op": "get", "key": "fleet:x"}, connect=down)
        self.assertEqual((503, "redis unreachable"), (code, out["error"]))
        self.assertIs(False, out["executed"], "no connection was made, and the answer must say so")
        self.assertNotIn("hunter2", repr(out) + self.log)
        self.assertIn("ConnectionError", self.log, "the TYPE reaches our log")

    def test_a_read_that_fails_mid_operation_is_502_failed_never_unreachable(self):
        class Dropping(gov_tests.FakeRedis):
            def get(self, key):
                raise ConnectionError("reset by peer")
        code, out = self.post({"op": "get", "key": "fleet:x"}, conn=Dropping())
        self.assertEqual((502, "failed", "ConnectionError"), (code, out["error"], out["type"]))
        self.assertNotIn("executed", out, "a 502 never promises that nothing ran")

    # --- "executed": false is a promise; its absence is not one (Aya on 919fd52)
    class CommitsThenDrops(gov_tests.FakeRedis):
        """The transaction RUNS and the reply is lost: the committed-write-with-a-lost-response case."""

        def pipeline(self, transaction=True):
            pipe = gov_tests.FakePipeline(self)
            real = pipe.execute

            def execute(raise_on_error=True):
                real(raise_on_error)
                raise ConnectionError("the reply was lost after EXEC")
            pipe.execute = execute
            return pipe

    def test_a_write_whose_reply_is_lost_is_OUTCOME_UNKNOWN_and_it_did_commit(self):
        conn = self.CommitsThenDrops()
        code, out = self.post({"op": "xadd", "key": "fleet:inbox:grok",
                               "val": self.b64("only once, please"), "enc": "b64"}, conn=conn)
        self.assertEqual((502, "outcome unknown", "ConnectionError"), (code, out["error"], out["type"]))
        self.assertNotIn("executed", out, "the bridge must not say a committed write did not run")
        self.assertNotEqual("redis unreachable", out["error"],
                            "the words for a connection never made must not describe this")
        stored = [fields for _, fields in conn.data.get("fleet:inbox:grok", [])]
        self.assertEqual(1, len(stored), "the write DID commit: a client that resends it writes twice")
        self.assertIn("OUTCOME UNKNOWN", self.log)

    def test_every_answer_for_a_request_that_never_reached_redis_gov_says_so(self):
        def down():
            raise ConnectionError("no route")
        cases = (
            ("no connection", dict(body={"op": "get", "key": "fleet:x"}, connect=down), 503),
            ("bad body", dict(body=b"not json"), 400),
            ("principal mismatch", dict(body={"op": "get", "key": "fleet:x", "expect": "owner"}), 403),
            ("unmapped caller", dict(body={"op": "get", "key": "fleet:x"}, callers={}), 503),
        )
        for name, kwargs, status in cases:
            code, out = self.post(**kwargs)
            self.assertEqual(status, code, name)
            self.assertIs(False, out.get("executed"), "%s must carry executed: false" % name)
            self.assertEqual([], self.conn.audit(), "%s reached Redis" % name)

    def test_a_misconfigured_bridge_says_executed_false_on_op_and_nothing_new_on_probe(self):
        saved = {k: os.environ.pop(k, None) for k in ("BRIDGE_AUDIENCE", "BRIDGE_CALLERS")}
        try:
            for path, expected in (("/op", {"error": bridge.UNAVAILABLE, "executed": False}),
                                   ("/probe", {"error": bridge.UNAVAILABLE})):
                handler = object.__new__(bridge.Handler)
                handler.path = path
                handler.headers = {"Content-Length": "0", "Authorization": "Bearer tok"}
                handler.rfile = io.BytesIO(b"")
                sent = []
                handler._send = lambda code, body: sent.append((code, body))
                with contextlib.redirect_stderr(io.StringIO()):
                    handler.do_POST()
                self.assertEqual([(503, expected)], sent, path)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_the_reads_are_exactly_redis_govs(self):
        self.assertEqual(tuple(redis_gov.OPS_READ), bridge_op.READS)

    # --- the principal binding: expect is a check, never a choice
    def test_expect_that_matches_the_bound_principal_changes_nothing(self):
        code, out = self.post({"op": "set", "key": "fleet:test:x", "val": self.b64("v"), "enc": "b64",
                               "expect": "chatgpt-codex-desktop"})
        self.assertEqual(200, code, out)
        self.assertEqual("chatgpt-codex-desktop", self.conn.audit()[-1]["actor"])

    def test_expect_that_does_not_match_is_refused_before_anything_runs(self):
        """A client set up with another agent's account must not write under that agent's name."""
        connected = []

        def connect():
            connected.append(1)
            return self.conn

        code, out = self.post({"op": "xadd", "key": "fleet:inbox:grok", "val": self.b64("as someone else"),
                               "enc": "b64", "expect": "claude-code-cli"}, connect=connect)
        self.assertEqual((403, "principal mismatch"), (code, out["error"]))
        self.assertIs(False, out["executed"])
        self.assertEqual([], connected, "no connection is made for a mismatched principal")
        self.assertEqual([], self.conn.audit())
        self.assertNotIn("fleet:inbox:grok", self.conn.data)

    def test_expect_cannot_select_a_principal(self):
        """Naming a more powerful principal gets a refusal, never that principal."""
        for wanted in ("owner", "milestones-sync", "bus-reconciler"):
            code, out = self.post({"op": "set", "key": "fleet:test:x", "val": self.b64("v"),
                                   "enc": "b64", "expect": wanted})
            self.assertEqual((403, "principal mismatch"), (code, out["error"]), wanted)
        self.assertEqual([], self.conn.audit())

    # --- a refusal whose own audit entry failed is still a refusal, and says its audit failed
    def test_a_refusal_with_a_failed_audit_keeps_both_facts(self):
        class AuditDown(gov_tests.FakeRedis):
            def xadd(self, key, fields, maxlen=None, approximate=None):
                if key == redis_gov.AUDIT_STREAM:
                    raise ConnectionError("audit stream unreachable")
                return super().xadd(key, fields, maxlen=maxlen, approximate=approximate)
        code, out = self.post({"op": "set", "key": "gov:audit", "val": self.b64("x"), "enc": "b64"},
                              conn=AuditDown())
        self.assertEqual((403, "refused"), (code, out["error"]))
        self.assertEqual("FAILED:ConnectionError", out["audit"], "the partial audit must be visible")
        self.assertIs(False, out["executed"], "a refused operation did not run")

    def test_another_failure_inside_redis_is_502_with_the_type_only(self):
        class Broken(gov_tests.FakeRedis):
            def get(self, key):
                raise RuntimeError("value was hunter2")
        code, out = self.post({"op": "get", "key": "fleet:x"}, conn=Broken())
        self.assertEqual((502, "failed", "RuntimeError"), (code, out["error"], out["type"]))
        self.assertNotIn("hunter2", repr(out) + self.log)

    # --- 400 and 413: the body
    def test_a_body_that_is_not_the_object_is_400(self):
        for bad in (b"", b"not json", b"[1,2]", b'"text"', b'{"op": "get"}', b'{"key": "fleet:x"}',
                    b'{"op": 1, "key": "fleet:x"}', b'{"op": "xrange", "key": "fleet:x", "count": "5"}',
                    b'{"op": "xrange", "key": "fleet:x", "count": true}'):
            code, out = self.post(bad)
            self.assertEqual((400, {"error": "bad request"}), (code, {"error": out["error"]}), bad)
            self.assertEqual([], self.conn.audit(), bad)

    def test_an_oversize_body_is_413_and_is_never_read(self):
        code, out = self.post(b"x", length=bridge.OP_MAX_BODY_BYTES + 1)
        self.assertEqual((413, "body too large"), (code, out["error"]))
        self.assertEqual([], self.conn.audit())

    def test_the_two_statements_of_the_bound_agree(self):
        self.assertEqual(bridge_op.MAX_BODY_BYTES, bridge.OP_MAX_BODY_BYTES)

    def test_the_probe_still_discards_its_body(self):
        conn = FakeRedis()
        code, out = self.post(b'{"op": "set", "key": "fleet:x"}', path="/probe", conn=conn)
        self.assertEqual("OK", out["status"])
        self.assertEqual({}, conn.store, "the probe acted on a body it must discard")

    # --- the caller-to-principal map
    def test_an_allowlisted_caller_with_no_principal_is_our_503_and_a_loud_log(self):
        code, out = self.post({"op": "get", "key": "fleet:x"}, callers={})
        self.assertEqual((503, {"error": "service unavailable"}), (code, {"error": out["error"]}))
        self.assertIn("no principal in callers.json", self.log)
        self.assertEqual([], self.conn.audit())

    def test_every_mapped_caller_is_a_principal_that_can_write_fleet_and_conf(self):
        """The typo that would lock an agent out, caught before a deploy (REDIS-PRIMARY 8.8)."""
        acl = redis_gov.load_acl()
        callers = bridge_op.load_callers()
        self.assertTrue(callers, "callers.json maps nobody, so /op would refuse everyone")
        for email, who in callers.items():
            self.assertRegex(email, r"^[a-z0-9-]+@sfdc24\.iam\.gserviceaccount\.com$",
                             "a caller is one of this project's service accounts, by full email")
            self.assertEqual(who, redis_gov.principal(acl, who), "%s is not a principal" % who)
            for key in ("fleet:inbox:probe", "conf:side:20261013"):
                redis_gov.check(acl, who, "xadd", key)      # raises Refused if it may not write

    def test_every_caller_the_deploy_admits_has_a_principal(self):
        import re
        admitted = re.search(r"BRIDGE_CALLERS=([^\s\\]+)", DEPLOY).group(1)
        for email in admitted.replace("${PROJECT}", "sfdc24").split(","):
            self.assertIn(email.strip().lower(), bridge_op.load_callers(),
                          "deploy.sh admits %s but callers.json gives it no principal" % email)

    def test_a_missing_or_malformed_map_maps_nobody(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "absent.json")
            self.assertEqual({}, bridge_op.load_callers(missing))
            for text in ("not json", "[]", '{"callers": []}', "{}"):
                bad = os.path.join(tmp, "bad.json")
                pathlib.Path(bad).write_text(text, encoding="utf-8")
                self.assertEqual({}, bridge_op.load_callers(bad), text)

    # --- op.py is a door, not a second governor
    def test_op_makes_no_redis_call_of_its_own(self):
        self.assertEqual(set(), redis_methods(OP_SOURCE))
        self.assertEqual(1, statements(OP_SOURCE).count("gov\n.\nexecute\n("),
                         "exactly one governed call")

    def test_no_value_and_no_key_reaches_the_log(self):
        self.post({"op": "set", "key": "fleet:secretkeyname", "val": self.b64("hunter2"), "enc": "b64"})
        self.assertNotIn("hunter2", self.log)
        self.assertNotIn("secretkeyname", self.log)
        self.assertIn("set ok", self.log)

    def test_the_image_carries_the_governed_module_the_access_list_and_the_map(self):
        for needed in ("COPY scripts/redis_gov.py /app/redis_gov.py",
                       "COPY scripts/redis_acl.json /app/redis_acl.json",
                       "COPY cloud/redis-tool-bridge/callers.json /app/callers.json",
                       "COPY cloud/redis-tool-bridge/op.py /app/op.py"):
            self.assertIn(needed, DOCKERFILE)

    def test_the_deploy_script_still_refuses_to_be_run(self):
        """D1 is not given. Until it is, the script keeps its own warning."""
        self.assertIn("DO NOT RUN THIS YET", DEPLOY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
