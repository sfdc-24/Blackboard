"""Tests for scripts/private_review.py: one private pair, verified first, asked once, kept private (no network)."""
from __future__ import annotations

import ast
import base64
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import private_review as pr  # noqa: E402

# Synthetic throughout: no real review's head, digest or path is in this file.
SENTINEL = "ZQX-PRIVATE-SENTINEL-7f3a91c4"
# Longer than every cap on the public path (2,500, 6,000 and 24,000 characters), with a last line to lose.
MD = ("# Synthetic architecture\n\n## 1. Scope\n\n" + SENTINEL + " is in the source.\n\n"
      + "".join("## %d. Section\n\nline %d of the body.\n\n" % (n, n) for n in range(2, 1500))
      + "## End\n\nthe last line.\n").encode("utf-8")
PDF = (b"%PDF-1.4\n% synthetic, page 1: " + SENTINEL.encode("ascii") + b"\n"
       + bytes(range(256)) * 64 + b"\n%%EOF\n")
HEAD = "a1" * 20
RID = "SYNTH-REVIEW-1"
LINK = "https://github.com/sfdc-24/conference/pull/4242"
GOOD = "VERDICT: AGREE\nSection 1 (Scope), PDF page 1: " + SENTINEL + " reads the same in both.\n"
ROW = "BCB|v=1|id=SYNTH-ASK-1|phase=DISPATCH|from=codex|to=gemini|review=%s" % RID


def setUpModule():
    # No test may reach the network: a socket that tries to connect fails the test that opened it.
    guard = mock.patch("socket.socket.connect", side_effect=AssertionError("a test tried to open a socket"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def http_error(case, code):
    """An HTTP failure whose message holds the sentinel, closed when the test ends."""
    error = urllib.error.HTTPError("https://synthetic.test/", code, SENTINEL, hdrs=None, fp=None)
    case.addCleanup(error.close)
    return error


def artifact(path, data, media_type):
    return {"path": path, "bytes": len(data), "sha256": sha(data), "media_type": media_type}


def manifest(**over):
    m = {"review_id": RID, "repo": "sfdc-24/conference", "pull_request": 7, "head": HEAD,
         "artifacts": [artifact("docs/synthetic.md", MD, "text/markdown"),
                       artifact("docs/synthetic.pdf", PDF, "application/pdf")],
         "provider": "gemini", "route": "api-key", "model": "gemini-pro-latest", "key_env": "GEMINI_API_KEY",
         "max_output_tokens": 8192, "deadline_seconds": 150,
         "result": {"repo": "sfdc-24/conference", "path": "docs/reviews/gemini/%s.md" % RID}}
    m.update(over)
    return m


def answer(text=GOOD, finish="STOP", **over):
    out = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
           "responseId": "resp-synth-1", "modelVersion": "gemini-pro-synth-001",
           "usageMetadata": {"promptTokenCount": 9000, "candidatesTokenCount": 40, "totalTokenCount": 9040}}
    out.update(over)
    return out


class Rig:
    """Every door the runner goes through, faked and counted. Nothing here opens a socket."""

    def __init__(self, *, heads=(HEAD,), blobs=None, reply=None, private=(True,), used=(), link=LINK,
                 stored=None, took=0.0, fetch_takes=0.0):
        self.heads, self.private, self.link, self.stored = list(heads), list(private), link, stored
        self.blobs = {"docs/synthetic.md": MD, "docs/synthetic.pdf": PDF} if blobs is None else blobs
        self.reply = answer() if reply is None else reply
        self.used, self.took, self.fetch_takes, self.t = set(used), took, fetch_takes, 0.0
        self.events, self.asked, self.written, self.lines = [], [], [], []

    def pr_head(self, repo, number):
        self.events.append("head %s #%d" % (repo, number))
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]

    def fetch(self, repo, head, path):
        self.events.append("fetch %s@%s" % (path, head))
        self.t += self.fetch_takes
        blob = self.blobs[path]
        if isinstance(blob, BaseException):
            raise blob
        return blob

    def is_private(self, repo):
        self.events.append("private? %s" % repo)
        return self.private.pop(0) if len(self.private) > 1 else self.private[0]

    def claim(self, review_id):
        self.events.append("claim")
        if review_id in self.used:
            return False
        self.used.add(review_id)
        return True

    def provider(self, body, *, route, model, key_env, timeout):
        self.events.append("provider")
        self.asked.append({"body": body, "route": route, "model": model, "key_env": key_env, "timeout": timeout})
        self.t += self.took
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    def write_result(self, destination, content):
        self.events.append("write")
        self.written.append((destination, content))
        if isinstance(self.link, BaseException):
            raise self.link
        return self.link

    def read_result(self, destination, link):
        self.events.append("read back")
        if isinstance(self.stored, BaseException):
            raise self.stored
        return self.written[-1][1] if self.stored is None else self.stored

    def wiring(self, **over):
        return dict(dict(pr_head=self.pr_head, fetch=self.fetch, is_private=self.is_private, claim=self.claim,
                         provider=self.provider, write_result=self.write_result, read_result=self.read_result,
                         env={}, now=lambda: "2026-01-02T03:04:05Z", clock=lambda: self.t,
                         log=self.lines.append), **over)

    def run(self, m=None, **over):
        return pr.run(manifest() if m is None else m, **self.wiring(**over))


