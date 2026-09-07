"""Execute staging shell logic in disposable fixtures with a fake clasp only."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get('TEST_BASH') or shutil.which('bash')


def run(args, cwd, env=None, check=True):
    return subprocess.run(args, cwd=cwd, env=env, check=check,
                          text=True, capture_output=True)


class StagingHelpers(unittest.TestCase):
    def setUp(self):
        if not BASH:
            self.fail('Bash is required (set TEST_BASH on Windows)')
        self.temp = tempfile.TemporaryDirectory(prefix='staging-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_helper_copies_nested_and_hidden_files_and_cleans_up(self):
        self.check_helper(fail_push=False)

    def test_failed_push_cleans_up_and_stops_before_staging(self):
        self.check_helper(fail_push=True)

    def check_helper(self, fail_push):
        source = self.root / 'apps-script/sample'
        (source / 'nested').mkdir(parents=True)
        (source / 'nested/Code.gs').write_text('nested source')
        (source / '.claspignore').write_text('hidden config')
        (source / '.clasp.json').write_text('{"scriptId":"must-not-use"}')
        (self.root / 'scratch').mkdir()
        (self.root / 'mock-bin').mkdir()
        helper = self.root / 'helper.sh'
        helper.write_text((ROOT / 'scripts/gas_create_staging.sh').read_text(), newline='\n')
        fake = self.root / 'mock-bin/clasp'
        fake.write_text(textwrap.dedent('''\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$1" >> "$TEST_LOG"
            case "$1" in
              create-script)
                test ! -e .clasp.json
                test "$(cat nested/Code.gs)" = 'nested source'
                test "$(cat .claspignore)" = 'hidden config'
                printf '%s' '{"scriptId":"mock-only"}' > .clasp.json ;;
              push) test "$FAIL_PUSH" = 0 ;;
              create-version|create-deployment|list-deployments) : ;;
              *) exit 99 ;;
            esac
            '''), newline='\n')
        fake.chmod(0o755)
        # mktemp and the helper's EXIT trap operate only inside this fixture.
        driver = '''set -euo pipefail
export PATH="$PWD/mock-bin:$PATH"
export TMPDIR="$PWD/scratch"
export TEST_LOG="$PWD/calls.txt"
bash helper.sh sample 'Mock project'
'''
        result = run([BASH, '-c', driver], self.root,
                     {**os.environ, 'FAIL_PUSH': '1' if fail_push else '0'}, check=False)
        self.assertEqual(result.returncode != 0, fail_push, result.stdout + result.stderr)
        self.assertEqual(list((self.root / 'scratch').iterdir()), [])
        calls = (self.root / 'calls.txt').read_text().splitlines()
        self.assertEqual(calls.count('create-script'), 1 if fail_push else 2)
        self.assertEqual(calls.count('create-deployment'), 0 if fail_push else 1)
        self.assertEqual((source / '.clasp.json').read_text(), '{"scriptId":"must-not-use"}')

    def test_push_diff_handles_changed_unchanged_initial_missing_and_invalid_refs(self):
        run(['git', 'init', '-q'], self.root)
        run(['git', 'config', 'user.email', 'test@example.invalid'], self.root)
        run(['git', 'config', 'user.name', 'Offline test'], self.root)
        source = self.root / 'apps-script/sample'
        source.mkdir(parents=True)
        (source / 'Code.gs').write_text('before')
        run(['git', 'add', '.'], self.root)
        run(['git', 'commit', '-qm', 'initial'], self.root)
        before = run(['git', 'rev-parse', 'HEAD'], self.root).stdout.strip()
        (source / 'Code.gs').write_text('after')
        run(['git', 'commit', '-qam', 'change'], self.root)
        after = run(['git', 'rev-parse', 'HEAD'], self.root).stdout.strip()
        workflow = (ROOT / '.github/workflows/staging-deploy.yml').read_text()
        block = re.search(r'id: changed[\s\S]*?run: \|\n([\s\S]*?)(?=\n      - uses:)', workflow)[1]
        body = 'set -euo pipefail\nexport GITHUB_OUTPUT="$PWD/result.txt"\n' + textwrap.dedent(block)
        for old, new, project, expected in [
            (before, after, 'sample', 'skip=false'),
            (before, after, 'other', 'skip=true'),
            ('0' * 40, after, 'sample', 'skip=false'),
            ('f' * 40, after, 'sample', 'skip=false'),
            (before, 'f' * 40, 'sample', None),
        ]:
            with self.subTest(old=old, new=new, project=project):
                output = self.root / 'result.txt'
                output.write_text('')
                result = run([BASH, '-c', body], self.root,
                             {**os.environ, 'BEFORE_SHA': old, 'AFTER_SHA': new, 'PROJECT': project}, check=False)
                self.assertEqual(result.returncode == 0, expected is not None, result.stderr)
                self.assertEqual(output.read_text().strip(), expected or '')


if __name__ == '__main__':
    unittest.main()
