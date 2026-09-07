"""Offline CLI regressions for v31 manifest equivalence and input rejection."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Immutable v31 manifest independently read back on 2026-09-07. Keep this
# fixture separate from the working manifest, which may advance after cutover.
V31_MANIFEST = {
    'dependencies': {},
    'exceptionLogging': 'STACKDRIVER',
    'oauthScopes': [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/script.external_request',
        'https://www.googleapis.com/auth/script.scriptapp',
        'https://www.googleapis.com/auth/script.send_mail',
        'https://www.googleapis.com/auth/userinfo.email',
        'https://www.googleapis.com/auth/userinfo.profile',
    ],
    'runtimeVersion': 'V8',
    'timeZone': 'America/Toronto',
    'webapp': {'access': 'ANYONE_ANONYMOUS', 'executeAs': 'USER_DEPLOYING'},
}


class BaselineVerifier(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sfdc24-baseline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.scripts = self.repo / 'scripts'
        self.source = self.repo / 'apps-script/governor-page-api'
        self.scripts.mkdir(parents=True)
        self.source.mkdir(parents=True)
        for name in ('baseline_verify.py', 'gas_build_identity.py', 'gas_staging_target.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.scripts / name)
        self.manifest = self.source / 'appsscript.json'
        self.write_manifest(json.dumps(V31_MANIFEST))

    def write_manifest(self, text):
        self.manifest.write_bytes(text.encode('utf-8'))

    def run_checker(self, cwd=None):
        return subprocess.run(
            [sys.executable, '-B', str(self.scripts / 'baseline_verify.py')],
            cwd=cwd or self.root, capture_output=True, text=True, timeout=15,
        )

    def assert_manifest_status(self, result, status):
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line.split() for line in result.stdout.splitlines()
                 if line.startswith('appsscript ')]
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertEqual(lines[0][1], status, result.stdout)

    def test_formatting_bom_and_working_directory_do_not_change_equivalence(self):
        first = self.run_checker(self.repo)
        self.assert_manifest_status(first, 'MATCH')
        reordered = dict(reversed(list(V31_MANIFEST.items())))
        text = '\ufeff' + json.dumps(reordered, indent=4).replace('\n', '\r\n') + '\r\n'
        self.write_manifest(text)
        for cwd in (self.repo, self.scripts, self.root):
            with self.subTest(cwd=cwd.name):
                actual = self.run_checker(cwd)
                self.assert_manifest_status(actual, 'MATCH')
                self.assertEqual(actual.stdout, first.stdout)

    def test_duplicate_top_level_key_cannot_match_v31(self):
        self.write_manifest('{"runtimeVersion":"CORRUPTED",' + json.dumps(V31_MANIFEST)[1:])
        self.assert_manifest_status(self.run_checker(), 'UNPARSEABLE')

    def test_duplicate_nested_key_cannot_match_v31(self):
        text = json.dumps(V31_MANIFEST).replace(
            '"access": "ANYONE_ANONYMOUS"',
            '"access": "CORRUPTED", "access": "ANYONE_ANONYMOUS"',
        )
        self.write_manifest(text)
        self.assert_manifest_status(self.run_checker(), 'UNPARSEABLE')

    def test_nonstandard_json_numbers_are_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(value=value):
                self.write_manifest('{"invalid":' + value + ',' + json.dumps(V31_MANIFEST)[1:])
                self.assert_manifest_status(self.run_checker(), 'UNPARSEABLE')

    def test_invalid_json_is_reported_as_unparseable(self):
        self.write_manifest('{"runtimeVersion":')
        self.assert_manifest_status(self.run_checker(), 'UNPARSEABLE')

    def test_real_scope_change_is_a_mismatch(self):
        changed = {**V31_MANIFEST, 'oauthScopes': V31_MANIFEST['oauthScopes'][:-1]}
        self.write_manifest(json.dumps(changed))
        self.assert_manifest_status(self.run_checker(), 'MISMATCH')

    def test_missing_source_directory_does_not_report_a_count(self):
        self.manifest.unlink()
        self.source.rmdir()
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('baseline dir not found', result.stderr)
        self.assertNotIn('stems match', result.stdout)


if __name__ == '__main__':
    unittest.main()