class Manifest(unittest.TestCase):
    """The schema is closed: what it does not name, it refuses."""

    def refused(self, m, why):
        with self.assertRaises(pr.Blocked) as caught:
            pr.validate_manifest(m)
        self.assertIn(why, str(caught.exception))

    def test_a_complete_manifest_is_accepted_and_ordered_markdown_then_pdf(self):
        m = manifest()
        self.assertEqual(m, pr.validate_manifest(m))
        swapped = pr.validate_manifest(manifest(artifacts=list(reversed(m["artifacts"]))))
        self.assertEqual(["text/markdown", "application/pdf"], [a["media_type"] for a in swapped["artifacts"]])

    def test_an_unknown_key_anywhere_refuses_it(self):
        self.refused(dict(manifest(), extra_paths=["docs/private.md"]), "1 unknown")
        forged = manifest()
        forged["artifacts"][0]["ref"] = "main"
        self.refused(forged, "an artifact does not have exactly its keys")
        self.refused(manifest(result={"repo": "sfdc-24/conference", "path": "docs/r.md", "to": "ALL"}),
                     "the result destination does not have exactly its keys")

    def test_a_missing_key_refuses_it(self):
        for key in manifest():
            m = manifest()
            del m[key]
            self.refused(m, "missing: %s" % key)

    def test_a_value_of_another_type_refuses_it(self):
        for key, value in (("pull_request", True), ("pull_request", "7"), ("max_output_tokens", 8192.0),
                           ("deadline_seconds", "150"), ("head", None), ("review_id", 1), ("result", "x.md"),
                           ("artifacts", {"0": 1}), ("model", ["gemini-pro-latest"])):
            self.refused(manifest(**{key: value}), "%s is not" % key)
        m = manifest()
        m["artifacts"][1]["bytes"] = float(len(PDF))
        self.refused(m, "bytes is not int")
        self.refused([manifest()], "the manifest is not an object")

    def test_artifacts_are_exactly_one_markdown_and_one_pdf(self):
        md, pdf = manifest()["artifacts"]
        self.refused(manifest(artifacts=[md, pdf, dict(md, path="docs/more.md")]), "not exactly two")
        self.refused(manifest(artifacts=[md]), "not exactly two")
        self.refused(manifest(artifacts=[pdf, dict(pdf, path="docs/other.pdf")]), "not one text/markdown")
        self.refused(manifest(artifacts=[dict(md, media_type="text/plain"), pdf]), "not one text/markdown")

    def test_head_and_digests_are_exact_lowercase_hex(self):
        for head in ("main", HEAD[:39], HEAD + "a", HEAD.upper(), "refs/pull/7/head", HEAD + "\n"):
            self.refused(manifest(head=head), "head is not 40 lowercase hex")
        for digest in ("", sha(MD)[:63], sha(MD).upper(), "sha256:" + sha(MD)):
            m = manifest()
            m["artifacts"][0]["sha256"] = digest
            self.refused(m, "sha256 is not 64 lowercase hex")

    def test_paths_stay_plain_repository_paths(self):
        for path in ("../secrets.md", "docs/../../x.md", "/etc/x.md", "docs\\x.md", "docs/.hidden.md",
                     "docs/x.md?ref=main", "docs/x.txt", "docs//x.md", "docs/x.md\n", "d" * 200 + ".md"):
            m = manifest()
            m["artifacts"][0]["path"] = path
            self.refused(m, "not a plain repository path")
        self.refused(manifest(result={"repo": "sfdc-24/conference", "path": "../out.md"}), "result destination")
        self.refused(manifest(result={"repo": "sfdc-24/conference", "path": "docs/synthetic.md"}),
                     "would overwrite an artifact")

    def test_repository_provider_route_model_and_key_are_closed_sets(self):
        self.refused(manifest(repo="sfdc-24/Blackboard"), "repo is not a repository this adapter reviews")
        self.refused(manifest(repo="someone/conference"), "repo is not")
        self.refused(manifest(result={"repo": "sfdc-24/Blackboard", "path": "docs/r.md"}), "result destination")
        self.refused(manifest(provider="openai"), "provider and route")
        self.refused(manifest(route="vertex-adc"), "provider and route")      # the old fallback is not offered
        self.refused(manifest(model="gpt-5"), "model is not a Gemini model name")
        self.refused(manifest(model="gemini-pro-latest:generateContent?key=x"), "model is not")
        self.refused(manifest(key_env="GEMINI_GITHUB_TOKEN"), "key_env is not a Gemini key variable")

    def test_the_budgets_are_bounded(self):
        for cap in (0, -1, pr.MAX_OUTPUT_TOKENS + 1):
            self.refused(manifest(max_output_tokens=cap), "max_output_tokens is outside")
        for seconds in (0, pr.MAX_DEADLINE_SECONDS + 1):
            self.refused(manifest(deadline_seconds=seconds), "deadline_seconds is outside")
        m = manifest()
        m["artifacts"][1]["bytes"] = pr.MAX_BYTES["application/pdf"] + 1
        self.refused(m, "length is outside")


class ManifestFile(unittest.TestCase):
    """The manifest comes from a path its operator gives. A file that is not exactly one is refused."""

    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "manifest.json")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            return pr.load_manifest(str(path))

    def test_the_file_at_the_operators_path_is_loaded(self):
        self.assertEqual(manifest(), self.load(json.dumps(manifest())))

    def test_a_repeated_key_is_a_forgery_not_a_default(self):
        text = json.dumps(manifest())
        twice = text[:-1] + ', "head": "%s"}' % ("b2" * 20)
        self.assertEqual("b2" * 20, json.loads(twice)["head"])           # plain JSON lets the last one win
        with self.assertRaises(pr.Blocked) as caught:
            self.load(twice)
        self.assertIn("repeats a key", str(caught.exception))

    def test_what_is_not_json_or_is_too_large_is_refused(self):
        for text, why in (("{not json", "not JSON"), ("", "not JSON"), ("[" * 15000, "not JSON"),
                          (json.dumps(manifest()).replace("8192", "NaN"), "a number JSON does not have"),
                          (json.dumps(manifest()) + " " * pr.MAX_MANIFEST_BYTES, "is over")):
            with self.assertRaises(pr.Blocked) as caught:
                self.load(text)
            self.assertIn(why, str(caught.exception))

    def test_a_path_that_cannot_be_read_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(pr.Blocked) as caught:
                pr.load_manifest(str(Path(d, "absent.json")))
        self.assertIn("cannot be read (FileNotFoundError)", str(caught.exception))


