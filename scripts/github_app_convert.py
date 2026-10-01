"""Finish a GitHub App manifest registration: trade the one-hour code for the App, and put its private key straight
into Secret Manager without printing it. Mr. Salam runs this himself, in his own terminal, right after he clicks
"Create GitHub App" (docs/github-apps/README.md, step 2):

    python scripts/github_app_convert.py sfdc24-cloud-clone
    python scripts/github_app_convert.py sfdc24-ccc-broker

It then asks for the code at a prompt that does not show it, or reads one line from stdin when stdin is not a
terminal. The code never goes on a command line: it is a one-time credential that can be traded for the App's
private key, and a command line lands in shell history and in the process list (Copilot on #309).

What it does:
1. POSTs /app-manifests/<code>/conversions to api.github.com itself, unauthenticated, as GitHub's manifest flow
   expects. No `gh` login is needed (Codex on #309), no child process sees the code, a redirect is refused rather than
   followed, and the request times out after 30 seconds. The response is held in memory only.
2. Refuses unless the App's slug is the one named and its permissions are exactly the manifest's.
3. Writes the private key (pem) to a NEW Secret Manager secret through `gcloud`'s stdin. It never goes on disk, in
   argv, or to stdout.
4. Prints only the App id, slug, permissions and the secret's name, then the install link.

It never prints the code, the pem, the webhook secret, the client secret or the raw response, including on errors: an
error says which step failed and the HTTP status, nothing more (memory: a key-load error once echoed part of a fresh
key).
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = "sfdc24"
MANIFESTS = Path(__file__).resolve().parent.parent / "docs" / "github-apps"
SECRET_FOR = {"sfdc24-cloud-clone": "GITHUB_APP_READONLY_PRIVATE_KEY", "sfdc24-ccc-broker": "GITHUB_APP_BROKER_PRIVATE_KEY"}
CODE = re.compile(r"^[0-9a-f]{20,64}$")
CONVERSIONS = "https://api.github.com/app-manifests/%s/conversions"
TIMEOUT = 30


class Stop(Exception):
    """A refusal: the message is safe to print, and never holds a credential."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise Stop("GitHub answered with a redirect (HTTP %d), which is not followed: nothing stored" % code)


def opener_for(*handlers):
    """The only opener the conversion uses: urllib's own, with redirects refused. Tests add a fake GitHub here."""
    return urllib.request.build_opener(_NoRedirect, *handlers)


def expected(slug: str) -> dict:
    manifest = json.loads((MANIFESTS / ("%s.manifest.json" % slug)).read_text(encoding="utf-8"))
    return dict(manifest["default_permissions"])


def read_code() -> str:
    """The code, from a prompt that does not echo it, or one line of stdin when stdin is not a terminal."""
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass("Paste the code from the address bar (it is not shown), then press Enter: ")
    return sys.stdin.readline() if sys.stdin is not None else ""


def convert(code: str, opener=None) -> dict:
    if not CODE.match(code):                                            # the URL is built from a checked code only
        raise Stop("that is not a manifest code: nothing stored")
    opener = opener or opener_for()
    request = urllib.request.Request(CONVERSIONS % code, data=b"", method="POST", headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sfdc24-github-app-convert"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        error.close()                                                   # its body is never read
        raise Stop("conversion failed (HTTP %d); the code may have expired (one hour) or been used: nothing stored"
                   % error.code) from None
    except (urllib.error.URLError, OSError) as error:
        raise Stop("conversion got no answer from GitHub (%s): nothing stored. If GitHub did convert, the code is "
                   "spent and the key was never shown; generate a new private key on the App's settings page"
                   % type(error).__name__) from None
    try:
        return json.loads(body)
    except ValueError:
        raise Stop("conversion answered with something that is not JSON: nothing stored") from None


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
    command = gcloud_cmd() + ["secrets", "create", secret, "--project", PROJECT, "--replication-policy", "automatic",
                              "--data-file=-"]
    try:
        out = run(command, input=pem, capture_output=True, text=True)
    except OSError as error:
        raise Stop("gcloud could not start (%s): nothing stored" % type(error).__name__) from None
    if out.returncode != 0:
        # Once the create has started, a failure is not proof that nothing was stored: a lost answer can follow an
        # accepted create (Copilot on #309). So the owner reads the secret back before retrying or deleting the App.
        raise Stop("gcloud secrets create %s failed (exit %d). It may still have stored the key, and an existing "
                   "secret is never overwritten. Before you retry or delete the App, check what is there: gcloud "
                   "secrets versions list %s --project %s" % (secret, out.returncode, secret, PROJECT))


def main(argv, run=subprocess.run, say=print, ask=read_code, opener=None) -> int:
    if len(argv) != 1 or argv[0] not in SECRET_FOR:
        say("usage: python scripts/github_app_convert.py <%s>, then paste the code at the prompt"
            % "|".join(SECRET_FOR))
        if any(CODE.match(a) for a in argv):
            say("The code must not go on the command line: that one is now in your shell history. Nothing was sent. "
                "Run the command again with the App name only, and paste the code at the prompt.")
        return 2
    slug = argv[0]
    code = (ask() or "").strip()
    if not CODE.match(code):
        say("STOPPED: that is not a manifest code (the value after code= in the address bar): nothing sent")
        return 2
    try:
        app = convert(code, opener)
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
