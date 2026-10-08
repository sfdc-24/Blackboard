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
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "redis-tool-bridge"))
sys.path.insert(0, str(ROOT / "scripts"))

import main as bridge  # noqa: E402

AUD = "https://redis-tool-bridge-yzet4vuplq-uc.a.run.app"
CALLER = "aya-runtime@sfdc24.iam.gserviceaccount.com"
SOURCE = (ROOT / "cloud" / "redis-tool-bridge" / "main.py").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "cloud" / "redis-tool-bridge" / "Dockerfile").read_text(encoding="utf-8")
DEPLOY = (ROOT / "cloud" / "redis-tool-bridge" / "deploy.sh").read_text(encoding="utf-8")


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


class Defect1TheAudienceIsChecked(unittest.TestCase):
    """An absent audience must refuse every request, NOT verify without one."""

    def test_no_configured_audience_refuses(self):
        who, why = bridge.verify_caller("any.token", "", frozenset({CALLER}))
        self.assertIsNone(who)
        self.assertIn("audience", why)

    def test_the_verifier_is_called_with_an_audience_argument(self):
        """The defect was ONE MISSING ARGUMENT, and no behavioural test can see it without a real
        Google token. So the source is the evidence: the call must pass audience=."""
        where = SOURCE.index("verify_oauth2_token(")
        call = SOURCE[where:where + 220]
        self.assertIn("audience=", call,
                      "verify_oauth2_token without audience= is defect 1, exactly")

    def test_the_audience_is_not_defaulted(self):
        """A default audience would be worse than none: it would look configured and check the
        wrong thing. os.environ.get must not supply a fallback URL."""
        self.assertNotIn('BRIDGE_AUDIENCE", "http', SOURCE)
        self.assertNotIn("BRIDGE_AUDIENCE', 'http", SOURCE)


class Defect2ServerIdentityVerificationStaysOn(unittest.TestCase):
    def test_the_bridge_does_not_implement_its_own_tls(self):
        """It borrows scripts/redis_dual.py. A private client here is how one process ends up with
        verification off while the other has it on."""
        for forbidden in ("ssl_cert_reqs", "check_hostname", "checkServerIdentity",
                          "CERT_NONE", "SSLContext", "verify=False"):
            self.assertNotIn(forbidden, CODE,
                             "%s in the bridge means it is making its own TLS decisions" % forbidden)

    def test_it_connects_through_redis_dual(self):
        self.assertIn("redis_dual", CODE)
        self.assertIn("client", CODE)
        self.assertIn("redis_dual.client(", SOURCE)

    def test_redis_dual_still_requires_a_verified_server(self):
        """The borrowed path is only a defence while it still verifies. Asserted against the module
        this image actually copies, not against a comment about it."""
        dual = (ROOT / "scripts" / "redis_dual.py").read_text(encoding="utf-8")
        self.assertIn('"ssl_cert_reqs": "required"', dual)
        self.assertIn("ssl_ca_certs", dual)

    def test_the_image_copies_the_shared_client_and_its_settings(self):
        self.assertIn("COPY scripts/redis_dual.py", DOCKERFILE)
        self.assertIn("COPY scripts/redis_dual.settings.json", DOCKERFILE)


