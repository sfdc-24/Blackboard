#!/usr/bin/env python3
"""J3: recommend a receipt depth for a PR from its metadata only (title, body, file
list via `gh pr view --json title,body,files`; no diffs, no code content sent to Jev).

Jev yesno: does this change touch threading, timing, polling, locks or concurrency?
P(yes) >= 0.3 counts as yes (deliberately low - a missed concurrency PR costs far more
than an extra 15-run receipt on one that didn't need it) and recommends depth=deep:
repeat the neighbouring concurrency tests 15+ times on Linux and Windows. Anything else
is depth=normal.

If Jev errors, falls back to a deterministic keyword check over the same metadata
(thread/timing/poll/lock/concurrency/race/deadline/mutex/barrier) and says so in the
result - the fallback is logged, never silent.

--dry prints the PR metadata that would be sent and skips the Jev call.

USAGE
  python scripts/jev_pr_depth.py REPO PR_NUMBER [--dry]
  REPO is the short name (conference, Blackboard, sfdc24-site); sfdc-24/ is added.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jev_agent  # noqa: E402

CONC_Q = ("Does this pull request change concurrency, threading, timing, polling, locks "
          "or deadlines in running code (not only documentation or a text prompt)?")
YES_THRESHOLD = 0.3
DEEP_INSTRUCTIONS = "repeat the neighbouring concurrency tests 15+ times on Linux and Windows"
_KEYWORDS = re.compile(
    r"\b(thread|timing|poll|lock|concurren\w*|race|deadlock|mutex|barrier|async|lease)\b",
    re.IGNORECASE)


def pr_metadata(repo, number):
    """(dict, error) from `gh pr view` - title/body/file paths only, no diffs."""
    full_repo = repo if "/" in repo else "sfdc-24/%s" % repo
    done = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", full_repo,
         "--json", "title,body,files"],
        capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        return None, "gh pr view exit %d: %s" % (done.returncode, done.stderr.strip()[:200])
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError as error:
        return None, "gh pr view gave non-JSON: %s" % error
    return data, None


def metadata_text(data):
    files = ", ".join(f.get("path", "") for f in (data.get("files") or []))
    return "title: %s\nbody: %s\nfiles: %s" % (data.get("title") or "", data.get("body") or "", files)


def keyword_fallback(text):
    return bool(_KEYWORDS.search(text))


def recommend(data):
    """(result dict) for one PR's metadata. Tries Jev first; falls back to the keyword
    check on any Jev error, and says so in the result."""
    text = metadata_text(data)
    jev_p, jev_err = jev_agent.ask_yesno(text, CONC_Q)
    if jev_err is not None:
        fell_back = keyword_fallback(text)
        return {"source": "keyword_fallback", "jev_error": jev_err, "jev_p": None,
                "concurrency": fell_back,
                "depth": "deep" if fell_back else "normal",
                "instructions": DEEP_INSTRUCTIONS if fell_back else None}
    concurrency = jev_p >= YES_THRESHOLD
    return {"source": "jev", "jev_error": None, "jev_p": jev_p,
            "concurrency": concurrency,
            "depth": "deep" if concurrency else "normal",
            "instructions": DEEP_INSTRUCTIONS if concurrency else None}


def main():
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv
    if len(args) < 2:
        print(__doc__.split("USAGE")[1].strip())
        return 2
    repo, number = args[0], args[1]
    data, error = pr_metadata(repo, number)
    if error:
        print(json.dumps({"error": error}))
        return 2
    if dry:
        print(json.dumps({"dry": True, "metadata": data}, indent=2))
        return 0
    result = recommend(data)
    result["repo"] = repo
    result["pr"] = number
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