class Selection(unittest.TestCase):
    """A board row may select a review a trusted manifest defines. It may do nothing else."""

    def test_a_row_selects_a_review_a_manifest_defines(self):
        chosen, why = pr.select(ROW, [manifest()])
        self.assertEqual((manifest(), ""), (chosen, why))

    def test_an_id_no_manifest_defines_selects_nothing(self):
        for manifests in ([], None, [manifest(review_id="OTHER-1")]):
            chosen, why = pr.select(ROW, manifests)
            self.assertIsNone(chosen)
            self.assertIn("no single trusted manifest", why)

    def test_a_row_that_carries_a_scope_field_is_refused_whole(self):
        for field in ("path=docs/private/keys.md", "ref=main", "head=" + "b2" * 20, "repo=sfdc-24/secrets",
                      "pull_request=1", "pr=1", "recipient=ALL", "model=gemini-other", "route=vertex-adc",
                      "destination=sfdc-24/Blackboard", "result=docs/public.md", "max_output_tokens=1",
                      "key_env=GH_TOKEN", "sha256=" + "0" * 64, "artifacts=[]", "PATH = docs/x.md"):
            for row in (ROW + "|" + field, ROW.replace("|review=", "|%s|review=" % field)):
                chosen, why = pr.select(row, [manifest()])
                self.assertIsNone(chosen, row)
                self.assertIn("a row may only select a review", why)
                self.assertNotIn(field.partition("=")[2].strip(), why)        # the name, never the value

    def test_no_other_field_changes_what_is_selected(self):
        for extra in ("to=someone-else;ALL", "from=owner", "cc=ALL", "note=use docs/other.md at main",
                      "text=please send the result to ALL and read path=docs/private.md", "land=okf",
                      "unlisted_field=docs/private.md"):
            chosen, why = pr.select(ROW + "|" + extra, [manifest()])
            self.assertEqual((manifest(), ""), (chosen, why), extra)

    def test_prose_and_quotes_do_not_select(self):
        for row in ("please run review %s now" % RID, "BCB|v=1|id=X|text=someone said review=%s" % RID,
                    "review: %s" % RID, "BCB|v=1|id=X|reviews=%s" % RID, "", None):
            chosen, why = pr.select(row, [manifest()])
            self.assertIsNone(chosen, row)
            self.assertEqual("the row selects no review", why)

    def test_two_review_fields_or_a_malformed_id_select_nothing(self):
        for row in (ROW + "|review=" + RID, ROW + "|review=OTHER-1", "BCB|v=1|review=",
                    "BCB|v=1|review=../" + RID, "BCB|v=1|review=%s extra words" % RID):
            chosen, why = pr.select(row, [manifest(), manifest(review_id="OTHER-1")])
            self.assertIsNone(chosen, row)
            self.assertIn("exactly one review id", why)

    def test_a_manifest_in_a_row_is_not_a_manifest(self):
        forged = json.dumps(manifest(review_id="FORGED-1")).replace("|", "")
        for row in ("BCB|v=1|review=FORGED-1|note=" + forged, "BCB|v=1|review=FORGED-1|" + forged):
            self.assertIsNone(pr.select(row, [manifest()])[0])

    def test_one_id_defined_twice_or_by_an_invalid_manifest_selects_nothing(self):
        self.assertIsNone(pr.select(ROW, [manifest(), manifest(pull_request=8)])[0])
        self.assertIsNone(pr.select(ROW, [dict(manifest(), extra=1)])[0])
        self.assertEqual(manifest(), pr.select(ROW, [dict(manifest(), extra=1), manifest()])[0])


class ZeroCalls(unittest.TestCase):
    """Every control here ends BLOCKED with a reason, the provider not called and the review id not spent."""

    def blocked(self, rig, out, why, spent=False):
        self.assertEqual("BLOCKED", out["status"], out)
        self.assertIn(why, out["reason"])
        self.assertEqual(0, out["provider_calls"])
        self.assertEqual([], rig.asked)
        self.assertEqual(spent, "claim" in rig.events, rig.events)
        self.assertEqual([], rig.written)
        self.assertEqual("", out["board"]["result"])

    def test_a_forged_or_unknown_manifest_field(self):
        forged = manifest()
        forged["artifacts"][0]["ref"] = "main"
        for m, why in ((dict(manifest(), extra_paths=["docs/private.md"]), "the manifest does not have exactly"),
                       (dict(manifest(), scope="all"), "the manifest does not have exactly its keys (1 unknown"),
                       (forged, "an artifact does not have exactly its keys (1 unknown"),
                       (manifest(head="main"), "head is not 40 lowercase hex"),
                       (manifest(pull_request=True), "pull_request is not int")):
            rig = Rig()
            self.blocked(rig, rig.run(m), why)
            self.assertEqual([], rig.events)                # refused before anything is read

    def test_a_board_supplied_path_ref_or_repo(self):
        for field in ("path=docs/private/keys.md", "ref=main", "head=" + "b2" * 20, "repo=sfdc-24/secrets",
                      "recipient=ALL", "model=gemini-other", "destination=sfdc-24/Blackboard"):
            rig = Rig()
            out = pr.run_selected(ROW + "|" + field, [manifest()], **rig.wiring())
            self.blocked(rig, out, "a row may only select a review")
            self.assertEqual([], rig.events)
            self.assertEqual({"status": "BLOCKED"}, {k: v for k, v in out["board"].items() if v})

    def test_a_row_that_only_selects_is_the_one_that_runs(self):
        # The control for the control: the same rig, the same manifest, the plain row.
        for row in (ROW, ROW + "|to=someone-else;ALL|cc=ALL|unlisted_field=docs/private.md"):
            rig = Rig()
            out = pr.run_selected(row, [manifest()], **rig.wiring())
            self.assertEqual(("AGREE", 1), (out["status"], out["provider_calls"]))
            self.assertEqual(["fetch docs/synthetic.md@" + HEAD, "fetch docs/synthetic.pdf@" + HEAD],
                             [e for e in rig.events if e.startswith("fetch")])
            self.assertEqual({"repo": "sfdc-24/conference", "path": "docs/reviews/gemini/%s.md" % RID},
                             rig.written[0][0])
            self.assertEqual("gemini-pro-latest", rig.asked[0]["model"])

    def test_an_unknown_review_id(self):
        rig = Rig()
        out = pr.run_selected(ROW.replace(RID, "NOT-DEFINED-9"), [manifest()], **rig.wiring())
        self.blocked(rig, out, "no single trusted manifest")
        self.assertEqual("", out["board"]["review_id"])

    def test_a_head_mismatch(self):
        for head in ("b2" * 20, HEAD.upper(), "", None, HEAD[:39]):
            rig = Rig(heads=[head])
            self.blocked(rig, rig.run(), "head is not the manifest's")
            self.assertFalse(any(e.startswith("fetch") for e in rig.events), rig.events)

    def test_a_head_that_moves_while_the_artifacts_are_read(self):
        rig = Rig(heads=[HEAD, "b2" * 20])
        self.blocked(rig, rig.run(), "head moved while the artifacts were read")

    def test_a_digest_mismatch(self):
        for path, good in (("docs/synthetic.md", MD), ("docs/synthetic.pdf", PDF)):
            changed = good[:-2] + bytes([good[-2] ^ 1]) + good[-1:]
            self.assertEqual(len(good), len(changed))
            rig = Rig(blobs={"docs/synthetic.md": MD, "docs/synthetic.pdf": PDF, path: changed})
            self.blocked(rig, rig.run(), "SHA-256 is not the manifest's")

    def test_a_length_mismatch(self):
        for delta in (1, -1):
            m = manifest()
            m["artifacts"][1]["bytes"] += delta
            rig = Rig()
            self.blocked(rig, rig.run(m), "the manifest says %d" % (len(PDF) + delta))

    def test_a_clipped_artifact(self):
        for path, clipped in (("docs/synthetic.md", MD[:24000]), ("docs/synthetic.md", MD[:-1]),
                              ("docs/synthetic.pdf", PDF[:6000]), ("docs/synthetic.pdf", b"")):
            rig = Rig(blobs={"docs/synthetic.md": MD, "docs/synthetic.pdf": PDF, path: clipped})
            self.blocked(rig, rig.run(), "clipped or changed")

    def test_an_artifact_that_arrives_as_text_not_bytes(self):
        rig = Rig(blobs={"docs/synthetic.md": MD.decode("utf-8"), "docs/synthetic.pdf": PDF})
        self.blocked(rig, rig.run(), "did not arrive as bytes")

    def test_a_path_the_manifest_does_not_name(self):
        rig = Rig()
        for path in ("docs/private/keys.md", "docs/synthetic.md/../other.md", "", "DOCS/SYNTHETIC.MD"):
            with self.assertRaises(pr.Blocked) as caught:
                pr.read_artifact(manifest(), path, rig.fetch)
            self.assertIn("a path the manifest does not name", str(caught.exception))
        self.assertEqual([], rig.events)                    # never fetched
        self.assertEqual(MD, pr.read_artifact(manifest(), "docs/synthetic.md", rig.fetch))

    def test_a_repeated_review_id(self):
        rig = Rig(used={RID})
        self.blocked(rig, rig.run(), "already used", spent=True)

    def test_a_ledger_that_fails_or_does_not_say_true(self):
        for said in (None, False, 1, "yes"):
            rig = Rig()
            self.blocked(rig, rig.run(claim=lambda rid, said=said: said), "already used")
        rig = Rig()

        def broken(rid):
            raise OSError(SENTINEL)

        self.blocked(rig, rig.run(claim=broken), "OSError while recording the review id")

    def test_a_deadline_spent_before_the_call(self):
        rig = Rig(fetch_takes=75.0)
        self.blocked(rig, rig.run(), "the deadline was spent before the provider call")

    def test_a_result_repository_that_is_not_private(self):
        for private in (False, None, "true", 1):
            rig = Rig(private=[private])
            self.blocked(rig, rig.run(), "is not private")
            self.assertEqual(["private? sfdc-24/conference"], rig.events)

    def test_a_read_that_fails(self):
        for error, why in ((http_error(self, 404),
                            "HTTP 404 while reading the application/pdf artifact"),
                           (urllib.error.URLError(SENTINEL), "URLError while reading the application/pdf"),
                           (TimeoutError(SENTINEL), "TimeoutError while reading the application/pdf")):
            rig = Rig(blobs={"docs/synthetic.md": MD, "docs/synthetic.pdf": error})
            out = rig.run()
            self.blocked(rig, out, why)
            self.assertNotIn(SENTINEL, json.dumps(out) + "\n".join(rig.lines))

    def test_a_markdown_that_is_not_utf8_or_a_pdf_that_is_not_one(self):
        for path, kind, bad, why in (("docs/synthetic.md", "text/markdown", b"# latin-1 caf\xe9\n", "not UTF-8"),
                                     ("docs/synthetic.pdf", "application/pdf", b"<html>not a pdf</html>",
                                      "does not start as a PDF")):
            m = manifest(artifacts=[artifact(path, bad, kind) if a["path"] == path else a
                                    for a in manifest()["artifacts"]])
            rig = Rig(blobs={"docs/synthetic.md": MD, "docs/synthetic.pdf": PDF, path: bad})
            self.blocked(rig, rig.run(m), why)

    def test_nothing_wired_and_no_key(self):
        rig = Rig()
        out = pr.run(manifest(), env={}, log=rig.lines.append)
        self.blocked(rig, out, "not wired: no pr_head, fetch, is_private, claim, write_result, read_result")
        out = rig.run(provider=None, env={"GEMINI_API_KEY": "  "})       # the default route, and no key
        self.blocked(rig, out, "no key in GEMINI_API_KEY")
        self.assertEqual([], rig.events)


