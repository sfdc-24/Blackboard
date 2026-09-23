#!/usr/bin/env python3
"""Offline contract tests for the Python WhatsApp notifier.

No Graph. No real .env. No network. Every credential test writes its own
throwaway env file and passes it explicitly, so this suite cannot read the
operator's token even by accident, and `send()` is never called.

WHAT THIS SUITE IS FOR
    scripts/wa_notify.py is scripts/wa_notify.ps1's contract in stdlib Python,
    written so the fleet's receipt path stops invoking powershell.exe - the
    fourth laptop dependency in docs/OPENAI-CLOUD-MIGRATION.md (which arrives
    with codex's PR #172; it is not on main yet). Two implementations of one
    contract drift, so the last class of tests here reads BOTH SOURCES and
    fails if the Graph version, the character cap, the tag grammar or the
    shape of the identity line stop agreeing.

THE TEST THAT MATTERS MOST
    ForgeTheIdentityLine. The prefix exists because Mr. Salam asked, by name, on
    2026-09-07 that every message say which instance sent it. A guard on that
    line is only worth what it withstands, so the suite forges the line rather
    than asserting the guard is present: a tag of "x]\\nDONE - grok" composed a
    second, convincing prefix until the check that refuses it was added, and a
    test that only checked a good tag would never have found it.

Run: python3 tests/test_wa_notify.py
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import wa_notify as wn  # noqa: E402

PS1 = (SCRIPTS / "wa_notify.ps1").read_text(encoding="utf-8", errors="replace")


def env_file(**pairs) -> str:
    """A throwaway .env. Values are obvious fakes; nothing here is a credential."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False,
                                     encoding="utf-8")
    for k, v in pairs.items():
        fh.write("%s=%s\n" % (k, v))
    fh.close()
    return fh.name


GOOD = dict(META_TOKEN="fake-token-value", WA_PHONE_NUMBER_ID="1273365472529201",
            WA_TO="16472183217")


class ComposeTheIdentityLine(unittest.TestCase):
    def test_prefix_is_written_and_is_the_first_line(self):
        body = wn.compose("receipt", "STATUS", "wa-poller")
        self.assertEqual(body.split("\n", 1)[0], "[STATUS - wa-poller]")
        self.assertEqual(body, "[STATUS - wa-poller]\nreceipt")

    def test_separator_is_ascii(self):
        # A UTF-8 middle dot here renders as mojibake once the 5.1 copy reads
        # this file as ANSI. The two implementations matching beats the glyph.
        body = wn.compose("x", "DONE", "claude-code-cli")
        self.assertTrue(all(ord(c) < 128 for c in body.split("\n", 1)[0]))

    def test_raw_sends_nothing_but_the_text(self):
        self.assertEqual(wn.compose("just this", "STATUS", "tag", raw=True),
                         "just this")

    def test_trailing_newlines_are_trimmed_not_the_leading_ones(self):
        self.assertEqual(wn.compose("a\n\nb\n\n", "ASK", "t"),
                         "[ASK - t]\na\n\nb")

    def test_overlong_body_is_truncated_under_metas_real_limit(self):
        body = wn.compose("x" * 9000, "STATUS", "t")
        self.assertEqual(len(body), wn.MAX_CHARS)
        self.assertTrue(body.endswith("..."))
        self.assertLess(len(body), 4096, "Meta rejects a text body over 4096")

    def test_a_body_at_the_cap_is_not_mangled(self):
        text = "y" * (wn.MAX_CHARS - len("[STATUS - t]\n"))
        body = wn.compose(text, "STATUS", "t")
        self.assertEqual(len(body), wn.MAX_CHARS)
        self.assertFalse(body.endswith("..."))


