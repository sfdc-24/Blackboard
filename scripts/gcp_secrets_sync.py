#!/usr/bin/env python3
r"""Put the fleet's credentials in Google Secret Manager, without ever printing one.

WHY THIS EXISTS
    Mr Salam chose Secret Manager + Cloud Run on 2026-09-23 as the answer to
    "where do bus and provider credentials live for a cloud runtime" - the open
    dependency 2 of docs/OPENAI-CLOUD-MIGRATION.md. It is not greenfield: the
    live `sfdc24-stt-relay` Cloud Run service already reads DEEPGRAM_API_KEY and
    RELAY_AUTH_SECRET this way. This extends a working pattern rather than
    inventing one.

THE TWO RULES THIS SCRIPT IS BUILT AROUND

    1. A VALUE NEVER REACHES A COMMAND LINE. Anything passed as an argument is
       visible in the process list to every process on the box, and lands in
       shell history. Values go to gcloud over STDIN, via --data-file=-, and
       nothing else. There is no code path here that puts a secret in argv, and
       --dry-run prints lengths, never content.

    2. A VALUE NEVER REACHES STDOUT. Every line this prints is a NAME, a length,
       or an outcome. That matters because this output goes into a session
       transcript and, when a waker runs it, into a log.

IDEMPOTENT BY DESIGN
    A secret that exists gets a NEW VERSION only when the value actually differs
    from `latest`. Re-running with an unchanged .env adds nothing, so this is
    safe to run repeatedly and safe to run from CI. Comparison is done on a
    SHA-256 digest, so the existing value is fetched but never displayed.

    python scripts/gcp_secrets_sync.py --dry-run
    python scripts/gcp_secrets_sync.py --only BUS_URL,BUS_SECRET
    python scripts/gcp_secrets_sync.py --grant-accessor 96522051727-compute@developer.gserviceaccount.com
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "sfdc24"

# Windows keeps the SDK out of PATH for this shell. Resolve it rather than
# telling the caller to fix their PATH.
CANDIDATES = [
    os.environ.get("GCLOUD_BIN") or "",
    "gcloud",
    str(Path(os.environ.get("LOCALAPPDATA", "")) /
        "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd"),
    r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
]

# Keys that must NOT be synced. WA_TO is his phone number and MODEL_PROVIDER,
# ANTHROPIC_MODEL and OPENAI_MODEL are configuration, not credentials - putting
# config in a secret store means a model change needs a secret rotation.
NOT_SECRETS = {"WA_TO", "MODEL_PROVIDER", "ANTHROPIC_MODEL", "OPENAI_MODEL"}


def gcloud_bin() -> str:
    for c in CANDIDATES:
        if not c:
            continue
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=120,
                           check=True)
            return c
        except Exception:  # noqa: BLE001 - a missing candidate is not an error
            continue
    raise SystemExit("gcloud not found. Set GCLOUD_BIN to its full path.")


def run(gb: str, args: list, stdin_bytes: bytes | None = None,
        timeout: int = 300) -> subprocess.CompletedProcess:
    """Never put a secret in `args`. Values go in stdin_bytes."""
    return subprocess.run([gb] + args, input=stdin_bytes, capture_output=True,
                          timeout=timeout)


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m and not line.lstrip().startswith("#"):
            env[m.group(1)] = m.group(2).strip('"').strip("'")
    return env


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def existing_secrets(gb: str) -> set:
    p = run(gb, ["secrets", "list", "--project", PROJECT, "--format=value(name)"])
    if p.returncode != 0:
        raise SystemExit("could not list secrets: %s"
                         % p.stderr.decode("utf-8", "replace")[:300])
    return {l.strip() for l in p.stdout.decode("utf-8", "replace").splitlines()
            if l.strip()}


def current_digest(gb: str, name: str) -> str | None:
    """Fetch `latest` only to hash it. The value is never printed or returned."""
    p = run(gb, ["secrets", "versions", "access", "latest", "--secret", name,
                 "--project", PROJECT])
    if p.returncode != 0:
        return None
    return digest(p.stdout.decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--only", help="comma-separated key names to sync")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--grant-accessor", metavar="SERVICE_ACCOUNT",
                    help="grant roles/secretmanager.secretAccessor on each "
                         "synced secret to this service account")
    args = ap.parse_args()

    gb = gcloud_bin()
    env = load_env(Path(args.env_file))
    wanted = ([k.strip() for k in args.only.split(",") if k.strip()]
              if args.only else sorted(env))
    wanted = [k for k in wanted if k not in NOT_SECRETS]

    missing = [k for k in wanted if not (env.get(k) or "").strip()]
    if missing:
        print("skipping %d key(s) absent or empty in the env file: %s"
              % (len(missing), ", ".join(missing)))
    wanted = [k for k in wanted if (env.get(k) or "").strip()]

    have = existing_secrets(gb)
    print("project=%s  candidates=%d  already in Secret Manager=%d"
          % (PROJECT, len(wanted), len(have)))
    print()

    created, updated, unchanged, failed = [], [], [], []
    for name in wanted:
        value = env[name]
        if args.dry_run:
            state = "exists" if name in have else "would create"
            print("  %-28s %-14s %d chars" % (name, state, len(value)))
            continue

        if name not in have:
            p = run(gb, ["secrets", "create", name, "--project", PROJECT,
                         "--replication-policy=automatic", "--data-file=-"],
                    stdin_bytes=value.encode("utf-8"))
            if p.returncode == 0:
                created.append(name)
                print("  %-28s created" % name)
            else:
                failed.append(name)
                print("  %-28s FAILED  %s" % (
                    name, p.stderr.decode("utf-8", "replace").strip()[:120]))
                continue
        else:
            if current_digest(gb, name) == digest(value):
                unchanged.append(name)
                print("  %-28s unchanged" % name)
            else:
                p = run(gb, ["secrets", "versions", "add", name,
                             "--project", PROJECT, "--data-file=-"],
                        stdin_bytes=value.encode("utf-8"))
                if p.returncode == 0:
                    updated.append(name)
                    print("  %-28s new version added" % name)
                else:
                    failed.append(name)
                    print("  %-28s FAILED  %s" % (
                        name, p.stderr.decode("utf-8", "replace").strip()[:120]))
                    continue

        if args.grant_accessor:
            p = run(gb, ["secrets", "add-iam-policy-binding", name,
                         "--project", PROJECT,
                         "--member=serviceAccount:" + args.grant_accessor,
                         "--role=roles/secretmanager.secretAccessor"])
            if p.returncode != 0:
                print("  %-28s   grant FAILED %s" % (
                    name, p.stderr.decode("utf-8", "replace").strip()[:100]))

    print()
    if args.dry_run:
        print("dry run: nothing was written")
        return 0
    print("created=%d  updated=%d  unchanged=%d  failed=%d"
          % (len(created), len(updated), len(unchanged), len(failed)))
    if failed:
        print("failed: %s" % ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