class TypedRequest(unittest.TestCase):
    """The whole source and the actual PDF reach the request, typed, with an explicit output cap."""

    def test_the_complete_pair_reaches_the_request_whole(self):
        rig = Rig()
        self.assertEqual("AGREE", rig.run()["status"])
        prompt, source, document = rig.asked[0]["body"]["contents"][0]["parts"]
        self.assertEqual(MD.decode("utf-8"), source["text"])                 # every character, not a prefix
        self.assertTrue(source["text"].endswith("the last line.\n"))
        self.assertEqual("application/pdf", document["inlineData"]["mimeType"])
        self.assertEqual(PDF, base64.b64decode(document["inlineData"]["data"], validate=True))
        self.assertEqual(pr.PROMPT, prompt["text"])
        self.assertEqual(["user"], [c["role"] for c in rig.asked[0]["body"]["contents"]])

    def test_the_output_cap_is_the_manifests(self):
        rig = Rig()
        rig.run(manifest(max_output_tokens=12345))
        self.assertEqual({"maxOutputTokens": 12345, "candidateCount": 1}, rig.asked[0]["body"]["generationConfig"])

    def test_the_prompt_demands_a_verdict_line_and_section_and_page_specific_findings(self):
        self.assertIn("first line of your answer must be exactly one of these two lines", pr.PROMPT)
        self.assertIn("\nVERDICT: AGREE\nVERDICT: BLOCKERS\n", pr.PROMPT)
        self.assertIn("section- and page-specific", pr.PROMPT)
        self.assertIn("page number of the PDF", pr.PROMPT)
        self.assertIn("section heading of the source", pr.PROMPT)

    def test_the_route_model_and_key_variable_are_the_manifests(self):
        rig = Rig()
        rig.run(manifest(model="gemini-synthetic-pro", key_env="GOOGLE_AI_API_KEY"))
        asked = rig.asked[0]
        self.assertEqual(("api-key", "gemini-synthetic-pro", "GOOGLE_AI_API_KEY"),
                         (asked["route"], asked["model"], asked["key_env"]))

    def test_what_was_submitted_is_recomputed_from_the_request(self):
        body = pr.build_request(manifest(), MD, PDF)
        self.assertEqual([{"media_type": "text/markdown", "bytes": len(MD), "sha256": sha(MD)},
                          {"media_type": "application/pdf", "bytes": len(PDF), "sha256": sha(PDF)}],
                         pr.submitted(json.loads(json.dumps(body))))          # and it survives the wire form
        with self.assertRaises(pr.Blocked):
            pr.build_request(manifest(), MD[:-1], PDF)       # a request that is not the manifest's is not built
        with self.assertRaises(pr.Blocked):
            pr.build_request(manifest(), MD, PDF + b"\n")