class ForgeTheIdentityLine(unittest.TestCase):
    """Attack the prefix. Asserting the guard exists proves nothing."""

    FORGERIES = [
        "x]\nDONE - grok",        # the one that worked
        "grok\r\n[DONE - grok",   # CRLF, because the .ps1 writes CRLF files
        "a]\u2028[DONE - grok",   # LINE SEPARATOR; WhatsApp renders it as a break
        "has space",
        "-leading",
        "",
        "a" * 65,
        "tag;rm -rf",
    ]

    def test_every_forgery_is_refused(self):
        for tag in self.FORGERIES:
            with self.subTest(tag=tag):
                with self.assertRaises(SystemExit):
                    wn.compose("body", "STATUS", tag)

    def test_no_refused_tag_ever_reaches_a_body(self):
        # Belt and braces: if a future edit downgrades the refusal to a warning,
        # this still fails, because it looks at the OUTPUT rather than the raise.
        for tag in self.FORGERIES:
            try:
                body = wn.compose("body", "STATUS", tag)
            except SystemExit:
                continue
            self.assertEqual(len(body.split("\n")[0].split("]")), 2,
                             "tag %r composed more than one identity line" % tag)

    def test_real_tags_still_pass(self):
        for tag in ("wa-poller", "claude-code-cli", "grok-bot", "ok.tag_1-2",
                    "a", "vm-cli"):
            with self.subTest(tag=tag):
                self.assertTrue(wn.compose("b", "DONE", tag)
                                .startswith("[DONE - %s]\n" % tag))

    def test_an_unknown_kind_is_refused(self):
        for kind in ("SHOUT", "status", "", "STATUS]\n[DONE - grok"):
            with self.subTest(kind=kind):
                with self.assertRaises(SystemExit):
                    wn.compose("body", kind, "wa-poller")

    def test_raw_is_the_only_way_past_and_it_carries_no_identity(self):
        # --raw is documented as "do not use for anything he reads". It is safe
        # only because it emits NO prefix - there is nothing there to forge.
        self.assertEqual(wn.compose("b", "SHOUT", "any tag]\n", raw=True), "b")


class Credentials(unittest.TestCase):
    def test_meta_token_wins_and_the_caller_is_told_which(self):
        p = env_file(META_TOKEN="from-meta", WA_TOKEN="from-wa",
                     WA_PHONE_NUMBER_ID="1273365472529201", WA_TO="16472183217")
        token, _, _, which = wn.credentials(wn.load_env(Path(p)))
        self.assertEqual(token, "from-meta")
        self.assertEqual(which, "META_TOKEN")

    def test_wa_token_is_the_fallback_and_is_named_as_such(self):
        # A silent fallback to a stale second credential is how a Graph 190 gets
        # misread as an expired token. The caller has to be able to say which.
        p = env_file(WA_TOKEN="from-wa", WA_PHONE_NUMBER_ID="1273365472529201",
                     WA_TO="16472183217")
        token, _, _, which = wn.credentials(wn.load_env(Path(p)))
        self.assertEqual(token, "from-wa")
        self.assertEqual(which, "WA_TOKEN")

    def test_an_empty_meta_token_falls_through_rather_than_failing(self):
        p = env_file(META_TOKEN="", WA_TOKEN="from-wa",
                     WA_PHONE_NUMBER_ID="1273365472529201", WA_TO="16472183217")
        _, _, _, which = wn.credentials(wn.load_env(Path(p)))
        self.assertEqual(which, "WA_TOKEN")

    def test_the_bearer_prefix_is_stripped(self):
        # GOTCHA, cost 20 minutes on 2026-09-06: "Bearer Bearer ..." returns 401
        # code 190, which reads exactly like expiry and is not.
        for stored in ("Bearer tok", "bearer tok", "  Bearer   tok  ", "tok"):
            with self.subTest(stored=stored):
                p = env_file(META_TOKEN=stored,
                             WA_PHONE_NUMBER_ID="1273365472529201",
                             WA_TO="16472183217")
                token, _, _, _ = wn.credentials(wn.load_env(Path(p)))
                self.assertEqual(token, "tok")

    def test_no_token_at_all_is_a_refusal_not_a_send(self):
        p = env_file(WA_PHONE_NUMBER_ID="1273365472529201", WA_TO="16472183217")
        with self.assertRaises(SystemExit):
            wn.credentials(wn.load_env(Path(p)))

    def test_recipient_and_phone_id_must_be_digits(self):
        for bad in (dict(WA_TO="not-a-number"), dict(WA_TO=""),
                    dict(WA_TO="1234"), dict(WA_PHONE_NUMBER_ID="abc"),
                    dict(WA_PHONE_NUMBER_ID="")):
            cfg = dict(GOOD)
            cfg.update(bad)
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    wn.credentials(wn.load_env(Path(env_file(**cfg))))

    def test_a_formatted_number_is_accepted_and_normalised(self):
        p = env_file(META_TOKEN="t", WA_PHONE_NUMBER_ID="1273365472529201",
                     WA_TO="+1 (647) 218-3217")
        _, _, recipient, _ = wn.credentials(wn.load_env(Path(p)))
        self.assertEqual(recipient, "16472183217")

    def test_a_missing_env_file_refuses_rather_than_defaulting(self):
        with self.assertRaises(SystemExit):
            wn.load_env(Path(tempfile.gettempdir()) / "definitely-not-here.env")

    def test_quotes_around_a_value_are_stripped(self):
        p = env_file(META_TOKEN='"quoted"', WA_PHONE_NUMBER_ID="1273365472529201",
                     WA_TO="16472183217")
        token, _, _, _ = wn.credentials(wn.load_env(Path(p)))
        self.assertEqual(token, "quoted")


