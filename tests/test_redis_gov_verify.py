#!/usr/bin/env python3
"""haudit: a generation's canonical digest and length, and nothing stored.

Offline. A fake Redis, synthetic hashes, no network. The flag defaults off. With it on, the
result may carry digests, lengths, counts, coverage and the generation and correlation ids.
A stored string, a token, or a timeout message must not.
"""
import base64
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bus_request as br                                                 # noqa: E402
import redis_gov as gov                                                  # noqa: E402
from test_redis_gov import ACL, FakeRedis                               # noqa: E402

SECRET = "SECRET-PAYLOAD-do-not-leak"
TOKEN = "ghp_AbCdEf0123456789token"
HEAD_SECRET = "SECRET-HEAD-do-not-leak"
NOW_TEXT = "2026-10-11T15:40:00Z"


def generation(char="a"):
    if char not in "0123456789abcdef":
        raise AssertionError(char)
    return char * 64


def prefix_for(gen, root="proj:pr-board:v1:g:"):
    return root + gen[:16] + ":"


def b64_manifest(expected):
    raw = json.dumps({"keys": expected}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def spec(mapping):
    digest, length = gov.payload_digest(mapping)
    return {"sha256": digest, "len": length}


class AuditRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.scans = 0
        self.gets = 0

    def scan_iter(self, match=None, count=None):
        import fnmatch
        self.scans += 1
        for key in list(self.data):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def get(self, key):
        self.gets += 1
        return super().get(key)


def put_hash(conn, key, mapping):
    conn.hset(key, mapping=dict(mapping))


def audit(conn, gen, expected, cid="cid-audit-1", who="cursor", head=None, prefix=None, **kw):
    prefix = prefix or prefix_for(gen)
    head = head or "proj:pr-board:v1:head"
    return gov.execute(conn, ACL, who, "REQ-AUDIT/ROW-1", gov.VERIFY_OP, prefix,
                       raw_value=b64_manifest(expected), enc="b64", generation=gen,
                       correlation=cid, head=head, **kw)


class FlagAndExistingOps(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(gov.VERIFY_FLAG, None)

    def tearDown(self):
        self._env.stop()

    def test_the_flag_defaults_off_and_only_explicit_on_enables_it(self):
        self.assertFalse(gov.verifier_enabled())
        for value in ("", "0", "false", "no", "off", "maybe"):
            self.assertFalse(gov.verifier_enabled({gov.VERIFY_FLAG: value}), value)
        for value in ("1", "true", "on", "yes", "TRUE"):
            self.assertTrue(gov.verifier_enabled({gov.VERIFY_FLAG: value}), value)

    def test_flag_off_refuses_the_op_and_existing_reads_are_unchanged(self):
        conn = AuditRedis()
        gen = generation("a")
        key = prefix_for(gen) + "meta"
        put_hash(conn, key, {"body": SECRET, "token": TOKEN})
        out = audit(conn, gen, {key: spec({"body": SECRET, "token": TOKEN})}, cid="cid-off-1")
        self.assertFalse(out["ok"])
        self.assertIn("off", out["refused"])
        self.assertEqual("cid-off-1", out["correlation"])
        self.assertEqual(gen, out["generation"])
        self.assertEqual(0, conn.scans)
        blob = json.dumps(out)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(TOKEN, blob)
        line = gov.summarise(out)
        self.assertNotIn(SECRET, line)
        self.assertNotIn("|", line)
        self.assertIn("cid-off-1", line)

        got = gov.execute(conn, ACL, "cursor", "via", "get", "fleet:inbox:cursor")
        conn.set("fleet:inbox:cursor", SECRET)
        got = gov.execute(conn, ACL, "cursor", "via", "get", "fleet:inbox:cursor")
        self.assertTrue(got["ok"])
        self.assertEqual(SECRET, got["value"])
        many = {str(n): "x" for n in range(gov.MAX_HASH_FIELDS + 3)}
        put_hash(conn, "proj:preview:hash", many)
        hashed = gov.execute(conn, ACL, "cursor", "via", "hgetall", "proj:preview:hash")
        self.assertTrue(hashed["truncated"])
        self.assertEqual(gov.MAX_HASH_FIELDS, len(hashed["value"]))
        self.assertNotIn(gov.VERIFY_OP, hashed)

    def test_flag_on_does_not_change_get_or_hgetall(self):
        os.environ[gov.VERIFY_FLAG] = "1"
        conn = AuditRedis()
        conn.set("proj:plain", "preview-me")
        got = gov.execute(conn, ACL, "cursor", "via", "get", "proj:plain")
        self.assertEqual({"ok": True, "op": "get", "key": "proj:plain", "found": True,
                          "value": "preview-me", "audit": "ok"}, {k: got[k] for k in got})


class CanonicalUtf8(unittest.TestCase):
    def test_unicode_normalisation_and_ordering_are_stable(self):
        nfc = {"b": "caf\u00e9", "a": "2"}
        nfd = {"a": "2", "b": "cafe\u0301"}
        self.assertEqual(gov.canonical_utf8(nfc), gov.canonical_utf8(nfd))
        self.assertEqual(gov.canonical_utf8({"z": "1", "a": "2"}),
                         gov.canonical_utf8({"a": "2", "z": "1"}))
        raw = gov.canonical_utf8(nfc)
        self.assertEqual(b'{"a":"2","b":"caf\xc3\xa9"}', raw)
        digest, length = gov.payload_digest(nfc)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        self.assertEqual(len(raw), length)
        self.assertGreater(length, len(raw.decode("utf-8")))
        # hput's existing canonical is untouched: it does not normalise.
        self.assertNotEqual(gov.canonical({"a": "e\u0301"}), gov.canonical({"a": "\u00e9"}))

    def test_shared_contract_cross_check_vector(self):
        documents = {"a": {"x": "1"}, "b": {"y": "\u00e9"}}
        raw = {name: gov.canonical_utf8(fields) for name, fields in documents.items()}
        self.assertEqual(b'{"x":"1"}', raw["a"])
        self.assertEqual('{"y":"\u00e9"}'.encode("utf-8"), raw["b"])
        specs = {}
        for name, blob in raw.items():
            digest = hashlib.sha256(blob).hexdigest()
            specs[name] = {"sha256": digest, "len": len(blob)}
            self.assertEqual(specs[name], dict(zip(("sha256", "len"), gov.payload_digest(documents[name]))))
        self.assertEqual(
            "013bdcb2b4d38b7d26f13f2a48631e9a5ae45530c3ef0e5946668d4f1892baa1", specs["a"]["sha256"])
        self.assertEqual(9, specs["a"]["len"])
        self.assertEqual(
            "e37ae44535cf9436886c8517b4a6346e25fcd0501667d96726dd1831b3b7b232", specs["b"]["sha256"])
        self.assertEqual(10, specs["b"]["len"])
        self.assertEqual(
            b'{"keys":{"a":{"len":9,"sha256":"013bdcb2b4d38b7d26f13f2a48631e9a5ae45530c3ef0e5946668d4f1892baa1"},'
            b'"b":{"len":10,"sha256":"e37ae44535cf9436886c8517b4a6346e25fcd0501667d96726dd1831b3b7b232"}}}',
            gov.manifest_body(specs))
        self.assertEqual("7ee61c4addbd4d475a50ace20c2060118887e158a0b85e8529a009e8c509d1fc",
                         gov.manifest_digest(specs))
        report_hex, report_len = gov.report_digest(specs)
        self.assertEqual(
            b"a\t013bdcb2b4d38b7d26f13f2a48631e9a5ae45530c3ef0e5946668d4f1892baa1\t9\n"
            b"b\te37ae44535cf9436886c8517b4a6346e25fcd0501667d96726dd1831b3b7b232\t10",
            gov.report_body(specs))
        self.assertEqual("d7f90960d6066e7208157ed3b0a559ec11d32a325d46686f23dea23ba6cb6960", report_hex)
        self.assertEqual(138, report_len)
        self.assertNotEqual(report_hex, gov.manifest_digest(specs))

    def test_a_stored_nfd_hash_matches_the_nfc_digest_and_hides_the_text(self):
        os.environ[gov.VERIFY_FLAG] = "1"
        conn = AuditRedis()
        gen = generation("b")
        key = prefix_for(gen) + "meta"
        stored = {"note": "private-caf\u00e9-value", "n": "1"}
        put_hash(conn, key, {"note": "private-cafe\u0301-value", "n": "1"})
        put_hash(conn, "proj:pr-board:v1:head", {"generation": gen, "witness": HEAD_SECRET})
        out = audit(conn, gen, {key: spec(stored)})
        self.assertTrue(out["ok"], out)
        self.assertEqual("match", out["keys"][0]["status"])
        self.assertEqual(spec(stored)["sha256"], out["keys"][0]["sha256"])
        self.assertEqual(spec(stored)["len"], out["keys"][0]["len"])
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn("private-caf", blob)
        self.assertNotIn(HEAD_SECRET, blob)


class GenerationAudit(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {gov.VERIFY_FLAG: "1"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _plant(self, conn, gen, hashes, head_generation=None):
        for key, mapping in hashes.items():
            put_hash(conn, key, mapping)
        put_hash(conn, "proj:pr-board:v1:head", {
            "generation": head_generation if head_generation is not None else gen,
            "witness": HEAD_SECRET,
            "advisory": "1",
        })

    def test_every_key_in_the_generation_is_covered(self):
        conn = AuditRedis()
        gen = generation("c")
        prefix = prefix_for(gen)
        one = {prefix + "meta": {"rows": "alpha", "seq": "0"}}
        two = {prefix + "i:0": {"rows": "beta", "seq": "1"}}
        self._plant(conn, gen, {**one, **two})
        expected = {key: spec(mapping) for key, mapping in {**one, **two}.items()}
        out = audit(conn, gen, expected, cid="cid-covered-1")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["coverage"]["covered"])
        self.assertEqual(2, out["coverage"]["expected"])
        self.assertEqual(2, out["coverage"]["seen"])
        self.assertEqual(2, out["coverage"]["matched"])
        self.assertEqual(0, out["coverage"]["missing"])
        self.assertEqual(0, out["coverage"]["extra"])
        self.assertEqual(0, out["coverage"]["changed"])
        self.assertFalse(out["partial"])
        self.assertFalse(out["moved"])
        self.assertFalse(out["timeout"])
        self.assertEqual("cid-covered-1", out["correlation"])
        self.assertEqual(gen, out["generation"])
        self.assertEqual(gen, out["head_generation"])
        self.assertEqual({"match", "match"}, {entry["status"] for entry in out["keys"]})
        lines = ["%s\t%s\t%d" % (entry["key"], entry["sha256"], entry["len"])
                 for entry in sorted(out["keys"], key=lambda item: item["key"])]
        report = "\n".join(lines).encode("utf-8")
        self.assertEqual(hashlib.sha256(report).hexdigest(), out["digest"])
        self.assertEqual(len(report), out["length"])
        self.assertEqual(2, out["listed"])
        self.assertEqual(gov.manifest_digest(expected), out["manifest_digest"])
        self.assertNotEqual(out["digest"], out["manifest_digest"])
        self.assertEqual(0, conn.scans)
        blob = json.dumps(out)
        self.assertNotIn("alpha", blob)
        self.assertNotIn("beta", blob)
        self.assertNotIn(HEAD_SECRET, blob)
        self.assertNotIn("value", out)
        line = gov.summarise(out)
        self.assertIn("covered=1", line)
        self.assertIn("cid-covered-1", line)
        self.assertNotIn("|", line)
        self.assertNotIn("\n", line)

    def test_missing_extra_and_changed_are_each_detected(self):
        conn = AuditRedis()
        gen = generation("d")
        prefix = prefix_for(gen)
        match = {prefix + "ok": {"body": "kept"}}
        changed = {prefix + "bad": {"body": SECRET}}
        extra = {prefix + "extra": {"body": TOKEN}}
        missing = prefix + "gone"
        self._plant(conn, gen, {**match, **changed, **extra})
        expected = {
            prefix + "ok": spec(match[prefix + "ok"]),
            prefix + "bad": spec({"body": "something-else"}),
            missing: spec({"body": "never-stored"}),
        }
        out = audit(conn, gen, expected, cid="cid-mix-1")
        by_status = {}
        for entry in out["keys"]:
            by_status.setdefault(entry["status"], []).append(entry["key"])
        self.assertEqual([prefix + "ok"], by_status["match"])
        self.assertEqual([prefix + "bad"], by_status["changed"])
        self.assertEqual([missing], by_status["missing"])
        self.assertNotIn("extra", by_status)
        self.assertEqual(1, out["coverage"]["missing"])
        self.assertEqual(0, out["coverage"]["extra"])
        self.assertEqual(1, out["coverage"]["changed"])
        self.assertEqual(1, out["coverage"]["matched"])
        self.assertEqual(0, conn.scans)
        self.assertFalse(out["ok"])
        self.assertFalse(out["coverage"]["covered"])
        self.assertFalse(out["partial"])
        self.assertEqual("cid-mix-1", out["correlation"])
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(HEAD_SECRET, blob)
        self.assertIn("changed=1", gov.summarise(out))
        self.assertIn("missing=1", gov.summarise(out))
        self.assertIn("extra=0", gov.summarise(out))

    def test_head_movement_during_the_audit_is_not_a_pass(self):
        conn = AuditRedis()
        gen = generation("e")
        prefix = prefix_for(gen)
        key = prefix + "meta"
        mapping = {"body": SECRET}
        self._plant(conn, gen, {key: mapping})
        real = conn.hgetall
        state = {"done": False}

        def hgetall(name):
            if name.startswith(prefix) and not state["done"]:
                state["done"] = True
                FakeRedis.hset(conn, "proj:pr-board:v1:head", "generation", "SECRET-HEAD-MOVED")
            return real(name)

        conn.hgetall = hgetall
        out = audit(conn, gen, {key: spec(mapping)}, cid="cid-moved-1")
        self.assertTrue(state["done"])
        self.assertTrue(out["moved"], out)
        self.assertFalse(out["ok"])
        self.assertFalse(out["coverage"]["covered"])
        self.assertEqual("cid-moved-1", out["correlation"])
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("SECRET-HEAD-MOVED", blob)

    def test_a_partial_read_is_never_ok(self):
        conn = AuditRedis()
        gen = generation("f")
        prefix = prefix_for(gen)
        keys = {prefix + "a": {"n": "1"}, prefix + "b": {"n": "2"}}
        self._plant(conn, gen, keys)
        real = conn.hgetall

        def hgetall(name):
            if str(name).endswith("b"):
                raise RuntimeError("page ended early " + SECRET)
            return real(name)

        conn.hgetall = hgetall
        expected = {key: spec(mapping) for key, mapping in keys.items()}
        out = audit(conn, gen, expected, cid="cid-partial-1")
        self.assertTrue(out["partial"], out)
        self.assertFalse(out["ok"])
        self.assertFalse(out["coverage"]["covered"])
        self.assertEqual("cid-partial-1", out["correlation"])
        self.assertNotIn(SECRET, json.dumps(out) + gov.summarise(out))
        self.assertEqual(0, conn.scans)

    def test_a_timeout_is_surfaced_and_its_message_is_not(self):
        conn = AuditRedis()
        gen = generation("1")
        key = prefix_for(gen) + "meta"
        self._plant(conn, gen, {key: {"body": SECRET}})

        def hget(key, field):
            raise TimeoutError("timed out at 10.9.9.9 with " + TOKEN)

        conn.hget = hget
        out = audit(conn, gen, {key: spec({"body": SECRET})}, cid="cid-time-1")
        self.assertFalse(out["ok"])
        self.assertTrue(out["timeout"])
        self.assertTrue(out["partial"])
        self.assertEqual("TimeoutError", out["error"])
        self.assertEqual("cid-time-1", out["correlation"])
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn("10.9.9.9", blob)
        self.assertNotIn(SECRET, blob)

    def test_a_timeout_mid_read_does_not_pass(self):
        conn = AuditRedis()
        gen = generation("2")
        prefix = prefix_for(gen)
        keys = {prefix + "a": {"n": "1"}, prefix + "b": {"body": SECRET}}
        self._plant(conn, gen, keys)
        real = conn.hgetall
        seen = {"n": 0}

        def hgetall(name):
            if str(name).startswith(prefix):
                seen["n"] += 1
                if seen["n"] >= 2:
                    raise TimeoutError("second key blew up " + SECRET)
            return real(name)

        conn.hgetall = hgetall
        expected = {key: spec(mapping) for key, mapping in keys.items()}
        out = audit(conn, gen, expected, cid="cid-time-2")
        self.assertTrue(out["timeout"], out)
        self.assertTrue(out["partial"])
        self.assertFalse(out["ok"])
        self.assertEqual("cid-time-2", out["correlation"])
        self.assertNotIn(SECRET, json.dumps(out) + gov.summarise(out))

    def test_a_head_pointing_at_another_generation_is_not_a_pass(self):
        conn = AuditRedis()
        gen = generation("8")
        key = prefix_for(gen) + "meta"
        mapping = {"body": SECRET}
        self._plant(conn, gen, {key: mapping}, head_generation=generation("9"))
        out = audit(conn, gen, {key: spec(mapping)}, cid="cid-head-1")
        self.assertFalse(out["ok"], out)
        self.assertFalse(out["moved"])
        self.assertFalse(out["coverage"]["covered"])
        self.assertEqual(generation("9"), out["head_generation"])
        self.assertNotIn(SECRET, json.dumps(out))

    def test_the_correlation_id_is_echoed_on_a_mismatch(self):
        conn = AuditRedis()
        gen = generation("3")
        key = prefix_for(gen) + "meta"
        self._plant(conn, gen, {key: {"body": "stored"}})
        out = audit(conn, gen, {key: spec({"body": "other"})}, cid="cid-echo-99")
        self.assertEqual("cid-echo-99", out["correlation"])
        self.assertEqual(gen, out["generation"])
        self.assertIn("cid-echo-99", gov.summarise(out))
        self.assertEqual("changed", out["keys"][0]["status"])

    def test_a_listed_key_changed_before_exec_is_not_a_pass(self):
        conn = AuditRedis()
        gen = generation("a")
        prefix = prefix_for(gen)
        key = prefix + "meta"
        mapping = {"body": "kept"}
        self._plant(conn, gen, {key: mapping})
        real_pipeline = conn.pipeline

        def pipeline(transaction=True):
            pipe = real_pipeline(transaction=transaction)
            real_execute = pipe.execute

            def execute(raise_on_error=True):
                FakeRedis.hset(conn, key, "body", TOKEN)
                return real_execute(raise_on_error=raise_on_error)

            pipe.execute = execute
            return pipe

        conn.pipeline = pipeline
        out = audit(conn, gen, {key: spec(mapping)}, cid="cid-moved-key")
        self.assertTrue(out["moved"], out)
        self.assertFalse(out["ok"])
        self.assertFalse(out["coverage"]["covered"])
        self.assertEqual("cid-moved-key", out["correlation"])
        self.assertEqual(0, conn.scans)
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn(TOKEN, blob)

    def test_a_queued_type_error_is_not_a_pass(self):
        conn = AuditRedis()
        gen = generation("b")
        key = prefix_for(gen) + "meta"
        mapping = {"body": SECRET}
        self._plant(conn, gen, {key: mapping})
        conn.fail_next["type"] = RuntimeError("WRONGTYPE " + SECRET)
        out = audit(conn, gen, {key: spec(mapping)}, cid="cid-type-1")
        self.assertFalse(out["ok"], out)
        self.assertFalse(out["coverage"]["covered"])
        self.assertEqual("RuntimeError", out.get("error"))
        self.assertEqual("cid-type-1", out["correlation"])
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("WRONGTYPE", blob)

    def test_a_head_from_another_namespace_is_refused(self):
        conn = AuditRedis()
        gen = generation("c")
        prefix = prefix_for(gen)
        key = prefix + "meta"
        mapping = {"body": SECRET}
        self._plant(conn, gen, {key: mapping}, head_generation=generation("9"))
        put_hash(conn, "proj:fake", {"generation": gen, "witness": HEAD_SECRET})
        out = audit(conn, gen, {key: spec(mapping)}, head="proj:fake", cid="cid-fake-head")
        self.assertFalse(out["ok"], out)
        self.assertIn("head", out["refused"])
        self.assertEqual(0, conn.scans)
        self.assertEqual("cid-fake-head", out["correlation"])
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(HEAD_SECRET, blob)
        self.assertEqual(gov._bound_head(prefix, gen), "proj:pr-board:v1:head")

    def test_invalid_utf8_is_not_a_digest_match(self):
        conn = AuditRedis()
        gen = generation("d")
        prefix = prefix_for(gen)
        key = prefix + "meta"
        self._plant(conn, gen, {})
        conn.data[key] = {b"note": b"\xff"}
        replaced = {"note": b"\xff".decode("utf-8", "replace")}
        self.assertEqual("\ufffd", replaced["note"])
        with self.assertRaises(UnicodeDecodeError):
            gov.payload_digest({b"note": b"\xff"})
        out = audit(conn, gen, {key: spec(replaced)}, cid="cid-utf8-1")
        self.assertFalse(out["ok"], out)
        self.assertFalse(out["coverage"]["covered"])
        self.assertNotEqual(["match"], [entry["status"] for entry in out["keys"]])
        self.assertNotEqual(spec(replaced)["sha256"], out["keys"][0]["sha256"])
        blob = json.dumps(out) + gov.summarise(out) + json.dumps(conn.audit())
        self.assertNotIn("\ufffd", blob)
        self.assertEqual("cid-utf8-1", out["correlation"])

        valid = AuditRedis()
        gen2 = generation("e")
        key2 = prefix_for(gen2) + "meta"
        text = {"note": "caf\u00e9"}
        valid.data[key2] = {b"note": "caf\u00e9".encode("utf-8")}
        put_hash(valid, "proj:pr-board:v1:head", {"generation": gen2})
        good = audit(valid, gen2, {key2: spec(text)}, cid="cid-utf8-ok")
        self.assertTrue(good["ok"], good)
        self.assertEqual("match", good["keys"][0]["status"])
        self.assertEqual(spec(text)["sha256"], good["keys"][0]["sha256"])

    def test_unlisted_keys_are_not_scanned_and_a_match_can_pass(self):
        conn = AuditRedis()
        gen = generation("f")
        prefix = prefix_for(gen)
        one = prefix + "000"
        put_hash(conn, one, {"n": "1"})
        for n in range(1, 300):
            put_hash(conn, prefix + "%03d" % n, {"n": "1"})
        put_hash(conn, "proj:pr-board:v1:head", {"generation": gen})
        called = {"keys": 0, "scan": 0, "eval": 0}

        def forbid(name):
            def wrapped(*args, **kwargs):
                called[name] += 1
                raise AssertionError(name)
            return wrapped

        for name in ("keys", "scan", "eval"):
            setattr(conn, name, forbid(name))
        out = audit(conn, gen, {one: spec({"n": "1"})}, cid="cid-nolist-1")
        self.assertEqual({"keys": 0, "scan": 0, "eval": 0}, called)
        self.assertEqual(0, conn.scans)
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["partial"])
        self.assertEqual(1, out["listed"])
        self.assertEqual(0, out["coverage"]["extra"])
        self.assertEqual("cid-nolist-1", out["correlation"])
        self.assertNotIn(prefix + "001", json.dumps(out))

    def test_a_manifest_over_the_cap_is_refused_and_never_ok(self):
        conn = AuditRedis()
        gen = generation("0")
        prefix = prefix_for(gen)
        expected = {}
        for n in range(gov.MAX_AUDIT_KEYS + 1):
            name = prefix + "k%03d" % n
            expected[name] = {"sha256": "ab" * 32, "len": 1}
        called = {"keys": 0, "scan": 0, "eval": 0}

        def forbid(name):
            def wrapped(*args, **kwargs):
                called[name] += 1
                raise AssertionError(name)
            return wrapped

        for name in ("keys", "scan", "eval"):
            setattr(conn, name, forbid(name))
        out = audit(conn, gen, expected, cid="cid-over-cap")
        self.assertFalse(out["ok"], out)
        self.assertIn("too many", out["refused"])
        self.assertEqual({"keys": 0, "scan": 0, "eval": 0}, called)
        self.assertEqual(0, conn.scans)
        self.assertEqual("cid-over-cap", out["correlation"])
        self.assertNotIn("ab" * 32, out["refused"])


class ScopeAndManifest(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {gov.VERIFY_FLAG: "1"}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_the_verifier_keeps_the_narrow_scope_and_the_acl(self):
        gen = generation("4")
        conn = AuditRedis()
        own_prefix = "fleet:inbox:cursor:g:%s:" % gen[:16]
        own = own_prefix + "meta"
        put_hash(conn, own, {"body": "mine"})
        put_hash(conn, "fleet:inbox:cursor:head", {"generation": gen})
        out = audit(conn, gen, {own: spec({"body": "mine"})}, who="cursor",
                    prefix=own_prefix, head="fleet:inbox:cursor:head")
        self.assertTrue(out["ok"], out)
        self.assertEqual(["match"], [entry["status"] for entry in out["keys"]])

        conf_prefix = prefix_for(gen, "conf:room:g:")
        conf_key = conf_prefix + "meta"
        put_hash(conn, conf_key, {"body": "room"})
        put_hash(conn, "conf:room:head", {"generation": gen})
        room = audit(conn, gen, {conf_key: spec({"body": "room"})}, prefix=conf_prefix,
                     head="conf:room:head", cid="cid-conf-1")
        self.assertTrue(room["ok"], room)

        other = prefix_for(gen, "fleet:inbox:grok:g:") + "meta"
        put_hash(conn, other, {"body": SECRET})
        before = conn.scans
        refused = audit(conn, gen, {other: spec({"body": SECRET})}, who="cursor",
                        prefix=prefix_for(gen, "fleet:inbox:grok:g:"))
        self.assertFalse(refused["ok"])
        self.assertIn("scope", refused["refused"])
        self.assertNotIn(SECRET, json.dumps(refused))
        self.assertEqual(before, conn.scans, "a refused scope does not list keys")

        bus = audit(conn, gen, {prefix_for(gen, "v1:bus:g:") + "row": spec({"a": "1"})},
                    prefix=prefix_for(gen, "v1:bus:g:"))
        self.assertIn("scope", bus["refused"])

        sync = audit(conn, gen, {prefix_for(gen) + "meta": spec({"a": "1"})}, who="milestones-sync")
        self.assertFalse(sync["ok"])
        self.assertIn("may not read", sync["refused"])

        heart = AuditRedis()
        heart.set(gov.HEARTBEAT_READ_KEY, '{"token":"%s"}' % TOKEN)
        key = prefix_for(gen) + "meta"
        put_hash(heart, key, {"body": "ok"})
        watched = audit(heart, gen, {key: spec({"body": "ok"})}, head=gov.HEARTBEAT_READ_KEY,
                        cid="cid-heart-1")
        self.assertFalse(watched["ok"], watched)
        self.assertEqual(0, heart.gets, "a string heartbeat is not read out")
        self.assertNotIn(TOKEN, json.dumps(watched) + gov.summarise(watched))
        self.assertEqual(br.HEARTBEAT_KEY, gov.HEARTBEAT_READ_KEY)

    def test_a_manifest_carrying_a_value_is_refused_and_not_echoed(self):
        gen = generation("5")
        key = prefix_for(gen) + "meta"
        raw = json.dumps({"keys": {key: {"sha256": "a" * 64, "len": 2, "value": SECRET}}})
        conn = AuditRedis()
        out = gov.execute(conn, ACL, "cursor", "via", gov.VERIFY_OP, prefix_for(gen),
                          raw_value=base64.b64encode(raw.encode()).decode(), enc="b64",
                          generation=gen, correlation="cid-manifest-1", head="proj:pr-board:v1:head")
        self.assertFalse(out["ok"])
        self.assertIn("sha256 and len", out["refused"])
        self.assertNotIn(SECRET, json.dumps(out) + gov.summarise(out))
        self.assertEqual("cid-manifest-1", out["correlation"])

    def test_the_access_list_still_does_not_grant_sfdc24_site_or_a_new_action(self):
        data = json.loads((ROOT / "scripts" / "redis_acl.json").read_text(encoding="utf-8"))
        self.assertEqual(["Blackboard", "conference"], data["git"]["repos"])
        self.assertNotIn("sfdc24-site", data["git"]["repos"])
        self.assertEqual(("redis-synthetic-probe", "redis-op"), br.ACTIONS)


class ThroughTheWorker(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(gov.VERIFY_FLAG, None)
        self.now = __import__("datetime").datetime(2026, 10, 11, 15, 45, tzinfo=__import__("datetime").timezone.utc)

    def tearDown(self):
        self._env.stop()

    def _row(self, gen, expected, cid, req="REQ-AUDIT01"):
        prefix = prefix_for(gen)
        payload = "|".join([
            "op=haudit", "key=" + prefix, "head=proj:pr-board:v1:head",
            "gen=" + gen, "cid=" + cid, "enc=b64", "val=" + b64_manifest(expected),
        ])
        return ["ROW-" + req, NOW_TEXT, "cursor", "bus-reconciler", "AYA_REQ",
                "BCB|v=1|id=%s|req=%s|do=redis-op|%s" % (req, req, payload),
                "", "", "", ""]

    def test_flag_off_is_refused_on_the_board_and_flag_on_returns_the_digest(self):
        gen = generation("6")
        key = prefix_for(gen) + "meta"
        mapping = {"body": SECRET}
        conn = AuditRedis()
        put_hash(conn, key, mapping)
        put_hash(conn, "proj:pr-board:v1:head", {"generation": gen, "witness": HEAD_SECRET})
        expected = {key: spec(mapping)}
        rows = []
        out = br.handle([self._row(gen, expected, "cid-worker-off")], conn, now=self.now,
                        append=rows.append)
        self.assertEqual("REFUSED", out["answered"][0]["status"])
        self.assertIn("off", rows[0]["payload"])
        self.assertIn("cid-worker-off", rows[0]["payload"])
        self.assertNotIn(SECRET, rows[0]["payload"])
        self.assertEqual(0, conn.scans)

        os.environ[gov.VERIFY_FLAG] = "1"
        rows = []
        out = br.handle([self._row(gen, expected, "cid-worker-on", req="REQ-AUDIT02")], conn,
                        now=self.now, append=rows.append)
        self.assertEqual("OK", out["answered"][0]["status"])
        self.assertIn("cid-worker-on", rows[0]["payload"])
        self.assertIn(spec(mapping)["sha256"], rows[0]["payload"])
        self.assertNotIn(SECRET, rows[0]["payload"])
        self.assertNotIn(HEAD_SECRET, rows[0]["payload"])
        self.assertNotIn("|", rows[0]["payload"].split("text=", 1)[-1])

    def test_a_mismatch_is_failed_and_a_timeout_is_an_error(self):
        os.environ[gov.VERIFY_FLAG] = "1"
        gen = generation("7")
        key = prefix_for(gen) + "meta"
        conn = AuditRedis()
        put_hash(conn, key, {"body": SECRET})
        put_hash(conn, "proj:pr-board:v1:head", {"generation": gen})
        rows = []
        out = br.handle([self._row(gen, {key: spec({"body": "nope"})}, "cid-fail-1",
                                    req="REQ-AUDIT03")], conn, now=self.now, append=rows.append)
        self.assertEqual("FAILED", out["answered"][0]["status"])
        self.assertEqual([], out["errors"])
        self.assertIn("changed=1", rows[0]["payload"])
        self.assertNotIn(SECRET, rows[0]["payload"])

        conn = AuditRedis()
        put_hash(conn, key, {"body": SECRET})
        put_hash(conn, "proj:pr-board:v1:head", {"generation": gen})

        def hget(key, field):
            raise TimeoutError("nope " + TOKEN)

        conn.hget = hget
        rows = []
        out = br.handle([self._row(gen, {key: spec({"body": SECRET})}, "cid-fail-2",
                                    req="REQ-AUDIT04")], conn, now=self.now, append=rows.append)
        self.assertEqual("ERROR", out["answered"][0]["status"])
        self.assertEqual(["REQ-AUDIT04"], out["errors"])
        self.assertIn("timeout=1", rows[0]["payload"])
        self.assertNotIn(TOKEN, rows[0]["payload"])
        self.assertNotIn(SECRET, rows[0]["payload"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