class OneUse(unittest.TestCase):
    """The id is spent before the call, so no crash, error or re-ask can pay for a second one."""

    def test_everything_is_verified_then_the_id_recorded_then_the_one_call(self):
        rig = Rig()
        out = rig.run()
        self.assertEqual(("AGREE", 1), (out["status"], out["provider_calls"]))
        self.assertEqual(["private? sfdc-24/conference", "head sfdc-24/conference #7",
                          "fetch docs/synthetic.md@" + HEAD, "fetch docs/synthetic.pdf@" + HEAD,
                          "head sfdc-24/conference #7", "claim", "provider",
                          "private? sfdc-24/conference", "write", "read back"], rig.events)

    def test_a_crash_in_the_call_has_already_spent_the_id(self):
        class Killed(BaseException):
            """Not an Exception: stands in for the container being killed mid-call."""

        rig = Rig(reply=Killed())
        with self.assertRaises(Killed):
            rig.run()
        self.assertEqual(1, len(rig.asked))
        rig.reply = answer()
        out = rig.run()
        self.assertEqual(("BLOCKED", 0), (out["status"], out["provider_calls"]))
        self.assertIn("already used", out["reason"])
        self.assertEqual(1, len(rig.asked))                 # no second paid call

    def test_a_second_run_of_a_finished_review_makes_no_call(self):
        rig = Rig()
        self.assertEqual("AGREE", rig.run()["status"])
        self.assertEqual("BLOCKED", rig.run()["status"])
        self.assertEqual(1, len(rig.asked))

    def test_an_incomplete_answer_is_not_retried(self):
        for reply in (answer(finish="MAX_TOKENS"), TimeoutError("timed out"), answer(text="")):
            rig = Rig(reply=reply)
            out = rig.run()
            self.assertEqual(("INCOMPLETE", 1), (out["status"], out["provider_calls"]))
            self.assertEqual(1, len(rig.asked))
            self.assertEqual(1, rig.events.count("claim"))


class Answer(unittest.TestCase):
    """Only a whole, well-formed answer is a verdict. Everything else is INCOMPLETE, never AGREE."""

    def incomplete(self, reply, why):
        rig = Rig(reply=reply)
        out = rig.run()
        self.assertEqual("INCOMPLETE", out["status"], reply)
        self.assertEqual("INCOMPLETE", out["board"]["status"])
        self.assertEqual(1, out["provider_calls"])
        self.assertIn(why, out["reason"])
        self.assertIn(b"- **status:** INCOMPLETE", rig.written[0][1])
        self.assertNotIn(b"- **status:** AGREE", rig.written[0][1])
        return rig, out

    def test_a_whole_answer_gives_its_verdict(self):
        for text, verdict in ((GOOD, "AGREE"), ("VERDICT: BLOCKERS\nSection 2, page 3: the table is cut.\n",
                                                "BLOCKERS"), ("VERDICT: AGREE", "AGREE")):
            out = Rig(reply=answer(text)).run()
            self.assertEqual(verdict, out["status"])
            self.assertEqual(verdict, out["board"]["status"])

    def test_a_truncated_answer_cannot_become_agree(self):
        rig, _ = self.incomplete(answer(GOOD, finish="MAX_TOKENS"), "finish reason MAX_TOKENS is not a normal stop")
        self.assertIn(GOOD.encode("utf-8"), rig.written[0][1])       # what did arrive is kept, privately

    def test_a_blocked_or_abnormal_finish(self):
        for finish in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "OTHER", "MALFORMED_FUNCTION_CALL",
                       "FINISH_REASON_UNSPECIFIED", "stop", "STOP ", ""):
            self.incomplete(answer(GOOD, finish=finish), "is not a normal stop")
        missing = answer(GOOD)
        del missing["candidates"][0]["finishReason"]
        self.incomplete(missing, "finish reason (missing or unrecognised) is not a normal stop")
        self.incomplete({"promptFeedback": {"blockReason": "SAFETY"}, "responseId": "resp-synth-2"},
                        "the provider blocked the request (SAFETY)")
        self.incomplete(answer(GOOD, promptFeedback={"blockReason": SENTINEL}),
                        "the provider blocked the request (reason not named)")

    def test_an_empty_answer(self):
        for text in ("", " \n\t\n"):
            self.incomplete(answer(text), "the answer is empty")
        self.incomplete(answer(GOOD, candidates=[{"content": {"parts": []}, "finishReason": "STOP"}]), "empty")
        self.incomplete(answer(GOOD, candidates=[{"finishReason": "STOP"}]), "the answer is empty")

    def test_an_answer_that_is_not_one_candidate(self):
        one = answer()["candidates"][0]
        for reply in ({}, [], "VERDICT: AGREE", 200, answer(candidates=[]), answer(candidates=[one, one]),
                      answer(candidates="VERDICT: AGREE"), answer(candidates=[GOOD])):
            self.incomplete(reply, "the")

    def test_a_missing_or_malformed_verdict_line(self):
        for text in ("Looks good to me.\n", "verdict: agree\nfine", "VERDICT: AGREE.\nfine", " VERDICT: AGREE\nfine",
                     "**VERDICT: AGREE**\nfine", "VERDICT: AGREED\nfine", "VERDICT:AGREE\nfine",
                     "VERDICT: AGREE BLOCKERS\nfine", "VERDICT: AGREE \nfine", "VERDICT: AGREE\r\nfine",
                     "\nVERDICT: AGREE\nfine", "I cannot say VERDICT: AGREE\nfine", "VERDICT: NOT AGREE\nfine",
                     "Findings first.\nVERDICT: AGREE\n", "VERDICT: AGREE, no VERDICT: BLOCKERS\n"):
            self.incomplete(answer(text), "the first line is not a verdict line")

    def test_an_answer_that_gives_both_verdicts(self):
        self.incomplete(answer("VERDICT: AGREE\nSection 1, page 1: fine.\nVERDICT: BLOCKERS\n"), "both verdicts")
        self.incomplete(answer("VERDICT: BLOCKERS\nSection 1, page 1: cut.\nVERDICT: AGREE"), "both verdicts")
        twice = answer("VERDICT: AGREE\nSection 1, page 1.\nVERDICT: AGREE\n")      # said twice, still one verdict
        self.assertEqual("AGREE", Rig(reply=twice).run()["status"])

    def test_a_verdict_in_the_models_thinking_is_not_its_answer(self):
        thought = {"content": {"parts": [{"text": "VERDICT: AGREE\n", "thought": True},
                                         {"text": "I could not open the PDF.\n"}]}, "finishReason": "STOP"}
        rig, _ = self.incomplete(answer(candidates=[thought]), "the first line is not a verdict line")
        self.assertNotIn(b"VERDICT: AGREE", rig.written[0][1])

    def test_an_error_or_a_timeout_from_the_provider(self):
        for error, why in ((TimeoutError(SENTINEL), "TimeoutError from the provider"),
                           (http_error(self, 429), "HTTP 429 from the provider"),
                           (urllib.error.URLError(SENTINEL), "URLError from the provider"),
                           (ValueError(GOOD), "ValueError from the provider"),
                           (json.JSONDecodeError(SENTINEL, GOOD, 0), "JSONDecodeError from the provider")):
            rig, out = self.incomplete(error, why)
            self.assertNotIn(SENTINEL, json.dumps(out) + "\n".join(rig.lines))

    def test_the_grammar_is_a_whole_line_never_a_substring(self):
        read = pr.read_answer(answer(GOOD))
        self.assertEqual(("AGREE", True, GOOD), (read["verdict"], read["complete"], read["text"]))
        for text in ("VERDICT: AGREE", "VERDICT: BLOCKERS"):
            self.assertTrue(pr.read_answer(answer(text))["complete"])
            for wrapped in ("x" + text, text + "x", text.lower(), text.replace(": ", ":  ")):
                read = pr.read_answer(answer(wrapped + "\nfindings"))
                self.assertEqual((False, ""), (read["complete"], read["verdict"]), wrapped)

    def test_provider_labels_are_kept_only_in_a_label_shape(self):
        read = pr.read_answer(answer(GOOD, finish="not a label: " + SENTINEL, responseId="two words " + SENTINEL,
                                     modelVersion=["x"], usageMetadata={"totalTokenCount": "many"}))
        self.assertEqual(("", "", "", {}), (read["finish_reason"], read["response_id"], read["model_version"],
                                            read["usage"]))
        self.assertFalse(read["complete"])


