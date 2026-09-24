#!/usr/bin/env python3
"""Is the LIVE Governor the source on main? Read-only.

gas/Code.js is not the deployment, and on 2026-09-24 the live version ran ahead
of main twice (v68 before its PR merged; v69 built and not deployed). This asks
clasp which version the public deployment serves, pulls THAT version's source
into a temporary copy of the project (never the working deploy folder), and
compares every file with the one on origin/main. It changes nothing: no push,
no version, no deploy.

    python scripts/governor_live_check.py
    python scripts/governor_live_check.py --deployment-id <id> --project-dir <dir>

Exit 0 when every file matches (line endings ignored), 1 on any difference, 2
when it cannot tell. It prints file names and line counts, never file bodies.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DEPLOYMENT = "AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng"
DEFAULT_PROJECT = Path("C:/Users/salam/Quantum/.gas-pull-20260924")
SOURCE_DIR = "apps-script/governor-page-api"


def clasp(args: list[str], cwd: Path) -> str:
    exe = shutil.which("clasp") or shutil.which("clasp.cmd")
    if not exe:
        raise SystemExit(2)
    out = subprocess.run([exe] + args, cwd=cwd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=180)
    if out.returncode != 0:
        # Fixed text only: clasp's own output can carry account or project
        # details, so it is never echoed.
        print("clasp %s failed (exit %d); cannot tell" % (args[0], out.returncode))
        raise SystemExit(2)
    return out.stdout


def deployed_version(deployments: str, deployment_id: str) -> int:
    for line in deployments.splitlines():
        # Parse the line, then compare the id EXACTLY: "- <id> @<n> [- desc]".
        # A pattern searched inside the line let a longer id that merely ends
        # with ours (or carries a hyphenated prefix) answer for it.
        m = re.match(r"^\s*-\s+(\S+)\s+@(\d+)(?:\s|$)", line)
        if m and m.group(1) == deployment_id:
            return int(m.group(2))
    print("deployment %s not found in `clasp deployments`" % deployment_id[:12])
    raise SystemExit(2)


def fresh_main() -> str:
    """Fetch, then pin origin/main to one SHA for the whole comparison. A fetch
    that fails would leave a stale origin/main looking current, so it is
    UNKNOWN, not a match."""
    if subprocess.run(["git", "fetch", "-q", "origin"], cwd=REPO).returncode != 0:
        print("git fetch failed; cannot tell whether main is current")
        raise SystemExit(2)
    return subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=REPO, text=True).strip()


def main_files(sha: str) -> list[str]:
    """Every file under the source dir, recursively, as a relative posix path.
    NUL-separated so a name with a space stays one name."""
    out = subprocess.check_output(["git", "ls-tree", "-r", "-z", "--name-only", "%s:%s" % (sha, SOURCE_DIR)],
                                  cwd=REPO, text=True, encoding="utf-8")
    return sorted(n for n in out.split("\0") if n)


def live_files(root: Path) -> dict[str, Path]:
    """Every pulled file, recursively, keyed by the same relative posix path."""
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
            if p.is_file() and p.relative_to(root).as_posix() != ".clasp.json"}


def main_source(sha: str, name: str) -> str | None:
    try:
        return subprocess.check_output(["git", "show", "%s:%s/%s" % (sha, SOURCE_DIR, name)],
                                       cwd=REPO, text=True, encoding="utf-8", errors="replace",
                                       stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return None


def norm(text: str) -> list[str]:
    return text.replace("\r\n", "\n").rstrip("\n").split("\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare the live Governor version with origin/main")
    ap.add_argument("--deployment-id", default=DEFAULT_DEPLOYMENT)
    ap.add_argument("--project-dir", type=Path, default=DEFAULT_PROJECT)
    args = ap.parse_args()

    sha = fresh_main()
    clasp_json = args.project_dir / ".clasp.json"
    if not clasp_json.exists():
        print("no .clasp.json in %s" % args.project_dir)
        return 2
    version = deployed_version(clasp(["deployments"], args.project_dir), args.deployment_id)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        shutil.copy(clasp_json, tmp / ".clasp.json")
        cfg = json.loads(clasp_json.read_text(encoding="utf-8"))
        cfg["rootDir"] = "."
        (tmp / ".clasp.json").write_text(json.dumps(cfg), encoding="utf-8")
        clasp(["pull", "--versionNumber", str(version)], tmp)
        live = live_files(tmp)
        if not live:
            print("the pull of version %d returned no files; cannot tell" % version)
            return 2
        on_main_names = main_files(sha)
        print("public deployment serves version %d (%d files); main is %s (%d files)"
              % (version, len(live), sha[:12], len(on_main_names)))
        differ = 0
        # The UNION of both sides: a file only on main is as much a drift as a
        # file only live.
        for name in sorted(set(live) | set(on_main_names)):
            if name not in live:
                print("  %-22s main only (not in the live version)" % name)
                differ += 1
                continue
            on_main = main_source(sha, name)
            if on_main is None:
                print("  %-22s live only (not on main)" % name)
                differ += 1
                continue
            f = live[name]
            a, b = norm(on_main), norm(f.read_text(encoding="utf-8", errors="replace"))
            if a == b:
                print("  %-22s MATCH" % name)
                continue
            changed = sum(1 for d in difflib.unified_diff(a, b, lineterm="", n=0)
                          if d[:1] in "+-" and not d.startswith(("+++", "---")))
            print("  %-22s DIFF (%d changed lines between main and live)" % (name, changed))
            differ += 1
    print("VERDICT: %s" % ("live == main" if not differ else "live != main in %d file(s)" % differ))
    return 1 if differ else 0


if __name__ == "__main__":
    raise SystemExit(main())
