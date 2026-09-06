#!/usr/bin/env python3
"""Validate reviewed staging targets and clasp 3.4.1 JSON before mutation.

This binds configuration to a reviewed staging inventory. It does not certify
runtime health, deployed source identity, or permission to promote production.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

TARGETS = Path(__file__).with_name('gas_staging_targets.json')
ID = re.compile(r'[A-Za-z0-9_-]+', re.ASCII)


def positive_version(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value, re.ASCII):
        return value
    raise ValueError('version must be a canonical positive integer')


def resolve(project, variables, inventory):
    if not isinstance(inventory, dict) or project not in inventory:
        raise ValueError('project is not in the reviewed staging inventory')
    if not isinstance(variables, dict):
        raise ValueError('repository variables must be an object')
    expected = inventory[project]
    if not isinstance(expected, dict):
        raise ValueError('invalid staging inventory entry')
    key = project.upper().replace('-', '_')
    resolved = {}
    for field, prefix in [('script_id', 'SCRIPT_ID'), ('deploy_id', 'DEPLOYMENT_ID')]:
        value = variables.get(f'STAGING_{prefix}_{key}')
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise ValueError(f'invalid staging {field}')
        if value != expected.get(field):
            raise ValueError(f'{field} differs from the reviewed staging inventory')
        resolved[field] = value
    url = f"https://script.google.com/macros/s/{resolved['deploy_id']}/exec"
    if variables.get(f'STAGING_EXEC_URL_{key}') != url:
        raise ValueError('exec URL must exactly match the reviewed staging deployment')
    resolved['exec_url'] = url
    return resolved


def deployment_version(data, deploy_id, expected=None):
    # clasp 3.4.1 --json list-deployments returns a flat array. Never inspect
    # descriptions: text resembling another id or @version is not evidence.
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError('deployment read-back must be a JSON array of objects')
    rows = [row for row in data if row.get('deploymentId') == deploy_id]
    if len(rows) != 1:
        raise ValueError('expected exactly one matching staging deployment')
    version = rows[0].get('versionNumber')
    if type(version) is not int or version < 1:
        raise ValueError('staging deployment must reference an immutable version')
    if expected is not None and str(version) != positive_version(expected):
        raise ValueError('staging deployment version does not match')
    return str(version)


def created_version(data):
    if not isinstance(data, dict) or set(data) != {'versionNumber'}:
        raise ValueError('create-version must return a versionNumber object')
    if type(data['versionNumber']) is not int:
        raise ValueError('create-version must return an integer versionNumber')
    return positive_version(data['versionNumber'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('resolve')
    sub.add_parser('version')
    sub.add_parser('created-version')
    check = sub.add_parser('deployment')
    check.add_argument('--expect-version')
    args = parser.parse_args()
    try:
        if args.command == 'resolve':
            result = resolve(os.environ.get('PROJECT', ''),
                             json.loads(os.environ.get('VARS_JSON', '{}')),
                             json.loads(TARGETS.read_text(encoding='utf-8')))
            print('\n'.join(f'{key}={value}' for key, value in result.items()))
        elif args.command == 'version':
            print(positive_version(os.environ.get('REQUESTED_VERSION', '')))
        elif args.command == 'created-version':
            print(created_version(json.load(sys.stdin)))
        else:
            print(deployment_version(json.load(sys.stdin),
                                     os.environ.get('DEPLOY_ID', ''), args.expect_version))
    except (ValueError, TypeError, KeyError, OSError):
        # Do not echo repository variables, command input, or response bodies.
        print('::error::staging target/version validation failed; inspect the reviewed inventory and JSON contract', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