class Deadline(unittest.TestCase):
    """One absolute deadline: the call gets what is left of it, and a late answer is not an answer."""

    def test_the_call_gets_only_the_time_that_is_left(self):
        rig = Rig(fetch_takes=5.0)
        self.assertEqual("AGREE", rig.run()["status"])
        self.assertEqual(140.0, rig.asked[0]["timeout"])

    def test_an_answer_after_the_deadline_is_incomplete(self):
        rig = Rig(took=150.5)
        out = rig.run()
        self.assertEqual(("INCOMPLETE", 1), (out["status"], out["provider_calls"]))
        self.assertIn("the answer arrived after the deadline", out["reason"])
        self.assertIn(b"- **status:** INCOMPLETE", rig.written[0][1])

    def test_an_answer_just_inside_the_deadline_counts(self):
        self.assertEqual("AGREE", Rig(took=150.0).run()["status"])


class PrivateResult(unittest.TestCase):
    """The whole result goes through the private writer only, and counts only once it reads back."""

    def test_the_result_holds_the_verdict_the_whole_text_and_the_receipt(self):
        rig = Rig()
        out = rig.run()
        destination, content = rig.written[0]
        self.assertEqual({"repo": "sfdc-24/conference", "path": "docs/reviews/gemini/%s.md" % RID}, destination)
        text = content.decode("utf-8")
        for held in ("# Private review %s: AGREE" % RID, "- **status:** AGREE", GOOD,
                     "- **reviewed:** sfdc-24/conference #7 at %s" % HEAD,
                     "- **submitted:** docs/synthetic.md (text/markdown), %d bytes, sha256 %s" % (len(MD), sha(MD)),
                     "- **submitted:** docs/synthetic.pdf (application/pdf), %d bytes, sha256 %s"
                     % (len(PDF), sha(PDF)),
                     "- **route:** gemini api-key, key variable GEMINI_API_KEY",
                     "- **model:** gemini-pro-latest (served as gemini-pro-synth-001)",
                     "- **max_output_tokens:** 8192", "- **response_id:** resp-synth-1",
                     "- **finish_reason:** STOP", "candidatesTokenCount 40, promptTokenCount 9000",
                     "- **started_at:** 2026-01-02T03:04:05Z", "- **called_at:** 2026-01-02T03:04:05Z",
                     "- **answered_at:** 2026-01-02T03:04:05Z"):
            self.assertIn(held, text)
        self.assertEqual(sha(content), out["result_sha256"])
        self.assertEqual(LINK, out["board"]["result"])

    def test_the_receipt_times_are_taken_in_order(self):
        times = iter(["2026-01-02T03:04:05Z", "2026-01-02T03:04:09Z", "2026-01-02T03:05:30Z"])
        rig = Rig()
        rig.run(now=lambda: next(times))
        text = rig.written[0][1].decode("utf-8")
        self.assertIn("- **started_at:** 2026-01-02T03:04:05Z\n- **called_at:** 2026-01-02T03:04:09Z\n"
                      "- **answered_at:** 2026-01-02T03:05:30Z\n", text)

    def test_a_result_that_does_not_read_back_is_blocked(self):
        for stored in (b"", b"something else", "str, not bytes", 0, OSError(SENTINEL), http_error(self, 404)):
            rig = Rig(stored=stored)
            out = rig.run()
            self.assertEqual(("BLOCKED", 1), (out["status"], out["provider_calls"]), stored)
            self.assertEqual("", out["board"]["result"])
            self.assertNotIn("AGREE", json.dumps(out))
        rig = Rig()
        out = rig.run(read_result=lambda destination, link: rig.written[-1][1] + b"\n")
        self.assertEqual("the private result did not read back as written", out["reason"])

    def test_a_writer_that_fails_is_blocked(self):
        for error, why in ((http_error(self, 403),
                            "HTTP 403 while writing the private result; the provider was called once"),
                           (OSError(SENTINEL), "OSError while writing the private result")):
            rig = Rig(link=error)
            out = rig.run()
            self.assertEqual("BLOCKED", out["status"])
            self.assertIn(why, out["reason"])
            self.assertNotIn("read back", rig.events)

    def test_a_link_outside_the_private_repository_is_blocked(self):
        path = "docs/reviews/gemini/%s.md" % RID
        for link in ("https://github.com/sfdc-24/Blackboard/pull/1", "http://github.com/sfdc-24/conference/pull/1",
                     "https://github.com/sfdc-24/conference/pull/1?note=" + SENTINEL,
                     "https://github.com/sfdc-24/conference/blob/main/" + path,
                     "https://github.com/sfdc-24/conference/blob/%s/docs/other.md" % HEAD,
                     "https://github.com/sfdc-24/conference/pull/1\n" + GOOD, SENTINEL, "", None, 4242):
            rig = Rig(link=link)
            out = rig.run()
            self.assertEqual("BLOCKED", out["status"], link)
            self.assertIn("returned no link into sfdc-24/conference", out["reason"])
            self.assertEqual("", out["board"]["result"])
            self.assertNotIn(SENTINEL, json.dumps(out) + "\n".join(rig.lines))
        commit = "https://github.com/sfdc-24/conference/blob/%s/%s" % ("c3" * 20, path)
        self.assertEqual(commit, Rig(link=commit).run()["board"]["result"])

    def test_a_repository_that_turned_public_after_the_call_gets_nothing_written(self):
        rig = Rig(private=[True, False])
        out = rig.run()
        self.assertEqual(("BLOCKED", 1), (out["status"], out["provider_calls"]))
        self.assertIn("is not private; the result was not written", out["reason"])
        self.assertEqual([], rig.written)

    def test_an_incomplete_answer_is_stored_privately_too(self):
        rig = Rig(reply=TimeoutError("timed out"))
        out = rig.run()
        text = rig.written[0][1].decode("utf-8")
        self.assertIn("- **why:** TimeoutError from the provider", text)
        self.assertIn("(no text)", text)
        self.assertEqual(("INCOMPLETE", LINK), (out["board"]["status"], out["board"]["result"]))


