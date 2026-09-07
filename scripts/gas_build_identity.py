#!/usr/bin/env python3
"""Prepare committed Apps Script source and verify a pinned staging build.

Network operations are read-only apart from OAuth token refresh. This module
never pushes source, creates versions, updates deployments or calls paid APIs.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from gas_staging_target import TARGETS, positive_version

PROJECTS = ('governor-page-api', 'glasses-intake-uploader')
MARKER_NAME = 'BuildIdentity'
FILE_TYPES = {'.gs': 'SERVER_JS', '.js': 'SERVER_JS', '.html': 'HTML', '.json': 'JSON'}
EXTENSIONS = {'SERVER_JS': '.gs', 'HTML': '.html', 'JSON': '.json'}
MAX_API_BYTES = 8 * 1024 * 1024


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def parse_json(text):
    return json.loads(text, object_pairs_hook=strict_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON number')))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def canonical_files(files):
    if not isinstance(files, list) or not files:
        raise ValueError('missing source inventory')
    result, names = [], set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('invalid source file')
        name, kind, source = (item.get(key) for key in ('name', 'type', 'source'))
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_/-]+', name)
                or any(part in ('', '.', '..') for part in name.split('/'))
                or name in names or kind not in EXTENSIONS or not isinstance(source, str)):
            raise ValueError('invalid or duplicate source identity')
        names.add(name)
        source = source.replace('\r\n', '\n').replace('\r', '\n')
        if kind == 'JSON':
            if name != 'appsscript':
                raise ValueError('only the Apps Script manifest is a supported JSON source')
            manifest = parse_json(source)
            if not isinstance(manifest, dict):
                raise ValueError('invalid manifest')
            source = canonical(manifest)
        result.append({'name': name, 'type': kind, 'source': source})
    if not any(f['name'] == 'appsscript' and f['type'] == 'JSON' for f in result):
        raise ValueError('missing manifest')
    return sorted(result, key=lambda f: (f['name'], f['type']))


def build(project, commit, repo):
    if project not in PROJECTS:
        raise ValueError('project has no reviewed build-health contract; sweeper is not a gateway')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('a full commit SHA is required')
    prefix = f'apps-script/{project}/'
    tree = subprocess.run(['git', 'ls-tree', '-rz', '--full-tree', commit, '--', prefix],
                          cwd=repo, check=True, capture_output=True).stdout
    files = []
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        meta, path = entry.decode('utf-8').split('\t', 1)
        mode, kind, blob = meta.split()
        if mode not in ('100644', '100755') or kind != 'blob' or not path.startswith(prefix):
            raise ValueError('source must contain regular tracked files only')
        relative = path[len(prefix):]
        suffix = Path(relative).suffix
        if suffix not in FILE_TYPES or any(part.startswith('.') for part in relative.split('/')):
            raise ValueError('unsupported source or clasp override in committed project')
        name = relative[:-len(suffix)]
        if name == MARKER_NAME:
            raise ValueError('build marker must be generated, never committed')
        source = subprocess.run(['git', 'cat-file', 'blob', blob], cwd=repo,
                                check=True, capture_output=True).stdout.decode('utf-8')
        files.append({'name': name, 'type': FILE_TYPES[suffix], 'source': source})
    files = canonical_files(files)
    code = next((f['source'] for f in files if f['name'] == 'Code' and f['type'] == 'SERVER_JS'), '')
    if "p.health === 'build'" not in code or 'sfdc24BuildIdentity_()' not in code:
        raise ValueError('selected commit predates the build-health contract')
    identity = {'schema': 1, 'service': 'sfdc24-build', 'project': project,
                'commit': commit, 'sourceSha256': digest(files)}
    marker = 'function sfdc24BuildIdentity_() {\n  return ' + canonical(identity) + ';\n}\n'
    files = canonical_files(files + [{'name': MARKER_NAME, 'type': 'SERVER_JS', 'source': marker}])
    config = parse_json(TARGETS.read_text(encoding='utf-8'))[project]
    return {'identity': identity, 'scriptId': config['script_id'], 'deploymentId': config['deploy_id'],
            'execUrl': f"https://script.google.com/macros/s/{config['deploy_id']}/exec",
            'files': files, 'fileSetSha256': digest(files)}


def prepare(expected, parent=None):
    run_dir = Path(tempfile.mkdtemp(prefix='gas-build-', dir=parent)).resolve()
    source_dir = run_dir / 'source'
    source_dir.mkdir()
    for item in expected['files']:
        dest = source_dir / (item['name'] + EXTENSIONS[item['type']])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(item['source'], encoding='utf-8', newline='\n')
    (source_dir / '.clasp.json').write_text(canonical({'scriptId': expected['scriptId'], 'rootDir': '.'}), encoding='utf-8')
    return source_dir


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HealthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme != 'https' or parsed.hostname != 'script.googleusercontent.com'
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError('unexpected health redirect')
        return urllib.request.Request(newurl, headers={'Cache-Control': 'no-cache'})


def read_json(request, limit=MAX_API_BYTES, health=False):
    opener = urllib.request.build_opener(HealthRedirect() if health else NoRedirect())
    with opener.open(request, timeout=45) as response:
        if response.status != 200:
            raise ValueError('unsuccessful response')
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('response too large')
        return parse_json(data.decode('utf-8'))


class Reader:
    def __init__(self):
        config = parse_json((Path.home() / '.clasprc.json').read_text(encoding='utf-8'))['tokens']['default']
        body = urllib.parse.urlencode({key: config[key] for key in
                                     ('client_id', 'client_secret', 'refresh_token')} |
                                    {'grant_type': 'refresh_token'}).encode()
        auth = read_json(urllib.request.Request('https://oauth2.googleapis.com/token', data=body), limit=64 * 1024)
        self.token = auth['access_token']
        if not isinstance(self.token, str) or not self.token:
            raise ValueError('missing access token')

    def api(self, path):
        request = urllib.request.Request('https://script.googleapis.com/v1/' + path,
                                         headers={'Authorization': 'Bearer ' + self.token})
        return read_json(request)

    def content(self, expected, version):
        return self.api(f"projects/{expected['scriptId']}/content?versionNumber={version}")

    def deployment(self, expected):
        return self.api(f"projects/{expected['scriptId']}/deployments/{expected['deploymentId']}")

    def health(self, expected, nonce):
        url = expected['execUrl'] + '?health=build&nonce=' + nonce
        return read_json(urllib.request.Request(url, headers={'Cache-Control': 'no-cache'}),
                         limit=16 * 1024, health=True)

    def preflight_health(self, expected, nonce):
        # Governor's first staging version predates health=build. Its existing
        # anonymous machine-read denial is exact JSON and runs no board/provider
        # action, so it is the safe bootstrap identity. Glasses already exposes
        # a public health payload and later stamped versions answer health=build.
        project = expected['identity']['project']
        query = ('?format=json' if project == 'governor-page-api'
                 else '?health=build&nonce=' + nonce)
        return read_json(urllib.request.Request(expected['execUrl'] + query,
                                                headers={'Cache-Control': 'no-cache'}),
                         limit=16 * 1024, health=True)


def verify_source(expected, version, reader):
    version = positive_version(version)
    data = reader.content(expected, version)
    if not isinstance(data, dict) or data.get('scriptId') != expected['scriptId'] or data.get('error'):
        raise ValueError('pinned source belongs to the wrong project or failed')
    files = canonical_files(data.get('files'))
    if files != expected['files']:
        raise ValueError('pinned source does not match the committed build')
    return version


def verify_deployment(expected, version, data):
    if not isinstance(data, dict) or data.get('error') or data.get('deploymentId') != expected['deploymentId']:
        raise ValueError('wrong deployment read-back')
    config = data.get('deploymentConfig')
    if (not isinstance(config, dict) or config.get('scriptId') != expected['scriptId']
            or type(config.get('versionNumber')) is not int or str(config['versionNumber']) != version
            or config.get('manifestFileName') != 'appsscript'):
        raise ValueError('deployment is not pinned to the requested source version')
    entries = data.get('entryPoints')
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ValueError('invalid deployment entry points')
    web = [entry.get('webApp') for entry in entries if entry.get('entryPointType') == 'WEB_APP']
    if len(web) != 1 or not isinstance(web[0], dict) or web[0].get('url') != expected['execUrl']:
        raise ValueError('wrong web-app URL')
    if web[0].get('entryPointConfig') != {'access': 'ANYONE_ANONYMOUS', 'executeAs': 'USER_DEPLOYING'}:
        raise ValueError('unexpected staging web-app access configuration')


def preflight(expected, version, reader):
    """Prove the currently deployed app answers before any staging mutation.

    The first reviewed build is necessarily unstamped, so it cannot return the
    final build receipt yet. An exact, side-effect-free bootstrap response still
    proves that the reviewed Apps Script app ran; a Google sign-in/interstitial
    page or an HTTP 403 cannot satisfy the JSON contract. Later stamped builds
    must echo a fresh nonce and a well-formed identity for the same project.
    """
    version = positive_version(version)
    verify_deployment(expected, version, reader.deployment(expected))
    nonce = secrets.token_hex(16)
    actual = reader.preflight_health(expected, nonce)
    project = expected['identity']['project']
    if (project == 'governor-page-api'
            and actual == {'ok': False, 'error': 'not authorized'}):
        return version
    if (project == 'glasses-intake-uploader' and isinstance(actual, dict)
            and set(actual) == {'actions', 'ok', 'service', 'time', 'version'}
            and actual.get('ok') is True
            and actual.get('service') == 'sfdc24-glasses-uploader'
            and actual.get('version') == 2
            and actual.get('actions') == ['upload', 'prune']
            and isinstance(actual.get('time'), str)
            and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', actual['time'])):
        return version
    if not isinstance(actual, dict) or set(actual) != {
            'ok', 'schema', 'service', 'project', 'commit', 'sourceSha256', 'nonce'}:
        raise ValueError('current staging app did not return its reviewed health contract')
    if (actual.get('ok') is not True or actual.get('schema') != 1
            or actual.get('service') != 'sfdc24-build'
            or actual.get('project') != project
            or not isinstance(actual.get('commit'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', actual['commit'])
            or not isinstance(actual.get('sourceSha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', actual['sourceSha256'])
            or actual.get('nonce') != nonce):
        raise ValueError('current staging app did not return its reviewed health contract')
    return version


def attest(expected, version, reader, retries=6, delay=10):
    version = positive_version(version)
    if not 1 <= retries <= 10 or not 0 <= delay <= 30:
        raise ValueError('retry bounds exceeded')
    verify_deployment(expected, version, reader.deployment(expected))
    verify_source(expected, version, reader)
    for attempt in range(retries):
        nonce = secrets.token_hex(16)
        try:
            actual = reader.health(expected, nonce)
            wanted = {'ok': True, **expected['identity'], 'nonce': nonce}
            if not isinstance(actual, dict) or canonical(actual) != canonical(wanted):
                raise ValueError('live build marker or nonce does not match')
            # A deployment changed during the HTTP read must not receive a receipt.
            verify_deployment(expected, version, reader.deployment(expected))
            return {'schema': 1, 'project': expected['identity']['project'],
                    'commit': expected['identity']['commit'], 'sourceSha256': expected['identity']['sourceSha256'],
                    'fileSetSha256': expected['fileSetSha256'], 'scriptId': expected['scriptId'],
                    'deploymentId': expected['deploymentId'], 'versionNumber': int(version),
                    'verifiedAt': datetime.now(timezone.utc).isoformat(),
                    'checks': ['deployment binding', 'immutable source equality', 'live build and fresh nonce']}
        except (ValueError, OSError, KeyError, TypeError):
            if attempt + 1 == retries:
                raise ValueError('live build could not be verified') from None
            if delay:
                time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'preflight', 'verify-source', 'attest'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--version')
    parser.add_argument('--receipt')
    args = parser.parse_args()
    try:
        expected = build(args.project, args.commit, Path(__file__).resolve().parents[1])
        if args.command == 'prepare':
            path = prepare(expected, os.environ.get('RUNNER_TEMP'))
            print(f'path={path.as_posix()}')
            print(f"source_sha256={expected['identity']['sourceSha256']}")
        else:
            version = positive_version(args.version)
            reader = Reader()
            if args.command == 'preflight':
                preflight(expected, version, reader)
                print('Current staging app returned its reviewed health contract; mutation may proceed.')
            elif args.command == 'verify-source':
                verify_source(expected, version, reader)
                print('Pinned source matches the committed build, including its generated marker.')
            else:
                if not args.receipt:
                    raise ValueError('receipt path required')
                receipt = attest(expected, version, reader)
                Path(args.receipt).write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
                print('Staging deployment, immutable source and live build identity verified.')
    except Exception as error:
        # Never dump remote source, credentials, response bodies or URL parameters.
        print(f'::error::build identity check failed ({type(error).__name__}); no receipt issued', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
