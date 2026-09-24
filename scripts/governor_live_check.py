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
        print("clasp %s failed: %s" % (" ".join(args), (out.stderr or out.stdout).strip()[:300]))
        raise SystemExit(2)
    return out.stdout


def deployed_version(deployments: str, deployment_id: str) -> int:
    for line in deployments.splitlines():
        m = re.search(re.escape(deployment_id) + r"\s+@(\d+)", line)
        if m:
            return int(m.group(1))
    print("deployment %s not found in `clasp deployments`" % deployment_id[:12])
    raise SystemExit(2)


def main_source(name: str) -> str | None:
    try:
        return subprocess.check_output(["git", "show", "origin/main:%s/%s" % (SOURCE_DIR, name)],
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

    subprocess.run(["git", "fetch", "-q", "origin"], cwd=REPO, check=False)
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
        live = sorted(p for p in tmp.iterdir() if p.is_file() and p.name != ".clasp.json")
        print("public deployment serves version %d (%d files)" % (version, len(live)))
        differ = 0
        for f in live:
            on_main = main_source(f.name)
            if on_main is None:
                print("  %-22s live only (not on main)" % f.name)
                differ += 1
                continue
            a, b = norm(on_main), norm(f.read_text(encoding="utf-8", errors="replace"))
            if a == b:
                print("  %-22s MATCH" % f.name)
                continue
            changed = sum(1 for d in difflib.unified_diff(a, b, lineterm="", n=0)
                          if d[:1] in "+-" and not d.startswith(("+++", "---")))
            print("  %-22s DIFF (%d changed lines between main and live)" % (f.name, changed))
            differ += 1
    print("VERDICT: %s" % ("live == main" if not differ else "live != main in %d file(s)" % differ))
    return 1 if differ else 0


if __name__ == "__main__":
    raise SystemExit(main())
