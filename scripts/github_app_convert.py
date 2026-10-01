"""Finish a GitHub App manifest registration: trade the one-hour code for the App, and put its private key straight
into Secret Manager without printing it. Mr. Salam runs this himself, in his own terminal, right after he clicks
"Create GitHub App" (docs/github-apps/README.md, step 2):

    python scripts/github_app_convert.py <code> sfdc24-cloud-clone
    python scripts/github_app_convert.py <code> sfdc24-ccc-broker

What it does:
1. POST /app-manifests/<code>/conversions through `gh api`, held in memory only.
2. Refuses unless the App's slug is the one named and its permissions are exactly the manifest's.
3. Writes the private key (pem) to a NEW Secret Manager secret through `gcloud`'s stdin. It never goes on disk, in
   argv, or to stdout.
4. Prints only the App id, slug, permissions and the secret's name, then the install link.

It never prints the pem, the webhook secret, the client secret or the raw response, including on errors: an error
says which step failed and the HTTP status, nothing more (memory: a key-load error once echoed part of a fresh key).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = "sfdc24"
MANIFESTS = Path(__file__).resolve().parent.parent / "docs" / "github-apps"
SECRET_FOR = {"sfdc24-cloud-clone": "GH_APP_CLONE_KEY", "sfdc24-ccc-broker": "GH_APP_BROKER_KEY"}
CODE = re.compile(r"^[0-9a-f]{20,64}$")


class Stop(Exception):
    """A refusal: the message is safe to print, and never holds a credential."""


def expected(slug: str) -> dict:
    manifest = json.loads((MANIFESTS / ("%s.manifest.json" % slug)).read_text(encoding="utf-8"))
    return dict(manifest["default_permissions"])


def convert(code: str, run=subprocess.run) -> dict:
    try:
        out = run(["gh", "api", "--method", "POST", "/app-manifests/%s/conversions" % code],
                  capture_output=True, text=True)
    except OSError as error:
        raise Stop("gh could not start (%s): nothing stored" % type(error).__name__) from None
    if out.returncode != 0:
        status = re.search(r"HTTP (\d{3})", out.stderr or "")
        raise Stop("conversion failed (HTTP %s); the code may have expired (one hour) or been used"
                   % (status.group(1) if status else "?"))
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise Stop("conversion answered with something that is not JSON") from None


def check(app: dict, slug: str) -> None:
    if app.get("slug") != slug:
        raise Stop("the new App's slug is %r, not %r: nothing stored" % (app.get("slug"), slug))
    want, got = expected(slug), dict(app.get("permissions") or {})
    if got != want:
        raise Stop("the new App's permissions %s are not the manifest's %s: nothing stored" % (got, want))
    if not str(app.get("pem") or "").startswith("-----BEGIN"):
        raise Stop("the response has no private key: nothing stored")


def gcloud_cmd() -> list:
    """gcloud as this laptop can run it. It is not on PATH here, so the SDK's bundled Python and gcloud.py are the
    fallback, as the fleet's other gcloud callers do."""
    for name in ("gcloud", "gcloud.cmd"):
        found = shutil.which(name)
        if found:
            return [found]
    sdk = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Cloud SDK" / "google-cloud-sdk"
    python, entry = sdk / "platform" / "bundledpython" / "python.exe", sdk / "lib" / "gcloud.py"
    if python.exists() and entry.exists():
        return [str(python), str(entry)]
    raise Stop("gcloud was not found (not on PATH, and no Cloud SDK under LOCALAPPDATA): nothing stored")


def store(secret: str, pem: str, run=subprocess.run) -> None:
    try:
        out = run(gcloud_cmd() + ["secrets", "create", secret, "--project", PROJECT, "--replication-policy",
                                  "automatic", "--data-file=-"], input=pem, capture_output=True, text=True)
    except OSError as error:
        raise Stop("gcloud could not start (%s): nothing stored" % type(error).__name__) from None
    if out.returncode != 0:
        raise Stop("gcloud secrets create %s failed (exit %d); if it already exists, nothing was overwritten"
                   % (secret, out.returncode))


def main(argv, run=subprocess.run, say=print) -> int:
    if len(argv) != 2 or argv[1] not in SECRET_FOR or not CODE.match(argv[0]):
        say("usage: python scripts/github_app_convert.py <code from the address bar> <%s>" % "|".join(SECRET_FOR))
        return 2
    code, slug = argv
    try:
        app = convert(code, run)
        check(app, slug)
        store(SECRET_FOR[slug], app["pem"], run)
    except Stop as stop:
        say("STOPPED: %s" % stop)
        return 1
    say("OK: App %s (id %s), permissions %s; private key stored as Secret Manager %s/%s (value not shown)."
        % (app["slug"], app.get("id"), json.dumps(app.get("permissions"), sort_keys=True), PROJECT,
           SECRET_FOR[slug]))
    say("Next: install it on sfdc-24/Blackboard and sfdc-24/conference only: https://github.com/apps/%s/installations/new"
        % app["slug"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
