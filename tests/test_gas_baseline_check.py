"""
Does the baseline checker still catch what matters after JSON stopped being
compared byte-for-byte?

WHY THIS FILE EXISTS
  scripts/gas_baseline_check.py compares the committed Apps Script mirror
  against deployed source by content hash. Measured 2026-09-13, it reported
  DRIFT on appsscript.json forever: the committed and deployed manifests held
  the same six keys with the same values in a different ORDER, and Apps Script
  reads the parsed manifest rather than the bytes. That is a false positive no
  reconciliation could ever clear.

  The fix canonicalises JSON before hashing. The risk the fix introduces is the
  opposite failure - normalising so much that a REAL manifest change stops being
  drift. A changed oauthScope or access level is exactly the thing this checker
  exists to refuse to deploy over, so most of the assertions below are about
  what must STILL be caught.
"""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "gas_baseline_check",
    Path(__file__).resolve().parents[1] / "scripts" / "gas_baseline_check.py",
)
gbc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gbc)


class DigestJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    # ---- the false positive this change removes ----------------------------
    def test_json_key_order_is_not_drift(self):
        a = self.write("a.json", '{"timeZone":"America/Toronto","runtimeVersion":"V8"}')
        b = self.write("b.json", '{"runtimeVersion":"V8","timeZone":"America/Toronto"}')
        self.assertEqual(gbc.digest(a), gbc.digest(b))

    def test_nested_json_key_order_is_not_drift(self):
        a = self.write("a.json", '{"webapp":{"executeAs":"USER_DEPLOYING","access":"ANYONE_ANONYMOUS"}}')
        b = self.write("b.json", '{"webapp":{"access":"ANYONE_ANONYMOUS","executeAs":"USER_DEPLOYING"}}')
        self.assertEqual(gbc.digest(a), gbc.digest(b))

    def test_json_indentation_is_not_drift(self):
        a = self.write("a.json", '{"a":1,"b":2}')
        b = self.write("b.json", '{\n    "a": 1,\n    "b": 2\n}\n')
        self.assertEqual(gbc.digest(a), gbc.digest(b))

    def test_a_utf8_bom_is_not_drift(self):
        a = self.write("a.json", '{"a":1}')
        b = self.dir / "b.json"
        b.write_bytes(b"\xef\xbb\xbf" + b'{"a":1}')
        self.assertEqual(gbc.digest(a), gbc.digest(b))

    # ---- everything that must STILL be caught -------------------------------
    def test_a_changed_value_is_still_drift(self):
        a = self.write("a.json", '{"timeZone":"America/Toronto"}')
        b = self.write("b.json", '{"timeZone":"Etc/UTC"}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_a_changed_access_level_is_still_drift(self):
        a = self.write("a.json", '{"webapp":{"access":"ANYONE_ANONYMOUS"}}')
        b = self.write("b.json", '{"webapp":{"access":"MYSELF"}}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_an_added_oauth_scope_is_still_drift(self):
        a = self.write("a.json", '{"oauthScopes":["a"]}')
        b = self.write("b.json", '{"oauthScopes":["a","b"]}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_reordered_ARRAY_elements_are_still_drift(self):
        # Object keys are unordered; JSON arrays are NOT. Sorting keys must not
        # have quietly sorted list contents too - oauthScopes order is data.
        a = self.write("a.json", '{"oauthScopes":["a","b"]}')
        b = self.write("b.json", '{"oauthScopes":["b","a"]}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_a_removed_key_is_still_drift(self):
        a = self.write("a.json", '{"a":1,"b":2}')
        b = self.write("b.json", '{"a":1}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_types_are_not_conflated(self):
        a = self.write("a.json", '{"a":1}')
        b = self.write("b.json", '{"a":"1"}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    # ---- the normalisation must not leak out of .json -----------------------
    def test_source_files_are_still_compared_byte_for_byte(self):
        # A .gs differing only in whitespace MUST still read as drift: the
        # preamble's promise is that a mirror which is "nearly" the deployed
        # source is not evidence of anything.
        a = self.write("a.gs", 'var X = 1;')
        b = self.write("b.gs", 'var  X  =  1;')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_a_gs_file_holding_json_text_is_not_canonicalised(self):
        a = self.write("a.gs", '{"b":2,"a":1}')
        b = self.write("b.gs", '{"a":1,"b":2}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_html_is_still_compared_byte_for_byte(self):
        a = self.write("a.html", "<p>x</p>")
        b = self.write("b.html", "<p>x</p>\n")
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    # ---- malformed input must not become "equal to anything" ----------------
    def test_malformed_json_falls_back_to_bytes_and_differs(self):
        a = self.write("a.json", '{"a":1')
        b = self.write("b.json", '{"a":2')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    def test_malformed_json_matching_itself_is_equal(self):
        a = self.write("a.json", "{not json at all")
        b = self.write("b.json", "{not json at all")
        self.assertEqual(gbc.digest(a), gbc.digest(b))

    def test_malformed_json_is_never_equal_to_valid_json(self):
        a = self.write("a.json", "{oops")
        b = self.write("b.json", '{"a":1}')
        self.assertNotEqual(gbc.digest(a), gbc.digest(b))

    # ---- CRLF normalisation, which predates this change, still holds --------
    def test_crlf_in_source_is_not_drift(self):
        a = self.dir / "a.gs"
        a.write_bytes(b"var X = 1;\r\nvar Y = 2;\r\n")
        b = self.dir / "b.gs"
        b.write_bytes(b"var X = 1;\nvar Y = 2;\n")
        self.assertEqual(gbc.digest(a), gbc.digest(b))

    def test_digest_is_a_sha256_hex_string(self):
        a = self.write("a.json", '{"a":1}')
        d = gbc.digest(a)
        self.assertEqual(len(d), 64)
        int(d, 16)  # raises if it is not hex
        self.assertEqual(
            d,
            hashlib.sha256(
                json.dumps({"a": 1}, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
