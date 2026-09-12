"""Run offline regression controls against disposable script copies."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--powershell', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
names = ('crosscheck_contract.ps1', 'crosscheck_intake.ps1', 'test_crosscheck_intake.ps1')
original = {name: (root / name).read_bytes() for name in names}
EXPECTED_COUNT = 57


def run(directory, expected):
    proc = subprocess.run(
        [args.powershell, '-NoProfile', '-File', str(directory / 'test_crosscheck_intake.ps1')],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
    )
    markers = re.findall(r'^RESULT passed=(\d+) failed=(\d+)\s*$', proc.stdout, re.M)
    failures = re.findall(r'^FAIL (.*?) : ', proc.stdout, re.M)
    passes = re.findall(r'^PASS .+$', proc.stdout, re.M)
    if (len(markers) != 1 or tuple(map(int, markers[0])) != (EXPECTED_COUNT-len(expected), len(expected))
            or set(failures) != expected or len(failures) != len(expected)
            or len(passes) != EXPECTED_COUNT-len(expected)
            or proc.returncode != (1 if expected else 0) or proc.stderr.strip()):
        raise RuntimeError(f'unexpected test outcome ({proc.returncode})\n{proc.stdout}\n{proc.stderr}')


controls = [
    ('typed document comparison', 'crosscheck_contract.ps1',
     '  # Unordered multisets: preserve element boundaries AND duplicate counts.',
     "  if ($Field -in @('documents_received','documents_pending')) { return (($Left | Sort-Object) -join ', ') -eq (($Right | Sort-Object) -join ', ') }\n"
     '  # Unordered multisets: preserve element boundaries AND duplicate counts.',
     {'document delimiter collision splits', 'same-length document collision splits'}),
    ('schema validation', 'crosscheck_contract.ps1',
     'if ($value.urgent -isnot [bool])', 'if ($false)',
     {'urgent string false is rejected', 'urgent null is rejected', 'entry rejects schema-invalid urgent from both providers'}),
    ('pending-human exit', 'crosscheck_intake.ps1',
     '  exit 2', '  exit 0',
     {'entry returns pending status and displays both split values'}),
]

run(root, set())
print(f'BASELINE passed={EXPECTED_COUNT} failed=0')
for label, filename, old, new, expected in controls:
    with tempfile.TemporaryDirectory(prefix='crosscheck-mutation-') as tmp:
        copied = Path(tmp)
        for name in names:
            shutil.copyfile(root / name, copied / name)
        target = copied / filename
        content = target.read_text(encoding='utf-8-sig')
        if content.count(old) != 1:
            raise RuntimeError(f'{label}: mutation anchor must match once')
        target.write_text(content.replace(old, new), encoding='utf-8', newline='\n')
        run(copied, expected)
        print(f'CAUGHT {label}: {len(expected)} intended failures')
for name, before in original.items():
    if (root / name).read_bytes() != before:
        raise RuntimeError(f'source changed: {name}')
print('SOURCE_UNCHANGED ' + ' '.join(f'{name}:{hashlib.sha256(value).hexdigest()}' for name, value in original.items()))
print('RESULT mutation_controls=3 caught=3')
