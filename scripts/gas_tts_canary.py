#!/usr/bin/env python3
"""Build and validate the isolated SITE-P0-TTS-001 canary contract.

This tool is intentionally non-deploying.  It prepares a minimal, reviewed
Apps Script API-executable bundle only after a dedicated canary script ID is
recorded in the repository and matched by repository variables.  It never
creates a script, pushes source, creates a version/deployment, invokes
``scripts.run``, or reads provider credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from gas_build_identity import canonical, canonical_files, digest, parse_json


PROJECT = 'governor-page-api'
TARGETS = Path(__file__).with_name('gas_tts_canary_target.json')
TEMPLATE_PATH = 'tests/fixtures/site-p0-tts-canary.gs.template'
SOURCE_PATH = 'apps-script/governor-page-api/Code.gs'
SCRIPT_ID = re.compile(r'[A-Za-z0-9_-]{20,}', re.ASCII)
RUN_ID = re.compile(r'[a-f0-9]{24}', re.ASCII)
FULL_SHA = re.compile(r'[a-f0-9]{40}', re.ASCII)

# Reviewed non-canary projects which are not all represented by a tracked
# .clasp.json. Keep this fail-closed list aligned with docs/CICD.md and
# docs/CICD-STAGING-IDS.md when the estate changes.
KNOWN_NON_CANARY_SCRIPT_IDS = {
    '1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp',
    '1XBE2qVMiIu8xOq5jks4T3BG3o6CRFx8HKsWXvbXIJ6sOefPUBN-bVOh-',
    '1PBfO1sPQmGTXPHWrAAot2wCgSCPizO2uC8RUwUKQ5hn7A7dUq0U4_Q_2',
    '1meav8p2zkRt-8obarV_fB5Q2EyExCvAaoZa3ro9_fmo4OE_95FpWkfu9',
    '1NBDw5Ya81Y8XlDME01N7GGM7UwproSvqIdtaeuMBu4iAhlGfDbYwmeoP',
    '1sOVS20USpkDZ7po8wFK-DgfujYI_1Vq-jgcGUEhJyCIGLZ0k0bRoMNAf',
    '1rjnl6ianZEKilaWFZDE3CfZANmUCArAN9sHB8NR6RCpQDUDgrBiQ-uoD',
}

EXACT_FUNCTIONS = (
    'jsonp_', 'ttsVoice_', 'ttsConfigured_', 'ttsDay_', 'ttsSessionHash_',
    'ttsLimit_', 'ttsBudget_', 'purgeExpiredTtsKeys_', 'mintTtsKey_',
    'claimTts_', 'TTS_STYLE_',
)
CRITICAL_FUNCTIONS = ('ttsBudget_', 'mintTtsKey_', 'claimTts_')
EXACT_CONSTANTS = (
    'TTS_SESSION_DEFAULT', 'TTS_DAILY_DEFAULT', 'TTS_KEY_TTL_SECS',
    'TTS_MODEL', 'TTS_DEFAULT', 'TTS_MAX_CHARS', 'TTS_VOICES',
)

MANIFEST = {
    'dependencies': {},
    'exceptionLogging': 'STACKDRIVER',
    'executionApi': {'access': 'MYSELF'},
    'oauthScopes': ['https://www.googleapis.com/auth/userinfo.email'],
    'runtimeVersion': 'V8',
    'timeZone': 'America/Toronto',
}


def sha256_text(value):
    return hashlib.sha256(value.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')).hexdigest()


def git_file(repo, commit, path):
    if not FULL_SHA.fullmatch(str(commit or '')):
        raise ValueError('a full commit SHA is required')
    result = subprocess.run(['git', 'show', f'{commit}:{path}'], cwd=repo,
                            check=True, capture_output=True)
    return result.stdout.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')


def _matching_brace(source, opening):
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    i = opening
    while i < len(source):
        char = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ''
        if line_comment:
            if char == '\n':
                line_comment = False
        elif block_comment:
            if char == '*' and nxt == '/':
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"', '`'):
            quote = char
        elif char == '/' and nxt == '/':
            line_comment = True
            i += 1
        elif char == '/' and nxt == '*':
            block_comment = True
            i += 1
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return i
            if depth < 0:
                break
        i += 1
    raise ValueError('unterminated function')


def extract_function(source, name):
    pattern = re.compile(rf'^function {re.escape(name)}\s*\(', re.MULTILINE)
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise ValueError(f'expected exactly one {name} function')
    start = matches[0].start()
    opening = source.find('{', matches[0].end())
    if opening < 0:
        raise ValueError(f'missing body for {name}')
    end = _matching_brace(source, opening) + 1
    return source[start:end]


def extract_constant(source, name):
    matches = list(re.finditer(rf'^var\s+{re.escape(name)}\s*=.*?;\s*(?://[^\n]*)?$',
                               source, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError(f'expected exactly one {name} declaration')
    return matches[0].group(0).rstrip()


def replace_placeholders(template, values):
    result = template
    for name, value in values.items():
        marker = '__' + name + '__'
        if result.count(marker) != 1:
            raise ValueError(f'expected exactly one {marker} placeholder')
        result = result.replace(marker, value)
    if re.search(r'__[A-Z0-9_]+__', result):
        raise ValueError('unresolved canary template placeholder')
    return result


def render_bundle(code, template, commit, canonical_source_sha256, run_id):
    if not FULL_SHA.fullmatch(str(commit or '')) or not RUN_ID.fullmatch(str(run_id or '')):
        raise ValueError('invalid commit or run id')
    if not re.fullmatch(r'[a-f0-9]{64}', str(canonical_source_sha256 or '')):
        raise ValueError('invalid canonical source hash')

    exact = {name: extract_function(code, name) for name in EXACT_FUNCTIONS}
    hashes = {name: sha256_text(exact[name]) for name in CRITICAL_FUNCTIONS}
    critical_hash = digest(hashes)
    audio = extract_function(code, 'ttsAudio_')
    needle = 'UrlFetchApp.fetch('
    if audio.count(needle) != 1:
        raise ValueError('canonical paid-provider boundary changed')
    audio = audio.replace(needle, 'siteP0CanaryProviderFetch_(', 1)
    if 'UrlFetchApp' in audio:
        raise ValueError('prepared audio path still contains UrlFetchApp')

    prefix = 'SITE_P0_' + run_id.upper() + '_'
    wrapper = replace_placeholders(template, {
        'RUN_ID_JSON': canonical(run_id),
        'COMMIT_JSON': canonical(commit),
        'SOURCE_SHA256_JSON': canonical(canonical_source_sha256),
        'FUNCTION_SHA256_JSON': canonical(critical_hash),
        'PROPERTY_PREFIX_JSON': canonical(prefix),
    })
    constants = [extract_constant(code, name) for name in EXACT_CONSTANTS]
    constants.extend([
        'var TTS_KEY_PROP_PREFIX = ' + canonical(prefix + 'KEY_') + ';',
        'var TTS_BUDGET_STATE = ' + canonical(prefix + 'BUDGET') + ';',
    ])
    pieces = constants + [wrapper] + [exact[name] for name in EXACT_FUNCTIONS] + [audio]
    source = '\n\n'.join(piece.rstrip() for piece in pieces) + '\n'

    forbidden = ('SpreadsheetApp', 'DriveApp', 'MailApp', 'GOVERNOR_PASS',
                 'ANTHROPIC_KEY', 'ALPHA_ID', 'function doGet', 'function doPost')
    if any(item in source for item in forbidden) or source.count('siteP0CanaryProviderFetch_(') != 2:
        raise ValueError('prepared source crossed the canary capability boundary')
    top_level = set(re.findall(r'^function\s+([A-Za-z0-9_$]+)\s*\(', source, re.MULTILINE))
    expected = set(EXACT_FUNCTIONS) | {
        'ttsAudio_', 'siteP0CanaryPreflight', 'siteP0CanaryStart',
        'siteP0CanaryPrepare', 'siteP0CanaryConsume', 'siteP0CanarySnapshot',
        'siteP0CanaryCleanup', 'siteP0RunOk_', 'siteP0Authorized_',
        'siteP0PropertyNameOk_',
        'siteP0Session_', 'siteP0MintSlot_', 'siteP0ConsumeArgsOk_',
        'siteP0Reason_', 'siteP0AwaitBarrier_', 'siteP0ObserveLoser_',
        'siteP0CanaryProviderFetch_', 'siteP0ResetScenario_', 'siteP0Sha256_',
    }
    if top_level != expected:
        raise ValueError('prepared source has an unexpected top-level function inventory')

    files = canonical_files([
        {'name': 'Code', 'type': 'SERVER_JS', 'source': source},
        {'name': 'appsscript', 'type': 'JSON', 'source': canonical(MANIFEST)},
    ])
    return {
        'files': files,
        'canonicalCodeSha256': sha256_text(code),
        'canonicalSourceSha256': canonical_source_sha256,
        'criticalFunctions': hashes,
        'criticalFunctionSha256': critical_hash,
        'preparedSourceSha256': digest(files),
        'runId': run_id,
        'commit': commit,
    }


def canonical_source_identity(repo, commit):
    # Import lazily to keep the render unit independently testable.
    from gas_build_identity import build
    return build(PROJECT, commit, repo)['identity']['sourceSha256']


def build_from_commit(repo, commit, run_id):
    code = git_file(repo, commit, SOURCE_PATH)
    template = git_file(repo, commit, TEMPLATE_PATH)
    return render_bundle(code, template, commit, canonical_source_identity(repo, commit), run_id)


def denied_script_ids(repo):
    denied = set(KNOWN_NON_CANARY_SCRIPT_IDS)
    root = parse_json((repo / '.clasp.json').read_text(encoding='utf-8'))
    if isinstance(root, dict) and isinstance(root.get('scriptId'), str):
        denied.add(root['scriptId'])
    staging = parse_json((repo / 'scripts/gas_staging_targets.json').read_text(encoding='utf-8'))
    if not isinstance(staging, dict):
        raise ValueError('invalid staging target inventory')
    for value in staging.values():
        if not isinstance(value, dict) or not isinstance(value.get('script_id'), str):
            raise ValueError('invalid staging target inventory')
        denied.add(value['script_id'])
    return denied


def resolve_target(repo, variables=None, inventory=None):
    variables = dict(os.environ) if variables is None else variables
    inventory = parse_json(TARGETS.read_text(encoding='utf-8')) if inventory is None else inventory
    if not isinstance(inventory, dict) or inventory.get('schema') != 1 or inventory.get('status') != 'READY':
        raise ValueError('dedicated canary project is not provisioned')
    script_id = inventory.get('script_id')
    if not isinstance(script_id, str) or not SCRIPT_ID.fullmatch(script_id):
        raise ValueError('invalid reviewed canary script id')
    if variables.get('SITE_P0_TTS_CANARY_SCRIPT_ID') != script_id:
        raise ValueError('repository variable differs from reviewed canary target')
    if script_id in denied_script_ids(repo):
        raise ValueError('canary target overlaps a production or staging script')
    return {'script_id': script_id}


def prepare(bundle, script_id, parent=None):
    if not SCRIPT_ID.fullmatch(str(script_id or '')):
        raise ValueError('invalid canary script id')
    output = Path(tempfile.mkdtemp(prefix='site-p0-tts-canary-', dir=parent)).resolve()
    for item in bundle['files']:
        suffix = '.gs' if item['type'] == 'SERVER_JS' else '.json'
        (output / (item['name'] + suffix)).write_text(item['source'], encoding='utf-8', newline='\n')
    (output / '.clasp.json').write_text(canonical({'rootDir': '.', 'scriptId': script_id}),
                                       encoding='utf-8', newline='\n')
    return output


def _exact_keys(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def validate_receipt(value, expected_script_id):
    if not SCRIPT_ID.fullmatch(str(expected_script_id or '')):
        raise ValueError('a reviewed canary script id is required')
    root_keys = {'schema', 'repository', 'commit', 'canonicalCodeSha256',
                 'canonicalSourceSha256', 'criticalFunctionSha256',
                 'preparedSourceSha256', 'canaryScriptId', 'canaryVersion',
                 'apiDeploymentId', 'providerKind', 'effectiveCaps', 'scenarios',
                 'privacy', 'cleanup', 'verdict'}
    if not _exact_keys(value, root_keys) or value.get('schema') != 'site-p0-tts-canary.v1':
        raise ValueError('invalid receipt schema')
    for key in ('commit',):
        if not FULL_SHA.fullmatch(str(value.get(key, ''))):
            raise ValueError('invalid receipt commit')
    for key in ('canonicalCodeSha256', 'canonicalSourceSha256',
                'criticalFunctionSha256', 'preparedSourceSha256'):
        if not re.fullmatch(r'[a-f0-9]{64}', str(value.get(key, ''))):
            raise ValueError('invalid receipt digest')
    if (value.get('repository') != 'sfdc-24/Blackboard' or value.get('verdict') != 'PASS'
            or value.get('providerKind') != 'credential-free synthetic admission counter'
            or value.get('canaryScriptId') != expected_script_id
            or type(value.get('canaryVersion')) is not int or value['canaryVersion'] < 1
            or not SCRIPT_ID.fullmatch(str(value.get('apiDeploymentId', '')))):
        raise ValueError('invalid receipt identity')
    caps = value.get('effectiveCaps')
    if not _exact_keys(caps, ('session', 'daily')) or any(type(caps[k]) is not int or caps[k] < 1
                                                          for k in caps):
        raise ValueError('invalid effective caps')
    scenarios = value.get('scenarios')
    if not _exact_keys(scenarios, ('valid', 'replay', 'concurrency', 'sessionCap', 'dailyCap')):
        raise ValueError('invalid scenario inventory')
    expected = {
        'valid': {'accepted': True, 'attemptDelta': 1},
        'replay': {'reason': 'expired', 'attemptDelta': 0},
        'sessionCap': {'acceptedDelta': 1, 'rejectedReason': 'tts-session-cap', 'rejectedDelta': 0},
        'dailyCap': {'acceptedDelta': 1, 'rejectedReason': 'tts-daily-cap', 'rejectedDelta': 0},
    }
    for name, wanted in expected.items():
        if scenarios.get(name) != wanted:
            raise ValueError('scenario did not prove the cardinality contract')
    concurrency = scenarios.get('concurrency')
    if concurrency != {'readyCount': 2, 'winnerCount': 1, 'loserReason': 'expired',
                        'attempts': 1, 'loserObservedWhileProviderInFlight': True}:
        raise ValueError('concurrency did not prove overlap and one admission')
    privacy = value.get('privacy')
    if privacy != {'providerCredentialsLoaded': False, 'freeformTextAccepted': False,
                   'rawResponsesPreserved': False, 'audioKeysPreserved': False}:
        raise ValueError('invalid privacy proof')
    cleanup = value.get('cleanup')
    if cleanup != {'propertiesRemaining': 0, 'trackedCacheEntriesRemaining': 0,
                   'temporaryDeploymentPresent': False, 'headRestored': True}:
        raise ValueError('invalid cleanup proof')
    encoded = canonical(value)
    forbidden = ('OPENAI_KEY', 'Bearer ', 'Synthetic cardinality probe', '"ak')
    if any(item in encoded for item in forbidden):
        raise ValueError('receipt contains forbidden material')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--commit', required=True)
    inspect.add_argument('--run-id', default='0' * 24)
    prep = sub.add_parser('prepare')
    prep.add_argument('--commit', required=True)
    prep.add_argument('--run-id', required=True)
    receipt = sub.add_parser('validate-receipt')
    receipt.add_argument('--receipt', required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        if args.command == 'validate-receipt':
            target = resolve_target(repo)
            validate_receipt(parse_json(Path(args.receipt).read_text(encoding='utf-8')),
                             target['script_id'])
            print('receipt=valid')
            return 0
        if args.command == 'inspect':
            bundle = build_from_commit(repo, args.commit, args.run_id)
            print(canonical({key: bundle[key] for key in (
                'commit', 'canonicalCodeSha256', 'canonicalSourceSha256',
                'criticalFunctionSha256', 'preparedSourceSha256')}))
            return 0
        target = resolve_target(repo)
        bundle = build_from_commit(repo, args.commit, args.run_id)
        output = prepare(bundle, target['script_id'], os.environ.get('RUNNER_TEMP'))
        print('path=' + output.as_posix())
        print('prepared_source_sha256=' + bundle['preparedSourceSha256'])
        return 0
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        # Never echo source, OAuth material, property values, raw API responses,
        # audio keys, or repository variables from a failing canary operation.
        print('::error::SITE-P0 TTS canary contract validation failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
