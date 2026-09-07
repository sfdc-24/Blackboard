"""Offline contract tests for the isolated SITE-P0 TTS cardinality canary."""
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
canary = importlib.import_module('gas_tts_canary')

COMMIT = 'a' * 40
SOURCE_SHA = 'b' * 64
RUN_ID = '0123456789abcdef01234567'


class CanaryBuildContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = (ROOT / canary.SOURCE_PATH).read_text(encoding='utf-8')
        cls.template = (ROOT / canary.TEMPLATE_PATH).read_text(encoding='utf-8')
        cls.bundle = canary.render_bundle(cls.code, cls.template, COMMIT, SOURCE_SHA, RUN_ID)
        cls.source = next(item['source'] for item in cls.bundle['files'] if item['name'] == 'Code')
        cls.manifest = json.loads(next(item['source'] for item in cls.bundle['files']
                                       if item['name'] == 'appsscript'))

    def test_critical_claim_and_budget_functions_are_byte_exact(self):
        for name in canary.CRITICAL_FUNCTIONS:
            self.assertEqual(canary.extract_function(self.source, name),
                             canary.extract_function(self.code, name))
            self.assertEqual(self.bundle['criticalFunctions'][name],
                             canary.sha256_text(canary.extract_function(self.code, name)))

    def test_only_paid_network_boundary_is_substituted(self):
        canonical = canary.extract_function(self.code, 'ttsAudio_')
        prepared = canary.extract_function(self.source, 'ttsAudio_')
        self.assertEqual(canonical.count('UrlFetchApp.fetch('), 1)
        self.assertEqual(prepared, canonical.replace(
            'UrlFetchApp.fetch(', 'siteP0CanaryProviderFetch_(', 1))
        self.assertNotIn('UrlFetchApp', self.source)
        self.assertEqual(self.source.count('siteP0CanaryProviderFetch_('), 2)

    def test_minimal_bundle_has_no_live_entrypoint_or_ambient_service(self):
        for forbidden in ('function doGet', 'function doPost', 'SpreadsheetApp', 'DriveApp',
                          'MailApp', 'ANTHROPIC_KEY', 'GOVERNOR_PASS', 'ALPHA_ID'):
            self.assertNotIn(forbidden, self.source)
        self.assertIn("var SITE_P0_TEXT_ = 'Synthetic cardinality probe.';", self.source)
        self.assertNotRegex(self.source, r'function siteP0\w+\([^)]*text')

    def test_private_api_manifest_has_no_webapp_or_network_scope(self):
        self.assertEqual(self.manifest['executionApi'], {'access': 'MYSELF'})
        self.assertNotIn('webapp', self.manifest)
        self.assertNotIn('https://www.googleapis.com/auth/script.external_request',
                         self.manifest['oauthScopes'])
        self.assertEqual(self.manifest['oauthScopes'],
                         ['https://www.googleapis.com/auth/userinfo.email'])

    def test_preflight_reads_names_only_and_cleanup_tracks_cache(self):
        preflight = canary.extract_function(self.source, 'siteP0CanaryPreflight')
        cleanup = canary.extract_function(self.source, 'siteP0CanaryCleanup')
        self.assertIn('getKeys()', preflight)
        self.assertNotIn('getProperties()', preflight)
        self.assertIn("cache.remove('tts_' + tracked[j])", cleanup)
        self.assertIn("cache.get('tts_' + tracked[n])", cleanup)
        self.assertIn('trackedCacheEntriesRemaining', cleanup)

    def test_caps_are_seeded_to_minus_one_without_mutating_cap_properties(self):
        prepare = canary.extract_function(self.source, 'siteP0CanaryPrepare')
        self.assertIn('sessionCap - 1', prepare)
        self.assertIn('dailyCap - 1', prepare)
        self.assertNotIn("setProperty('TTS_SESSION_CAP'", prepare)
        self.assertNotIn("setProperty('TTS_DAILY_CAP'", prepare)
        self.assertLess(prepare.index("siteP0MintSlot_('two'"), prepare.index('return {\n    ok: true'))

    def test_server_side_slots_keep_audio_keys_out_of_control_results(self):
        setup = canary.extract_function(self.source, 'siteP0MintSlot_')
        prepare = canary.extract_function(self.source, 'siteP0CanaryPrepare')
        self.assertIn('SITE_P0_SLOT_PREFIX_', setup)
        self.assertNotIn('key:', prepare)
        self.assertNotIn('ak:', prepare)

    def test_changed_provider_boundary_or_extra_top_level_function_is_rejected(self):
        with self.assertRaises(ValueError):
            canary.render_bundle(self.code.replace('UrlFetchApp.fetch(', 'otherFetch(', 1),
                                 self.template, COMMIT, SOURCE_SHA, RUN_ID)
        with self.assertRaises(ValueError):
            canary.render_bundle(self.code, self.template + '\nfunction surprise() {}\n',
                                 COMMIT, SOURCE_SHA, RUN_ID)

    def test_invalid_identity_inputs_are_rejected(self):
        for commit, source_hash, run_id in [
                ('HEAD', SOURCE_SHA, RUN_ID), (COMMIT, 'short', RUN_ID),
                (COMMIT, SOURCE_SHA, '../escape')]:
            with self.subTest(commit=commit, run_id=run_id), self.assertRaises(ValueError):
                canary.render_bundle(self.code, self.template, commit, source_hash, run_id)

    def test_prepare_contains_only_generated_source_and_local_binding(self):
        with tempfile.TemporaryDirectory() as parent:
            output = canary.prepare(self.bundle, 'A' * 24, parent)
            self.assertEqual({path.name for path in output.iterdir()},
                             {'Code.gs', 'appsscript.json', '.clasp.json'})
            binding = json.loads((output / '.clasp.json').read_text(encoding='utf-8'))
            self.assertEqual(binding, {'rootDir': '.', 'scriptId': 'A' * 24})
            check_path = Path(parent) / 'generated-canary.js'
            check_path.write_text((output / 'Code.gs').read_text(encoding='utf-8'),
                                  encoding='utf-8', newline='\n')
            subprocess.run(['node', '--check', str(check_path)], check=True,
                           capture_output=True, text=True)


class CanaryTargetContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tts-canary-target-')
        self.repo = Path(self.temp.name)
        (self.repo / 'scripts').mkdir()
        (self.repo / '.clasp.json').write_text(json.dumps({'scriptId': 'P' * 24}), encoding='utf-8')
        (self.repo / 'scripts/gas_staging_targets.json').write_text(json.dumps({
            'governor-page-api': {'script_id': 'S' * 24, 'deploy_id': 'D' * 24},
        }), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_checked_in_target_is_deliberately_unprovisioned(self):
        inventory = json.loads(canary.TARGETS.read_text(encoding='utf-8'))
        self.assertEqual(inventory, {'schema': 1, 'status': 'UNPROVISIONED', 'script_id': None})
        with self.assertRaises(ValueError):
            canary.resolve_target(self.repo, {}, inventory)

    def test_ready_target_must_match_variable_and_not_overlap(self):
        good = 'C' * 24
        inventory = {'schema': 1, 'status': 'READY', 'script_id': good}
        self.assertEqual(canary.resolve_target(
            self.repo, {'SITE_P0_TTS_CANARY_SCRIPT_ID': good}, inventory), {'script_id': good})
        for value in ('P' * 24, 'S' * 24):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canary.resolve_target(
                    self.repo, {'SITE_P0_TTS_CANARY_SCRIPT_ID': value},
                    {'schema': 1, 'status': 'READY', 'script_id': value})
        with self.assertRaises(ValueError):
            canary.resolve_target(self.repo, {'SITE_P0_TTS_CANARY_SCRIPT_ID': 'X' * 24}, inventory)

    def test_every_documented_non_canary_script_id_is_denied(self):
        for value in canary.KNOWN_NON_CANARY_SCRIPT_IDS:
            with self.subTest(value=value), self.assertRaises(ValueError):
                canary.resolve_target(
                    self.repo, {'SITE_P0_TTS_CANARY_SCRIPT_ID': value},
                    {'schema': 1, 'status': 'READY', 'script_id': value})


class CanaryReceiptContract(unittest.TestCase):
    def setUp(self):
        self.receipt = {
            'schema': 'site-p0-tts-canary.v1',
            'repository': 'sfdc-24/Blackboard',
            'commit': 'c' * 40,
            'canonicalCodeSha256': '1' * 64,
            'canonicalSourceSha256': '2' * 64,
            'criticalFunctionSha256': '3' * 64,
            'preparedSourceSha256': '4' * 64,
            'canaryScriptId': 'C' * 24,
            'canaryVersion': 1,
            'apiDeploymentId': 'D' * 24,
            'providerKind': 'credential-free synthetic admission counter',
            'effectiveCaps': {'session': 8, 'daily': 60},
            'scenarios': {
                'valid': {'accepted': True, 'attemptDelta': 1},
                'replay': {'reason': 'expired', 'attemptDelta': 0},
                'concurrency': {'readyCount': 2, 'winnerCount': 1,
                                'loserReason': 'expired', 'attempts': 1,
                                'loserObservedWhileProviderInFlight': True},
                'sessionCap': {'acceptedDelta': 1, 'rejectedReason': 'tts-session-cap',
                               'rejectedDelta': 0},
                'dailyCap': {'acceptedDelta': 1, 'rejectedReason': 'tts-daily-cap',
                             'rejectedDelta': 0},
            },
            'privacy': {'providerCredentialsLoaded': False, 'freeformTextAccepted': False,
                        'rawResponsesPreserved': False, 'audioKeysPreserved': False},
            'cleanup': {'propertiesRemaining': 0, 'trackedCacheEntriesRemaining': 0,
                        'temporaryDeploymentPresent': False, 'headRestored': True},
            'verdict': 'PASS',
        }

    def test_only_exact_pass_receipt_is_accepted(self):
        self.assertIs(canary.validate_receipt(self.receipt, 'C' * 24), self.receipt)
        mutations = [
            lambda r: r.update(verdict='HOLD'),
            lambda r: r['scenarios']['replay'].update(attemptDelta=1),
            lambda r: r['scenarios']['concurrency'].update(
                loserObservedWhileProviderInFlight=False),
            lambda r: r['privacy'].update(providerCredentialsLoaded=True),
            lambda r: r['cleanup'].update(temporaryDeploymentPresent=True),
            lambda r: r.update(extra='not-allowlisted'),
        ]
        for mutate in mutations:
            value = copy.deepcopy(self.receipt)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                canary.validate_receipt(value, 'C' * 24)

        with self.assertRaises(ValueError):
            canary.validate_receipt(self.receipt, 'X' * 24)

    def test_receipt_rejects_keys_text_and_authorization_material(self):
        for field, value in [('repository', 'Bearer private'),
                             ('providerKind', 'Synthetic cardinality probe'),
                             ('apiDeploymentId', 'ak0123456789abcdef')]:
            changed = copy.deepcopy(self.receipt)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                canary.validate_receipt(changed, 'C' * 24)

    def test_cli_failure_is_generic_and_does_not_echo_environment(self):
        with mock.patch.object(sys, 'argv', ['gas_tts_canary.py', 'prepare', '--commit', 'HEAD',
                                             '--run-id', RUN_ID]), \
                mock.patch.dict(os.environ, {'SITE_P0_TTS_CANARY_SCRIPT_ID': 'private-value'}), \
                mock.patch('builtins.print') as output:
            self.assertEqual(canary.main(), 1)
        rendered = ' '.join(str(call) for call in output.call_args_list)
        self.assertNotIn('private-value', rendered)


if __name__ == '__main__':
    unittest.main()