class Defect3AuthenticationIsNotAuthorisation(unittest.TestCase):
    def test_no_configured_allowlist_refuses(self):
        who, why = bridge.verify_caller("any.token", AUD, frozenset())
        self.assertIsNone(who)
        self.assertIn("allowlist", why)

    def test_a_verified_identity_off_the_allowlist_is_refused(self):
        """THE DEFECT ITSELF. A real, verified, Google-signed token for a real service account that
        nobody authorised must not get in."""
        claims = {"email": "someone-else@sfdc24.iam.gserviceaccount.com", "email_verified": True}
        with Verifier(claims):
            who, why = bridge.verify_caller("tok", AUD, frozenset({CALLER}))
        self.assertIsNone(who, "a verified identity is not an authorised one")
        self.assertEqual("not authorised", why)

    def test_the_allowlisted_caller_is_accepted_and_named(self):
        """The control. Without this the tests above would pass on a bridge that refuses everyone."""
        with Verifier({"email": CALLER, "email_verified": True}):
            who, why = bridge.verify_caller("tok", AUD, frozenset({CALLER}))
        self.assertEqual(CALLER, who)
        self.assertIsNone(why)

    def test_an_unverified_email_claim_is_refused(self):
        with Verifier({"email": CALLER, "email_verified": False}):
            who, why = bridge.verify_caller("tok", AUD, frozenset({CALLER}))
        self.assertIsNone(who)

    def test_the_allowlist_is_case_insensitive_but_not_substring(self):
        os.environ["BRIDGE_CALLERS"] = "AYA-Runtime@sfdc24.iam.gserviceaccount.com, other@x.com"
        try:
            self.assertIn(CALLER, bridge.allowlist())
            self.assertNotIn("aya-runtime@sfdc24.iam.gserviceaccount.com.evil.com",
                             bridge.allowlist())
        finally:
            del os.environ["BRIDGE_CALLERS"]

    def test_the_platform_gate_is_demanded_too(self):
        """Cloud Run must refuse an unauthenticated request before it reaches the container. Two
        independent gates: --no-allow-unauthenticated is the one the deploy script owns."""
        self.assertIn("--no-allow-unauthenticated", DEPLOY)
        self.assertNotIn("--allow-unauthenticated", DEPLOY.replace("--no-allow-unauthenticated", ""))

    def test_the_invoker_is_never_everyone(self):
        """ASSERTED AS A PROPERTY OF THE MEMBERS, not as the absence of a word.

        The first version forbade the string "allUsers" anywhere in deploy.sh - and failed, because
        the script's own readback checklist tells a human to look for exactly that. Grepping a file
        for a word finds the warning about the word. So: every principal this script would grant
        must be a NAMED service account."""
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
        """set-secrets with a BARE NAME binds an env var; with a PATH it mounts a file. The
        difference is one character and it is the whole defect."""
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
        """cloud/bus-reconciler/main.py: the instance is 'never committed to this public repository'."""
        import re
        self.assertIsNone(re.search(r"\b10\.\d+\.\d+\.\d+\b", DEPLOY),
                          "a private address is written into a public repository")
        self.assertIn("${REDIS_HOST}", DEPLOY)

    def test_set_env_vars_appears_ONCE(self):
        """--set-env-vars REPLACES the environment. Repeated, only the last survives - which would
        have shipped a bridge with no audience, no host and no CA path."""
        flags = [line for line in DEPLOY.splitlines() if "--set-env-vars=" in line]
        self.assertEqual(1, len(flags), flags)
        for name in ("REDIS_HOST", "REDIS_PORT", "REDIS_AUTH_FILE", "REDIS_CA_CERT_PATH",
                     "BRIDGE_AUDIENCE", "BRIDGE_CALLERS"):
            self.assertIn(name + "=", flags[0], "%s is missing from the one env flag" % name)

    def test_a_deploy_without_REDIS_HOST_refuses(self):
        self.assertIn('if [ -z "${REDIS_HOST:-}" ]', DEPLOY)


class ItCannotDoAnythingButProbe(unittest.TestCase):
    """The caller selects no key, no command, no namespace and no TTL. Absence, not refusal."""

    def test_there_is_no_arbitrary_command_surface(self):
        for forbidden in ("KEYS", "SCAN", "FLUSHDB", "FLUSHALL", "eval(", "exec(",
                          "execute_command", "getattr"):
            self.assertNotIn(forbidden, CODE,
                             "%s is a command surface this bridge must not have" % forbidden)

    def test_the_only_redis_calls_are_the_five_the_probe_makes(self):
        """Named explicitly, so adding a sixth is a test change and therefore a review."""
        calls = {tok.split("(")[0] for tok in CODE.split("conn .")[1:]}
        self.assertEqual(set(), calls - {"setex", "get", "ttl", "delete"},
                         "an unexpected Redis call: %s" % sorted(calls))

    def test_the_probe_key_is_the_servers(self):
        self.assertIn("PROBE_NAMESPACE + secrets.token_hex", SOURCE)

    def test_the_request_body_is_read_and_discarded(self):
        self.assertIn("DISCARD", SOURCE)

    def test_only_two_endpoints_exist(self):
        self.assertIn('path == "/healthz"', SOURCE)
        self.assertIn('path != "/probe"', SOURCE)

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
        """THE MATCHER IS IMPORTED, NOT REWRITTEN, and the first version of this test is why.

        I wrote my own check: collect every `!` line and see whether the path or one of its
        ancestors appears. It passed with the negation for this directory DELETED - because
        `!/cloud` admits `cloud`, and my prefix walk stopped there and called it covered. It never
        saw the `/cloud/*` line that re-ignores everything inside. A permissive guard that reports
        OK is worse than no guard, and that is the third one of mine today.

        tests/test_image_layout.py already implements gitignore semantics correctly - last matching
        rule wins, every ancestor tested. Borrowing it means one description of the rules."""
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
    def test_the_default_request_logger_is_overridden(self):
        """BaseHTTPRequestHandler logs the request line. A bearer token in a query string would go
        straight into Cloud Logging."""
        self.assertIn("def log_message", SOURCE)
        self.assertIn("NO REQUEST LINE", SOURCE)

    def test_healthz_touches_nothing(self):
        where = SOURCE.index('path == "/healthz"')
        block = SOURCE[where:where + 500]
        for forbidden in ("connect(", "synthetic_probe", "allowlist()", "audience()"):
            self.assertNotIn(forbidden, block,
                             "a health check that reveals state is a disclosure")


# ---------------------------------------------------------------- doubles


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