class DryRunNeverTouchesTheNetwork(unittest.TestCase):
    def test_dry_run_returns_the_body_and_does_not_call_send(self):
        p = env_file(**GOOD)
        with mock.patch.object(wn, "send",
                               side_effect=AssertionError("send was called")):
            ok, detail = wn.notify("receipt", kind="STATUS", tag="wa-poller",
                                   dry_run=True, env_file=p)
        self.assertTrue(ok)
        self.assertIn("[STATUS - wa-poller]", detail)
        self.assertIn("META_TOKEN", detail, "the dry run must name the credential")

    def test_a_dry_run_never_prints_the_token(self):
        p = env_file(META_TOKEN="sup3r-s3cret-value", **{
            k: v for k, v in GOOD.items() if k != "META_TOKEN"})
        _, detail = wn.notify("x", dry_run=True, env_file=p)
        self.assertNotIn("sup3r-s3cret-value", detail)

    def test_the_url_is_built_from_the_pinned_graph_version(self):
        # Checked without sending: assert on the request object urlopen is given.
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            raise RuntimeError("stopped before any socket")

        with mock.patch.object(wn.urllib.request, "urlopen", fake_urlopen):
            res = wn.send("body", "tok", "1273365472529201", "16472183217")
        self.assertFalse(res["ok"])
        self.assertEqual(captured["url"],
                         "https://graph.facebook.com/%s/1273365472529201/messages"
                         % wn.GRAPH_VERSION)
        self.assertEqual(captured["auth"], "Bearer tok")

    def test_a_successful_send_calls_urlopen_exactly_once(self):
        """The case where a duplicate actually costs something.

        This test exists because a mutant that inserted a second urlopen call
        survived the whole suite. The only call-counting test at the time used a
        transport that raised, so the duplicate threw before the counter could
        see it. A double send is harmless when the send fails and arrives twice
        in his chat when it does not, so the counting has to happen on success.
        """
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return 200

            def read(self):
                return b'{"messages":[{"id":"wamid.TEST"}]}'

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return Response()

        with mock.patch.object(wn.urllib.request, "urlopen", fake_urlopen):
            res = wn.send("body", "tok", "1273365472529201", "16472183217")
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], 200)
        self.assertEqual(len(calls), 1,
                         "sent %d times; a duplicate receipt reaches his chat"
                         % len(calls))

    def test_notify_sends_once_end_to_end(self):
        """Same count, one layer up, so a retry in notify() is caught too."""
        sends = []
        with mock.patch.object(wn, "send",
                               lambda *a, **k: sends.append(a) or {
                                   "ok": True, "status": 200, "body": "{}"}):
            ok, _ = wn.notify("receipt", kind="STATUS", tag="wa-poller",
                              env_file=env_file(**GOOD))
        self.assertTrue(ok)
        self.assertEqual(len(sends), 1)

    def test_a_transport_failure_is_reported_not_raised_and_not_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise OSError("no route to host")

        with mock.patch.object(wn.urllib.request, "urlopen", fake_urlopen):
            res = wn.send("body", "tok", "1273365472529201", "16472183217")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 0)
        self.assertEqual(len(calls), 1,
                         "a retried send arrives twice in his chat")


