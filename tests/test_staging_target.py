"""Exercise staging validation and the real workflow blocks with fake clasp."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/gas_staging_target.py'
spec = importlib.util.spec_from_file_location('staging_target', SCRIPT)
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)
INVENTORY = json.loads(target.TARGETS.read_text())
PROJECT = 'governor-page-api'
REVIEWED = INVENTORY[PROJECT]
KEY = PROJECT.upper().replace('-', '_')
VARIABLES = {
    f'STAGING_SCRIPT_ID_{KEY}': REVIEWED['script_id'],
    f'STAGING_DEPLOYMENT_ID_{KEY}': REVIEWED['deploy_id'],
    f'STAGING_EXEC_URL_{KEY}': f"https://script.google.com/macros/s/{REVIEWED['deploy_id']}/exec",
}


class TargetValidation(unittest.TestCase):
    def test_reviewed_inventory_accepts_each_project(self):
        for project, entry in INVENTORY.items():
            key = project.upper().replace('-', '_')
            variables = {
                f'STAGING_SCRIPT_ID_{key}': entry['script_id'],
                f'STAGING_DEPLOYMENT_ID_{key}': entry['deploy_id'],
                f'STAGING_EXEC_URL_{key}': f"https://script.google.com/macros/s/{entry['deploy_id']}/exec",
            }
            self.assertEqual(target.resolve(project, variables, INVENTORY)['script_id'], entry['script_id'])

    def test_wrong_project_or_missing_variables_fail(self):
        for project, variables in [('unknown', VARIABLES), (PROJECT, {}), (PROJECT, []), (PROJECT, None)]:
            with self.subTest(project=project, variables=variables), self.assertRaises(ValueError):
                target.resolve(project, variables, INVENTORY)

    def test_unreviewed_cross_project_and_injected_ids_fail(self):
        for field in ('SCRIPT_ID', 'DEPLOYMENT_ID'):
            for value in ('unreviewed-production-id', INVENTORY['blackboard-production'][
                    'script_id' if field == 'SCRIPT_ID' else 'deploy_id'], 'abc\ninjected=yes',
                    '$(touch forbidden)', None, 123, '', 'abc/def'):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    target.resolve(PROJECT, {**VARIABLES, f'STAGING_{field}_{KEY}': value}, INVENTORY)

    def test_url_must_be_exact_without_dev_path_query_or_credentials(self):
        url = VARIABLES[f'STAGING_EXEC_URL_{KEY}']
        for value in (url + '?token=private', url + '#fragment', url.replace('/exec', '/dev'),
                      url.replace('https:', 'http:'), url.replace('script.google.com', 'script.google.com.evil.invalid'),
                      url.replace('script.google.com', 'user@script.google.com'), url + '/', ' ' + url,
                      url.replace(REVIEWED['deploy_id'], 'different-id'), ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                target.resolve(PROJECT, {**VARIABLES, f'STAGING_EXEC_URL_{KEY}': value}, INVENTORY)

    def test_version_rejects_shell_syntax_head_and_noncanonical_numbers(self):
        for value in (True, False, 0, -1, 1.0, None, '', '01', '+1', ' 1', '1\n', '1.0', 'HEAD',
                      '１', '1; touch forbidden', '$(touch forbidden)', '1";touch forbidden;#'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                target.positive_version(value)
        self.assertEqual(target.positive_version('123'), '123')
        self.assertEqual(target.positive_version(123), '123')

    def test_deployment_matches_structured_id_and_version(self):
        rows = [{'deploymentId': 'expected', 'versionNumber': 12},
                {'deploymentId': 'unrelated', 'versionNumber': 42}]
        self.assertEqual(target.deployment_version(rows, 'expected', '12'), '12')

    def test_deployment_description_cannot_spoof_id_or_version(self):
        cases = [
            [{'deploymentId': 'prefix-expected', 'versionNumber': 12}],
            [{'deploymentId': 'other', 'versionNumber': 12, 'description': 'expected @12'}],
            [{'deploymentId': 'expected', 'versionNumber': 1, 'description': '@12'}],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                target.deployment_version(rows, 'expected', '12')

    def test_missing_duplicate_head_and_invalid_inventories_fail(self):
        row = {'deploymentId': 'expected', 'versionNumber': 12}
        for data in ([], {}, None, 'expected @12', [row, row], [None],
                     [{'deploymentId': 'expected'}],
                     *[[{**row, 'versionNumber': value}] for value in (True, '12', 0, -1, None)]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                target.deployment_version(data, 'expected')

    def test_created_version_requires_pinned_json_contract(self):
        self.assertEqual(target.created_version({'versionNumber': 12}), '12')
        for data in ([], None, {}, {'versionNumber': True}, {'versionNumber': '12'},
                     {'versionNumber': 0}, {'versionNumber': 12, 'error': 'failed'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                target.created_version(data)

    def test_cli_failure_does_not_echo_configuration(self):
        with mock.patch.dict(os.environ, {'PROJECT': PROJECT, 'VARS_JSON': 'private-secret'}), \
                mock.patch.object(sys, 'argv', ['target', 'resolve']), \
                contextlib.redirect_stderr(io.StringIO()) as error, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(target.main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('private-secret', error.getvalue())


def workflow_blocks(name):
    workflow = (ROOT / '.github/workflows' / name).read_text()
    steps = re.split(r'\n      - ', workflow)[1:]
    blocks = {}
    for step in steps:
        if not step.startswith('name: '):
            continue
        title = step.splitlines()[0][6:].strip("'\"")
        match = re.search(r'\n        run: (.*)', step)
        if not match:
            continue
        if match[1] == '|':
            body = textwrap.dedent(step[match.end() + 1:])
        else:
            body = match[1] + '\n'
        blocks[title] = body
    return blocks


class WorkflowSupplyChain(unittest.TestCase):
    def test_secret_bearing_workflows_pin_actions_and_drop_checkout_credentials(self):
        expected = (
            'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683',
            'actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020',
            'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02',
        )
        for filename in ('staging-deploy.yml', 'staging-rollback.yml'):
            workflow = (ROOT / '.github/workflows' / filename).read_text()
            with self.subTest(filename=filename):
                for action in expected:
                    self.assertIn(action, workflow)
                self.assertNotRegex(
                    workflow,
                    r'uses:\s+actions/(?:checkout|setup-node|upload-artifact)@v\d',
                )
                self.assertIn('persist-credentials: false', workflow)


class WorkflowGates(unittest.TestCase):
    def setUp(self):
        self.bash = os.environ.get('TEST_BASH') or shutil.which('bash')
        self.assertTrue(self.bash, 'Bash required (set TEST_BASH on Windows)')
        self.temp = tempfile.TemporaryDirectory(prefix='staging-gates-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        shutil.copyfile(SCRIPT, self.root / 'scripts/gas_staging_target.py')
        shutil.copyfile(target.TARGETS, self.root / 'scripts/gas_staging_targets.json')
        (self.root / 'bin').mkdir()
        fake = self.root / 'bin/clasp'
        fake.write_text('''#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PWD/calls.txt"
case "$1 $2" in
  '--json list-deployments') printf '%s' "$TEST_DEPLOYMENTS"; exit "${TEST_CLASP_STATUS:-0}" ;;
  '--json create-version') printf '%s' "$TEST_CREATED" ;;
  *) printf 'mutation\\n' >> "$PWD/mutations.txt" ;;
esac
''', newline='\n')
        fake.chmod(0o755)
        # Git Bash on Windows may resolve python3 to the Store alias.
        shim = self.root / 'bin/python3'
        shim.write_text('#!/usr/bin/env bash\nexec "$TEST_PYTHON" "$@"\n', newline='\n')
        shim.chmod(0o755)
        self.env = {**os.environ, 'TEST_PYTHON': sys.executable.replace('\\', '/'),
                    'PROJECT': PROJECT, 'VARS_JSON': json.dumps(VARIABLES),
                    'SCRIPT_ID': REVIEWED['script_id'], 'DEPLOY_ID': REVIEWED['deploy_id'],
                    'REQUESTED_VERSION': '12', 'COMMIT_SHA': 'a' * 40,
                    'TEST_DEPLOYMENTS': json.dumps([{'deploymentId': REVIEWED['deploy_id'], 'versionNumber': 1}]),
                    'TEST_CREATED': '{"versionNumber":12}', 'TEST_CLASP_STATUS': '0'}

    def execute(self, body, **env):
        prefix = '''set -euo pipefail
export PATH="$PWD/bin:$PATH"
export GITHUB_WORKSPACE="$PWD"
export GITHUB_OUTPUT="$PWD/outputs.txt"
'''
        driver = self.root / 'driver.sh'
        driver.write_text(prefix + body, newline='\n')
        for name in ('mutations.txt', 'outputs.txt', 'calls.txt', 'forbidden'):
            path = self.root / name
            if path.exists():
                path.unlink()
        return subprocess.run([self.bash, 'driver.sh'], cwd=self.root,
                              env={**self.env, **env}, capture_output=True, text=True)

    def test_wrong_target_stops_before_any_clasp_call(self):
        for filename in ('staging-deploy.yml', 'staging-rollback.yml'):
            blocks = workflow_blocks(filename)
            resolve = next(body for name, body in blocks.items() if name.startswith('Resolve reviewed'))
            preflight = blocks['Verify deployment belongs to the staging project before mutation']
            invalid = {**VARIABLES, f'STAGING_SCRIPT_ID_{KEY}': 'unreviewed'}
            result = self.execute(resolve + '\n' + preflight + '\nclasp push -f\n', VARS_JSON=json.dumps(invalid))
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((self.root / 'calls.txt').exists())

    def test_rollback_shell_input_stops_without_executing_it(self):
        blocks = workflow_blocks('staging-rollback.yml')
        body = next(body for name, body in blocks.items() if name.startswith('Resolve reviewed'))
        result = self.execute(body + '\nclasp redeploy target\n', REQUESTED_VERSION='1"; touch forbidden; #')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'forbidden').exists())
        self.assertFalse((self.root / 'calls.txt').exists())

    def test_failed_missing_or_wrong_deployment_stops_before_mutation(self):
        for filename in ('staging-deploy.yml', 'staging-rollback.yml'):
            body = workflow_blocks(filename)['Verify deployment belongs to the staging project before mutation']
            for data, status in [('[]', '0'), ('not-json', '0'),
                                 (json.dumps([{'deploymentId': 'wrong', 'versionNumber': 1}]), '0'),
                                 (self.env['TEST_DEPLOYMENTS'], '7')]:
                with self.subTest(filename=filename, data=data, status=status):
                    result = self.execute(body + '\nclasp push -f\n', TEST_DEPLOYMENTS=data, TEST_CLASP_STATUS=status)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse((self.root / 'mutations.txt').exists())

    def test_valid_preflight_allows_next_step(self):
        body = workflow_blocks('staging-deploy.yml')['Verify deployment belongs to the staging project before mutation']
        result = self.execute(body + '\nclasp push -f\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / 'mutations.txt').exists())

    def test_create_version_invalid_json_stops_before_redeploy(self):
        body = workflow_blocks('staging-deploy.yml')['Create immutable version']
        for value in ('Created version 12 commit 99', '{"versionNumber":true}', '{"versionNumber":0}'):
            result = self.execute(body, TEST_CREATED=value)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / 'mutations.txt').exists())
        result = self.execute(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('version=12', (self.root / 'outputs.txt').read_text())
        self.assertFalse((self.root / 'mutations.txt').exists())

    def test_source_verification_failure_stops_deploy_and_rollback(self):
        # Execute each workflow's source check followed by its actual redeploy
        # block. A failed read-back must stop the shell before any clasp call.
        checker = self.root / 'scripts/gas_build_identity.py'
        checker.write_text('import sys\nsys.exit(7)\n', encoding='utf-8')
        for filename, check_name, mutation_name in [
            ('staging-deploy.yml', 'Verify immutable source before repointing deployment',
             'Repoint staging deployment at verified source'),
            ('staging-rollback.yml', 'Verify rollback candidate source before mutation',
             'Repoint deployment at requested version'),
        ]:
            blocks = workflow_blocks(filename)
            result = self.execute(blocks[check_name] + '\n' + blocks[mutation_name], EXPECTED_VERSION='12', ACTOR='fixture')
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((self.root / 'calls.txt').exists())


if __name__ == '__main__':
    unittest.main()