class BoardAndLogs(unittest.TestCase):
    """The board payload and every log line are metadata. The sentinel is in the source, the PDF and the answer."""

    def scenarios(self):
        clipped = {"docs/synthetic.md": MD[:24000], "docs/synthetic.pdf": PDF}
        yield "agree", Rig(), ROW
        yield "blockers", Rig(reply=answer("VERDICT: BLOCKERS\nSection 1, page 1: " + SENTINEL + "\n")), ROW
        yield "truncated", Rig(reply=answer(GOOD, finish="MAX_TOKENS")), ROW
        yield "no verdict", Rig(reply=answer(SENTINEL + " is what the document says.")), ROW
        yield "provider error", Rig(reply=RuntimeError(SENTINEL)), ROW
        yield "label abuse", Rig(reply=answer(GOOD, finish=SENTINEL, responseId="id " + SENTINEL,
                                              modelVersion=SENTINEL + " " + GOOD)), ROW
        yield "clipped", Rig(blobs=clipped), ROW
        yield "fetch error", Rig(blobs={"docs/synthetic.md": KeyError(SENTINEL), "docs/synthetic.pdf": PDF}), ROW
        yield "moved", Rig(heads=[HEAD, SENTINEL]), ROW
        yield "repeat", Rig(used={RID}), ROW
        yield "no read-back", Rig(stored=GOOD.encode("utf-8")), ROW
        yield "bad link", Rig(link="https://github.com/sfdc-24/conference/pull/1#" + SENTINEL), ROW
        yield "forged row", Rig(), ROW + "|path=docs/%s.md|note=%s" % (SENTINEL, SENTINEL)
        yield "unknown id", Rig(), "BCB|v=1|review=%s" % SENTINEL

    def test_the_sentinel_never_reaches_the_board_payload_the_outcome_or_the_logs(self):
        self.assertIn(SENTINEL.encode("ascii"), MD)
        self.assertIn(SENTINEL.encode("ascii"), PDF)
        self.assertIn(SENTINEL, GOOD)
        encoded = base64.b64encode(PDF).decode("ascii")
        statuses = set()
        for name, rig, row in self.scenarios():
            out_stream, err_stream = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_stream), contextlib.redirect_stderr(err_stream):
                out = pr.run_selected(row, [manifest()], **rig.wiring(log=None))     # the default log: print
            statuses.add(out["status"])
            seen = "\n".join([json.dumps(out), json.dumps(out["board"]), pr.board_line(out["board"]),
                              out_stream.getvalue(), err_stream.getvalue()])
            self.assertTrue(out_stream.getvalue().strip(), name)                    # it did log something
            for private in (SENTINEL, "Synthetic architecture", "reads the same in both", "Section 1",
                            "line 2 of the body", encoded[:80], encoded[-80:]):
                self.assertNotIn(private, seen, name)
        self.assertEqual({"AGREE", "BLOCKERS", "INCOMPLETE", "BLOCKED"}, statuses)

    def test_the_sentinel_does_reach_the_private_result_and_nowhere_else(self):
        rig = Rig()
        out = rig.run()
        self.assertIn(SENTINEL.encode("ascii"), rig.written[0][1])
        self.assertNotIn(SENTINEL, json.dumps(out) + "\n".join(rig.lines))

    def test_the_board_payload_is_exactly_six_metadata_fields(self):
        out = Rig().run()
        self.assertEqual({"review_id": RID, "head": HEAD, "markdown_sha256": sha(MD), "pdf_sha256": sha(PDF),
                          "status": "AGREE", "result": LINK}, out["board"])
        self.assertEqual(["status", "reason", "provider_calls", "result_sha256", "board"], list(out))
        blocked = Rig(heads=["b2" * 20]).run()["board"]
        self.assertEqual(("review_id", "head", "markdown_sha256", "pdf_sha256", "status", "result"), tuple(blocked))
        self.assertEqual(("BLOCKED", "", HEAD), (blocked["status"], blocked["result"], blocked["head"]))

    def test_the_board_line_is_those_fields_and_cannot_re_ask(self):
        line = pr.board_line(Rig().run()["board"])
        self.assertEqual("PRIVATE-REVIEW|review=%s|head=%s|markdown_sha256=%s|pdf_sha256=%s|status=AGREE|result=%s"
                         % (RID, HEAD, sha(MD), sha(PDF), LINK), line)
        self.assertLess(len(line), 400)                     # whole inside the board's 1,500-character reply
        chosen, why = pr.select(line, [manifest()])
        self.assertIsNone(chosen)
        self.assertIn("a row may only select a review", why)
        with self.assertRaises(ValueError):
            pr.board_line({"review_id": RID, "status": "AGREE", "text": GOOD})

    def test_the_payload_takes_no_status_or_link_outside_the_closed_sets(self):
        with self.assertRaises(ValueError):
            pr.board_payload(manifest(), "AGREE: " + GOOD)
        with self.assertRaises(ValueError):
            pr.board_payload(manifest(), "AGREE", "https://example.test/" + SENTINEL)
        with self.assertRaises(ValueError):
            pr.board_payload(None, "BLOCKED", LINK)
        with self.assertRaises(pr.Blocked):
            pr.board_payload(dict(manifest(), note=GOOD), "AGREE")


class FakeResponse:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, n):
        chunk, self.raw = self.raw[:n], self.raw[n:]
        return chunk


class FakeOpener:
    """Stands in for urllib's opener, so the real call's request can be read without sending it."""

    def __init__(self, raw=b"{}"):
        self.raw, self.requests = raw, []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        return FakeResponse(self.raw)


