"""Audit what an Apps Script web-app deployment is ACTUALLY configured to do.

VM-CICD-001. The manifest in the repo says what we *asked* for; the deployment
config says what Google *did*. Those are not the same thing, and on 2026-09-04
they disagreed: every STAGING deployment carried a manifest declaring
``ANYONE_ANONYMOUS`` while its /exec URL returned 403 to an anonymous caller.

For each deployment this prints the authoritative entry-point config from the
Apps Script API and then, separately, probes the live URL with no credentials.
Both are reported, because either one alone can lie:

  * the API config can say ANYONE_ANONYMOUS while the URL still 403s (the
    deploying user has not granted the script's OAuth scopes yet), and
  * a 200 from the URL says nothing about which version answered it.

Usage:
    python scripts/gas_deployment_audit.py                 # audit the estate
    python scripts/gas_deployment_audit.py <scriptId> ...  # audit specific ids
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# The STAGING estate (VM-CICD-001 STEP_2). Prod ids stay out of this file on
# purpose: this tool is used by the staging lane and must not invite a
# production mutation by making prod one typo away.
ESTATE = {
    "governor-page-api": "1S4LQE0SQwnE9kngxMhRngyAmSNGhFtDRxoYQqIxSSH0UoQ5u7T84MKU1",
    "blackboard-production": "1IxPEx_5gT7HGEMwOFSB7wcO6qPUTMLr32qAzDykGYJ5KdrxscQeoZC1K",
    "glasses-intake-uploader": "1ElMYYzbdCsKfnhN30Gxt47TUoSFZg7ixGNTEdZNJORolpsS9eXVaYWuI",
}

# Google serves its own chrome-wrapped notice page for a script that cannot run
# (unauthorized, not shared, no such deployment). It carries this productName.
# It can arrive with HTTP *200*, which is why status alone must never be read as
# "the app answered" — measured 2026-09-04, when a HEAD deployment returned 200
# with this page while the app itself was unreachable.
GOOGLE_NOTICE_PAGE = "26981ed0d57bbad37e728ff58134270c"

# Google intercepts an unreachable web app in more than one way, and BOTH can
# carry HTTP 200. Measured 2026-09-04 on this estate:
#   * the Drive/Script notice page (GOOGLE_NOTICE_PAGE above), and
#   * a full accounts.google.com sign-in page, which is what a HEAD deployment
#     serves to a caller it will not run for.
# Blacklisting the interstitials is deliberate: the estate's endpoints return
# both JSON (bus, glasses) and HTML (governor page API), so "looks like JSON"
# is not a usable test for whether the app ran.
GOOGLE_INTERSTITIALS = (
    GOOGLE_NOTICE_PAGE,
    "accounts.google.com/v3/signin",
    "AccountsSignInUi",
)


def app_answered(status, body):
    """True only when the Apps Script *app* produced the response.

    Status 200 is necessary and nowhere near sufficient. A known-good anonymous
    endpoint returns its own payload; an unrunnable one returns a Google
    interstitial, sometimes with a 200.
    """
    if status != 200 or not body:
        return False
    return not any(marker in body for marker in GOOGLE_INTERSTITIALS)


def token():
    d = json.load(open(os.path.expanduser("~/.clasprc.json")))["tokens"]["default"]
    body = urllib.parse.urlencode({
        "client_id": d["client_id"], "client_secret": d["client_secret"],
        "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]


def api(path, tok):
    req = urllib.request.Request(
        "https://script.googleapis.com/v1/" + path,
        headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_anonymous(url):
    """GET the /exec URL with no credentials, following the 302 with a BARE GET.

    Apps Script answers /exec with a 302 to googleusercontent.com. urllib's
    automatic handler mangles that into a 404, so the redirect is followed by
    hand (the same quirk gas_version_assert.py encodes).
    """
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=45) as r:
            return r.status, r.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307):
            try:
                with urllib.request.urlopen(e.headers["Location"], timeout=45) as r2:
                    return r2.status, r2.read(400).decode("utf-8", "replace")
            except urllib.error.HTTPError as e2:
                return e2.code, e2.read(300).decode("utf-8", "replace")
        return e.code, e.read(300).decode("utf-8", "replace")
    except Exception as exc:  # network-level failure is a finding too
        return None, f"{type(exc).__name__}: {exc}"


def audit(name, script_id, tok):
    print(f"\n=== {name}\n    scriptId {script_id}")
    try:
        deployments = api(f"projects/{script_id}/deployments", tok).get("deployments", [])
    except urllib.error.HTTPError as e:
        print(f"    API ERROR {e.code}: {e.read(200).decode('utf-8', 'replace')}")
        return []
    findings = []
    for dep in deployments:
        dc = dep.get("deploymentConfig", {})
        dep_id = dep.get("deploymentId", "")
        version = dc.get("versionNumber", "HEAD")
        desc = dc.get("description", "")
        entries = dep.get("entryPoints", []) or []
        web = [e for e in entries if e.get("entryPointType") == "WEB_APP"]
        if not web:
            print(f"    - {dep_id[:28]}… @{version} — no WEB_APP entry point ({desc})")
            continue
        for e in web:
            cfg = e["webApp"].get("entryPointConfig", {})
            access, execute_as = cfg.get("access"), cfg.get("executeAs")
            url = e["webApp"].get("url", "")
            print(f"    - {dep_id[:28]}… @{version}  access={access}  executeAs={execute_as}")
            print(f"      {desc}")
            status, body = probe_anonymous(url)
            anon_ok = app_answered(status, body)
            intercepted = any(m in (body or "") for m in GOOGLE_INTERSTITIALS)
            verdict = "app answered" if anon_ok else (
                "Google interstitial, NOT the app" if intercepted else "NOT ANONYMOUS-READY")
            print(f"      anonymous GET -> {status} — {verdict}")
            if not anon_ok:
                print(f"      body: {' '.join((body or '').split())[:150]}")
            findings.append({
                "project": name, "deploymentId": dep_id, "version": version,
                "access": access, "executeAs": execute_as, "url": url,
                "anonymous_http": status, "anonymous_ok": anon_ok,
                "config_claims_anonymous": access == "ANYONE_ANONYMOUS",
            })
    return findings


def main():
    tok = token()
    targets = (list(ESTATE.items()) if len(sys.argv) == 1
               else [(f"arg{i}", a) for i, a in enumerate(sys.argv[1:], 1)])
    all_findings = []
    for name, script_id in targets:
        all_findings.extend(audit(name, script_id, tok))

    print("\n" + "=" * 72)
    lying = [f for f in all_findings if f["config_claims_anonymous"] and not f["anonymous_ok"]]
    ready = [f for f in all_findings if f["anonymous_ok"]]
    print(f"web-app deployments audited      : {len(all_findings)}")
    print(f"anonymous-ready (the APP replied): {len(ready)}")
    print(f"config claims ANYONE_ANONYMOUS but the app does not answer: {len(lying)}")
    for f in lying:
        print(f"  ! {f['project']} @{f['version']} -> HTTP {f['anonymous_http']}")
    if lying:
        print("\nThis is the gap the version assertion cannot see. A deploy that")
        print("cannot be reached anonymously cannot be verified from outside, so")
        print("the pipeline would be asserting against a door only it can open.")
    return 1 if lying else 0


if __name__ == "__main__":
    sys.exit(main())
