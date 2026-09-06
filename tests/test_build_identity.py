"""Offline evidence tests: immutable source, API binding and fresh HTTP identity."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
identity = importlib.import_module('gas_build_identity')


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeReader:
    def __init__(self, expected):
        self.expected = expected
        self.calls = []
        self.data = {'scriptId': expected['scriptId'], 'files': copy.deepcopy(expected['files'])}
        self.deployment_data = {
            'deploymentId': expected['deploymentId'],
            'deploymentConfig': {'scriptId': expected['scriptId'], 'versionNumber': 12, 'manifestFileName': 'appsscript'},
            'entryPoints': [{'entryPointType': 'WEB_APP', 'webApp': {
                'url': expected['execUrl'],
                'entryPointConfig': {'access': 'ANYONE_ANONYMOUS', 'executeAs': 'USER_DEPLOYING'},
            }}],
        }
        self.health_reply = None

    def content(self, expected, version):
        self.calls.append(('content', version))
        return self.data

    def deployment(self, expected):
        self.calls.append(('deployment', None))
        return self.deployment_data

    def health(self, expected, nonce):
        self.calls.append(('health', nonce))
        if self.health_reply is not None:
            return self.health_reply
        return {'ok': True, **expected['identity'], 'nonce': nonce}


class BuildProof(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='build-proof-')
        cls.repo = Path(cls.temp.name)
        cls.source = cls.repo / 'apps-script/governor-page-api'
        cls.source.mkdir(parents=True)
        (cls.source / 'Code.gs').write_text("function doGet(e) { var p=e.parameter; if (p.health === 'build') return sfdc24BuildIdentity_(); }\n", encoding='utf-8')
        (cls.source / 'appsscript.json').write_text('{"timeZone":"UTC","runtimeVersion":"V8"}\n', encoding='utf-8')
        (cls.source / 'nested').mkdir()
        (cls.source / 'nested/Reception.html').write_text('<p>committed template</p>\n', encoding='utf-8')
        git(cls.repo, 'init', '-q')
        git(cls.repo, 'config', 'user.email', 'fixture@example.invalid')
        git(cls.repo, 'config', 'user.name', 'Offline fixture')
        git(cls.repo, 'add', '.')
        git(cls.repo, 'commit', '-qm', 'build health fixture')
        cls.commit = git(cls.repo, 'rev-parse', 'HEAD')
        cls.expected = identity.build('governor-page-api', cls.commit, cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.reader = FakeReader(self.expected)
        self.guard = mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_prepared_files_come_from_commit_not_working_tree(self):
        file = self.source / 'Code.gs'
        saved = file.read_text(encoding='utf-8')
        file.write_text('uncommitted replacement', encoding='utf-8')
        untracked = self.source / 'Untracked.gs'
        untracked.write_text('must not deploy', encoding='utf-8')
        try:
            actual = identity.build('governor-page-api', self.commit, self.repo)
            self.assertEqual(actual, self.expected)
            with tempfile.TemporaryDirectory() as parent:
                output = identity.prepare(actual, parent)
                self.assertEqual((output / 'Code.gs').read_text(encoding='utf-8'), saved)
                self.assertFalse((output / 'Untracked.gs').exists())
                self.assertTrue((output / 'BuildIdentity.gs').exists())
                self.assertEqual(json.loads((output / '.clasp.json').read_text())['scriptId'], actual['scriptId'])
        finally:
            file.write_text(saved, encoding='utf-8')
            untracked.unlink()

    def test_full_commit_and_supported_project_required(self):
        for project, commit in [('blackboard-production', self.commit), ('../governor-page-api', self.commit),
                                ('governor-page-api', 'HEAD'), ('governor-page-api', self.commit[:8]),
                                ('governor-page-api', '$(touch forbidden)')]:
            with self.subTest(project=project, commit=commit), self.assertRaises(ValueError):
                identity.build(project, commit, self.repo)

    def test_pinned_source_requires_version_and_exact_project(self):
        self.assertEqual(identity.verify_source(self.expected, '12', self.reader), '12')
        self.assertEqual(self.reader.calls, [('content', '12')])
        self.reader.data['scriptId'] = 'other-project'
        with self.assertRaises(ValueError):
            identity.verify_source(self.expected, '12', self.reader)
        for version in ('HEAD', '0', True, None):
            with self.assertRaises(ValueError):
                identity.verify_source(self.expected, version, self.reader)

    def test_source_equality_tolerates_only_declared_normalization(self):
        files = self.reader.data['files']
        files.reverse()
        for file in files:
            if file['type'] == 'JSON':
                file['source'] = json.dumps(json.loads(file['source']), indent=4)
            else:
                file['source'] = file['source'].replace('\n', '\r\n')
            file['lastModifyUser'] = {'name': 'irrelevant API metadata'}
        identity.verify_source(self.expected, '12', self.reader)

    def test_changed_missing_extra_or_duplicate_source_is_rejected(self):
        original = copy.deepcopy(self.expected['files'])
        cases = []
        changed = copy.deepcopy(original)
        changed[0]['source'] += '// stale or altered\n'
        cases.extend([changed, original[:-1], original + [original[0]],
                      original + [{'name': 'Unexpected', 'type': 'SERVER_JS', 'source': 'var extra=true;'}]])
        for files in cases:
            self.reader.data['files'] = files
            with self.subTest(files=len(files)), self.assertRaises(ValueError):
                identity.verify_source(self.expected, '12', self.reader)

    def test_marker_alone_cannot_certify_changed_application_source(self):
        code = next(f for f in self.reader.data['files'] if f['name'] == 'Code')
        code['source'] = 'function doGet() { return "wrong build"; }'
        with self.assertRaises(ValueError):
            identity.attest(self.expected, '12', self.reader, retries=1, delay=0)
        self.assertFalse(any(call[0] == 'health' for call in self.reader.calls))

    def test_manifest_or_generated_marker_change_fails(self):
        for name in ('appsscript', 'BuildIdentity'):
            self.reader.data = {'scriptId': self.expected['scriptId'], 'files': copy.deepcopy(self.expected['files'])}
            file = next(f for f in self.reader.data['files'] if f['name'] == name)
            file['source'] = '{"runtimeVersion":"wrong"}' if name == 'appsscript' else 'function sfdc24BuildIdentity_(){return {};}'
            with self.subTest(name=name), self.assertRaises(ValueError):
                identity.verify_source(self.expected, '12', self.reader)

    def test_invalid_remote_file_inventory_is_rejected(self):
        for files in (None, [], {}, [None], [{'name': '../escape', 'type': 'SERVER_JS', 'source': 'x'}],
                      [{'name': 'Code', 'type': 'UNKNOWN', 'source': 'x'}]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                identity.canonical_files(files)
        for data in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(ValueError):
                identity.parse_json(data)

    def test_wrong_version_target_url_or_access_stops_before_health(self):
        original = copy.deepcopy(self.reader.deployment_data)
        mutations = [
            lambda d: d.update(deploymentId='wrong'),
            lambda d: d['deploymentConfig'].update(scriptId='wrong'),
            lambda d: d['deploymentConfig'].update(versionNumber=True),
            lambda d: d['deploymentConfig'].update(versionNumber=1),
            lambda d: d['deploymentConfig'].update(manifestFileName='other'),
            lambda d: d['entryPoints'][0]['webApp'].update(url='https://example.invalid/exec'),
            lambda d: d['entryPoints'][0]['webApp']['entryPointConfig'].update(access='MYSELF'),
            lambda d: d.update(entryPoints=[]),
        ]
        for mutate in mutations:
            data = copy.deepcopy(original)
            mutate(data)
            self.reader.deployment_data = data
            self.reader.calls = []
            with self.assertRaises(ValueError):
                identity.attest(self.expected, '12', self.reader, retries=1, delay=0)
            self.assertEqual(self.reader.calls, [('deployment', None)])

    def test_only_matching_live_build_with_fresh_nonce_gets_receipt(self):
        receipt = identity.attest(self.expected, '12', self.reader, retries=1, delay=0)
        self.assertEqual(receipt['commit'], self.commit)
        self.assertEqual(receipt['versionNumber'], 12)
        self.assertEqual(receipt['fileSetSha256'], self.expected['fileSetSha256'])
        self.assertEqual([call[0] for call in self.reader.calls], ['deployment', 'content', 'health', 'deployment'])
        self.assertNotIn('files', receipt)
        self.assertNotIn('token', receipt)

    def test_wrong_health_schema_build_and_nonce_never_get_receipt(self):
        for actual in ('<html>Google sign in</html>', {'ok': True, 'version': 12},
                       {'ok': True, **self.expected['identity'], 'nonce': 'stale'},
                       {'ok': True, **self.expected['identity'], 'nonce': 'stale', 'error': 'failed'}):
            self.reader.health_reply = actual
            with self.assertRaises(ValueError):
                identity.attest(self.expected, '12', self.reader, retries=1, delay=0)

    def test_deployment_changed_during_health_read_cannot_get_receipt(self):
        original = self.reader.health
        def health(expected, nonce):
            value = original(expected, nonce)
            self.reader.deployment_data['deploymentConfig']['versionNumber'] = 13
            return value
        self.reader.health = health
        with self.assertRaises(ValueError):
            identity.attest(self.expected, '12', self.reader, retries=1, delay=0)

    def test_health_retries_are_bounded_and_use_fresh_nonces(self):
        self.reader.health_reply = {'ok': False}
        with mock.patch.object(identity.time, 'sleep') as sleep, self.assertRaises(ValueError):
            identity.attest(self.expected, '12', self.reader, retries=3, delay=1)
        nonces = [call[1] for call in self.reader.calls if call[0] == 'health']
        self.assertEqual(len(nonces), 3)
        self.assertEqual(len(set(nonces)), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_content_api_requests_explicit_version_and_never_prints_token(self):
        reader = identity.Reader.__new__(identity.Reader)
        reader.token = 'private-token'
        with mock.patch.object(identity, 'read_json', return_value=self.reader.data) as read:
            reader.content(self.expected, '12')
        request = read.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/content?versionNumber=12'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-token')

    def test_health_request_is_anonymous_and_redirect_is_restricted(self):
        reader = identity.Reader.__new__(identity.Reader)
        reader.token = 'private-token'
        with mock.patch.object(identity, 'read_json', return_value={}) as read:
            reader.health(self.expected, 'a' * 32)
        request = read.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))
        redirect = identity.HealthRedirect()
        for url in ('https://accounts.google.com/signin', 'http://script.googleusercontent.com/x',
                    'https://script.googleusercontent.com.evil.invalid/x', 'https://user@script.googleusercontent.com/x'):
            with self.assertRaises(ValueError):
                redirect.redirect_request(request, None, 302, '', {}, url)
        self.assertIsNotNone(redirect.redirect_request(request, None, 302, '', {}, 'https://script.googleusercontent.com/macros/echo'))


if __name__ == '__main__':
    unittest.main()