class RealCall(unittest.TestCase):
    """call_gemini, the default provider, with its opener replaced: the request is built and never sent."""

    KEY = "synthetic-key-not-a-secret"
    HOW = dict(route="api-key", model="gemini-pro-latest", key_env="GEMINI_API_KEY", timeout=100.0)

    def test_the_key_is_an_unredirected_header_never_the_url_and_never_printed(self):
        body = pr.build_request(manifest(), MD, PDF)
        opener = FakeOpener(json.dumps(answer()).encode("utf-8"))
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            got = pr.call_gemini(body, env={"GEMINI_API_KEY": self.KEY}, opener=opener, **self.HOW)
        self.assertEqual(answer(), got)
        req, timeout = opener.requests[0]
        self.assertEqual("https://generativelanguage.googleapis.com/v1beta/models/gemini-pro-latest:generateContent",
                         req.full_url)
        self.assertEqual(self.KEY, req.unredirected_hdrs["X-goog-api-key"])
        self.assertNotIn(self.KEY, req.full_url + json.dumps(dict(req.headers)))
        self.assertEqual(("POST", 100.0), (req.get_method(), timeout))
        self.assertEqual(body, json.loads(req.data))
        self.assertEqual("", printed.getvalue())

    def test_no_key_or_a_name_outside_the_closed_sets_sends_nothing(self):
        opener = FakeOpener()
        with self.assertRaises(LookupError):
            pr.call_gemini({}, env={}, opener=opener, **self.HOW)
        for over in ({"route": "vertex-adc"}, {"model": "gemini-x/../../files"}, {"key_env": "GEMINI_GITHUB_TOKEN"}):
            with self.assertRaises(ValueError):
                pr.call_gemini({}, env={"GEMINI_API_KEY": self.KEY, "GEMINI_GITHUB_TOKEN": "t"}, opener=opener,
                               **dict(self.HOW, **over))
        with self.assertRaises(TimeoutError):
            pr.call_gemini({}, env={"GEMINI_API_KEY": self.KEY}, opener=opener, **dict(self.HOW, timeout=0))
        self.assertEqual([], opener.requests)

    def test_a_redirect_is_refused_not_followed(self):
        self.assertTrue(any(isinstance(h, pr._NoRedirect) for h in pr._OPENER.handlers))
        req = urllib.request.Request(pr.GENERATE % "gemini-pro-latest")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            pr._NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://elsewhere.test/collect")
        caught.exception.close()

    def test_the_deadline_is_checked_between_reads(self):
        ticks = iter([0.0, 0.0, 100.0])
        with self.assertRaises(TimeoutError):
            pr.call_gemini({}, env={"GEMINI_API_KEY": self.KEY}, opener=FakeOpener(b"{" + b" " * 70000 + b"}"),
                           clock=lambda: next(ticks), **self.HOW)

    def test_an_answer_over_the_cap_or_not_json_raises(self):
        with self.assertRaises(ValueError):
            pr.call_gemini({}, env={"GEMINI_API_KEY": self.KEY}, clock=lambda: 0.0,
                           opener=FakeOpener(b" " * (pr.MAX_RESPONSE_BYTES + 1)), **self.HOW)
        with self.assertRaises(ValueError):
            pr.call_gemini({}, env={"GEMINI_API_KEY": self.KEY}, opener=FakeOpener(b"<html>"), **self.HOW)

    def test_run_without_a_provider_uses_it_once_with_the_manifests_route(self):
        rig = Rig()
        env = {"GEMINI_API_KEY": self.KEY}
        with mock.patch.object(pr, "call_gemini", return_value=answer()) as real:
            out = rig.run(provider=None, env=env)
        self.assertEqual(("AGREE", 1), (out["status"], out["provider_calls"]))
        real.assert_called_once()
        self.assertEqual(dict(self.HOW, timeout=150.0, env=env),
                         {k: v for k, v in real.call_args.kwargs.items() if k != "clock"})
        self.assertNotIn(self.KEY, json.dumps(out) + "\n".join(rig.lines) + rig.written[0][1].decode("utf-8"))


class CommandLine(unittest.TestCase):
    def main(self, argv, m=None, **kwargs):
        printed = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "manifest.json")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(json.dumps(manifest() if m is None else m))
            argv = [str(path) if a == "PATH" else a for a in argv]
            if "env" in kwargs:
                kwargs["env"] = {k: (str(path) if v == "PATH" else v) for k, v in kwargs["env"].items()}
            with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
                code = pr.main(argv, **kwargs)
        return code, printed.getvalue()

    def test_check_validates_offline_and_prints_metadata(self):
        code, printed = self.main(["check", "--manifest", "PATH"], env={})
        self.assertEqual(0, code)
        self.assertIn("manifest ok: review %s, sfdc-24/conference #7 at %s" % (RID, HEAD), printed)

    def test_the_path_can_come_from_the_operators_environment(self):
        self.assertEqual(0, self.main(["check"], env={"PRIVATE_REVIEW_MANIFEST": "PATH"})[0])
        code, printed = self.main(["check"], env={})
        self.assertEqual(2, code)
        self.assertIn("no manifest", printed)

    def test_a_forged_manifest_is_refused(self):
        code, printed = self.main(["run", "--manifest", "PATH"], dict(manifest(), scope="all"), env={})
        self.assertEqual(1, code)
        self.assertIn("BLOCKED", printed)

    def test_run_from_the_command_line_is_not_wired_and_calls_nothing(self):
        with mock.patch.object(pr, "call_gemini", side_effect=AssertionError("called")) as real:
            code, printed = self.main(["run", "--manifest", "PATH"], env={"GEMINI_API_KEY": "synthetic"})
        self.assertEqual(1, code)
        self.assertIn("BLOCKED: not wired", printed)
        self.assertIn("|status=BLOCKED|result=\n", printed)
        real.assert_not_called()

    def test_a_wired_run_prints_the_board_line_and_no_text(self):
        rig = Rig()
        code, printed = self.main(["run", "--manifest", "PATH"], wiring=rig.wiring(), env={})
        self.assertEqual(0, code)
        self.assertEqual(pr.board_line(pr.board_payload(manifest(), "AGREE", LINK)) + "\n", printed)
        self.assertNotIn(SENTINEL, printed)


class NotActivated(unittest.TestCase):
    """Writing this file activated nothing. Activation is its own owner-gated change, and it edits this test."""

    def test_the_image_and_the_public_path_do_not_carry_or_import_it(self):
        for path in ("cloud/agent-waker/Dockerfile", ".gcloudignore", "cloud/agent-waker/main.py",
                     "scripts/agent_waker.py", "scripts/gemini_agent.py", "scripts/repo_context.py",
                     "scripts/okf_land.py"):
            self.assertNotIn("private_review", (REPO / path).read_text(encoding="utf-8"), path)

    def test_it_imports_only_the_standard_library_and_has_no_way_to_post(self):
        tree = ast.parse((REPO / "scripts" / "private_review.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual({"__future__", "argparse", "base64", "datetime", "hashlib", "json", "os", "re", "sys",
                          "time", "urllib.error", "urllib.request"}, imported)


if __name__ == "__main__":
    unittest.main()
