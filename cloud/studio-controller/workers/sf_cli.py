"""The ONE way this repository talks to Salesforce for the build lane: the official sf CLI.

Mr. Salam (2026-10-09): "ensure that you are using all tools and processes available; so
you don't build redundancy". Deploy and validate (checkOnly), deploy reports, describe,
queries, sample-data import, permission set assignment, scratch org create/delete, limits
and snapshots all go through `sf ... --json`, using the CLI's own auth (the laptop's
logins today; `sf org login jwt` against the Dev Hub on a server - see the PR).

A closed allowlist of commands. Nothing that prints a credential can be run through it
(`org auth show-*`, `org display --verbose`), and its errors carry the CLI's message only.

    cli = SfCli()
    cli.run("sobject", "describe", "--sobject", "Lead", "--target-org", "sfdc24web")
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

ALLOWED = (
    ("org", "display"), ("org", "list", "limits"), ("org", "assign", "permset"),
    ("org", "create", "scratch"), ("org", "delete", "scratch"),
    ("sobject", "describe"), ("sobject", "list"),
    ("data", "query"), ("data", "import", "tree"), ("data", "create", "record"),
    ("project", "deploy", "start"), ("project", "deploy", "report"),
)
NEVER = ("--verbose", "show-access-token", "show-sfdx-auth-url", "show-user-password", "auth")


class SalesforceError(RuntimeError):
    """The CLI or Salesforce refused; the message is theirs (never a credential)."""


class SfCli:
    def __init__(self, runner=subprocess.run, binary: str | None = None, timeout: float = 900):
        self.runner = runner
        self.binary = binary or os.environ.get("SF_CLI") or shutil.which("sf") or "sf"
        self.timeout = timeout

    def run(self, *args: str, cwd: str | None = None, timeout: float | None = None):
        args = [str(a) for a in args]
        if not any(tuple(args[:len(prefix)]) == prefix for prefix in ALLOWED):
            raise SalesforceError("sf %s is not an allowed command" % " ".join(args[:3]))
        if any(a in NEVER for a in args):
            raise SalesforceError("refusing an sf command that could print a credential")
        proc = self.runner([self.binary] + args + ["--json"], capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout or self.timeout, cwd=cwd)
        try:
            out = json.loads(proc.stdout or "{}")
        except ValueError:
            raise SalesforceError("sf returned no JSON (%s)" % (proc.stderr or "")[:200]) from None
        if int(out.get("status", 0) or 0) != 0:
            message = str(out.get("message") or out.get("name") or "sf failed")
            result = out.get("result")
            if isinstance(result, dict) and result.get("status"):
                return result                  # a finished deploy that failed: its result carries the errors
            raise SalesforceError(message[:500])
        return out.get("result")


__all__ = ["SfCli", "SalesforceError", "ALLOWED"]