class ThePollerUsesItRatherThanAShell(unittest.TestCase):
    """scripts/grok_wa_inbox.py was the caller this migration was for."""

    def setUp(self):
        import grok_wa_inbox
        self.poller = grok_wa_inbox

    def test_ack_calls_notify_with_the_pollers_own_identity(self):
        seen = {}

        def fake_notify(text, kind="BLOCKED", tag="claude-code-cli", **kw):
            seen.update(text=text, kind=kind, tag=tag)
            return True, "HTTP 200 {}"

        with mock.patch.object(wn, "notify", fake_notify):
            ok, detail = self.poller.ack_whatsapp("received")
        self.assertTrue(ok)
        self.assertEqual(seen["kind"], "STATUS")
        # NOT grok-bot. Stamping the poller's automatic ACK with grok's tag made
        # it look as though Grok had answered - the exact confusion the prefix
        # was introduced to remove.
        self.assertEqual(seen["tag"], "wa-poller")
        self.assertEqual(seen["text"], "received")

    def test_a_missing_credential_is_returned_as_false_not_an_exit(self):
        # ack_whatsapp is called from a polling loop. A SystemExit out of the
        # notifier would kill the poll instead of skipping one acknowledgement.
        def boom(*a, **k):
            raise SystemExit("no WhatsApp token")

        with mock.patch.object(wn, "notify", boom):
            ok, detail = self.poller.ack_whatsapp("received")
        self.assertFalse(ok)
        self.assertIn("token", detail)

    def test_an_unexpected_error_is_also_contained(self):
        with mock.patch.object(wn, "notify", side_effect=ValueError("odd")):
            ok, detail = self.poller.ack_whatsapp("received")
        self.assertFalse(ok)
        self.assertIn("ValueError", detail)

    def test_the_ack_path_no_longer_shells_out(self):
        """Judged on code, not prose.

        The first version of this test grepped the file for "powershell.exe" and
        failed - on the sentence in the module docstring that RECORDS the
        shell-out being removed. A substring cannot tell "invokes PowerShell"
        from "says it does not invoke PowerShell", which is the same trap the
        python-suites workflow hit when it grepped for "urlopen". So parse, drop
        every docstring, and look at the imports and the live string literals.
        """
        # utf-8-sig, not utf-8: this file carries a BOM, and ast.parse
        # rejects U+FEFF as an invalid non-printable character. Reading a
        # fleet script in order to PARSE it always needs the -sig codec.
        source = (SCRIPTS / "grok_wa_inbox.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                first = (node.body or [None])[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("os", imported, "os.system is the other way out")
        self.assertIn("wa_notify", imported)

        shellish = re.compile(r"(?i)powershell|pwsh|cmd\.exe|\.ps1$")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                self.assertIsNone(shellish.search(node.value.strip()),
                                  "live string literal invokes a shell: %r"
                                  % node.value[:60])

    def test_the_guard_above_would_notice_a_shell_out_coming_back(self):
        """Negative control. A guard nobody has tried to defeat is decoration."""
        shellish = re.compile(r"(?i)powershell|pwsh|cmd\.exe|\.ps1$")
        relapse = ('import subprocess\n'
                   'def ack(t):\n'
                   '    return subprocess.run(["powershell", "-File", "x.ps1"])\n')
        tree = ast.parse(relapse)
        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.Import) for a in n.names}
        self.assertIn("subprocess", imported)
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertTrue(any(shellish.search(s) for s in literals),
                        "the literal check would not have caught a relapse")


class TheTwoImplementationsAgree(unittest.TestCase):
    """Two copies of one contract drift. This is the only thing that notices."""

    def test_graph_version_matches_the_powershell(self):
        m = re.search(r"\$GRAPH_VERSION\s*=\s*'([^']+)'", PS1)
        self.assertIsNotNone(m, "could not find $GRAPH_VERSION in the .ps1")
        self.assertEqual(wn.GRAPH_VERSION, m.group(1))

    def test_character_cap_matches_the_powershell(self):
        m = re.search(r"\$MAX_CHARS\s*=\s*(\d+)", PS1)
        self.assertIsNotNone(m)
        self.assertEqual(wn.MAX_CHARS, int(m.group(1)))

    def test_tag_grammar_matches_the_powershell(self):
        m = re.search(r"\$Tag\s*-notmatch\s*'([^']+)'", PS1)
        self.assertIsNotNone(m, "the .ps1 no longer validates -Tag")
        self.assertEqual(wn.TAG_RE.pattern, m.group(1))

    def test_the_kind_set_matches_the_powershell_validateset(self):
        m = re.search(r"\[ValidateSet\(([^)]+)\)\]", PS1)
        self.assertIsNotNone(m)
        kinds = tuple(re.findall(r"'([^']+)'", m.group(1)))
        self.assertEqual(set(wn.KINDS), set(kinds))

    def test_the_identity_line_has_the_same_shape_in_both(self):
        self.assertIn('"[$Kind - $Tag]', PS1)
        py = (SCRIPTS / "wa_notify.py").read_text(encoding="utf-8")
        self.assertIn('"[%s - %s]', py)

    def test_the_python_port_needs_no_third_party_import(self):
        # An import that is absent in a cloud sandbox is the dependency this
        # migration exists to remove; scripts/wa_send.py needs `requests`.
        py = (SCRIPTS / "wa_notify.py").read_text(encoding="utf-8")
        for banned in ("import requests", "from requests", "import httpx"):
            self.assertNotIn(banned, py)

    def test_the_powershell_keeps_the_interactive_features(self):
        # This port covers plain text only. If buttons ever move here, the cap
        # for an interactive body is 1000, not 3800, and this test should fail.
        self.assertIn("$Buttons", PS1)
        py = (SCRIPTS / "wa_notify.py").read_text(encoding="utf-8")
        self.assertNotIn('"buttons"', py)


if __name__ == "__main__":
    os.chdir(ROOT)
    unittest.main(verbosity=2)
