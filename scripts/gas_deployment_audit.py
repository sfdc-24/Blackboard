"""Audit what an Apps Script web-app deployment is ACTUALLY configured to do.

VM-CICD-001. The manifest in the repo says what we *asked* for; the deployment
config says what Google *did*. Those are not the same thing, and on 2026-09-04
they disagreed: every STAGING deployment carried a manifest declaring
``ANYONE_ANONYMOUS`` while its /exec URL returned 403 to an anonymous caller.

For each deployment this prints the authoritative entry-point config from the
Apps Script API and then, separately, probes the live URL with no credentials.
Both are reported, because either one alone can lie:

  * the API config can say ANYONE_ANONYMOUS while the URL still 403s (the
    cause needs separate authorization/deployment evidence), and
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
# These markers explain failures only. Their absence cannot prove readiness;
# the positive health contract below is the actual acceptance condition.
GOOGLE_INTERSTITIALS = (
    GOOGLE_NOTICE_PAGE,
    "accounts.google.com/v3/signin",
    "AccountsSignInUi",
)


# Only the glasses app currently has a reviewed machine-readable health
# signature. Other projects stay unverified until their owners add one.
# This is application identity evidence, not a source/build hash assertion.
EXPECTED_SERVICES = {"glasses-intake-uploader": "sfdc24-glasses-uploader"}
MAX_RESPONSE_BYTES = 256 * 1024


def app_answered(status, body, expected_service=None):
    """Require a positive JSON health signature; unknown HTML never proves it."""
    if status != 200 or not body or not expected_service:
        return False
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return (isinstance(data, dict) and data.get("ok") is True
            and data.get("service") == expected_service
            and data.get("error") in (None, ""))


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


def read_response(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeds audit size limit")
    return response.status, body.decode("utf-8", "replace")


def probe_anonymous(url):
    """GET the /exec URL with no credentials, following the 302 with a BARE GET.

    Apps Script answers /exec with a 302 to googleusercontent.com. urllib's
    automatic handler mangles that into a 404, so the redirect is followed by
    hand (the same quirk gas_version_assert.py encodes).
    """
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=45) as r:
            return read_response(r)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            try:
                with urllib.request.urlopen(e.headers["Location"], timeout=45) as r2:
                    return read_response(r2)
            except urllib.error.HTTPError as e2:
                return e2.code, "HTTP error"
            except Exception as exc:
                return None, f"redirect fetch failed ({type(exc).__name__})"
        return e.code, "HTTP error"
    except Exception as exc:  # network-level failure is a finding too
        return None, f"fetch failed ({type(exc).__name__})"


def list_deployments(script_id, tok):
    """Read every page; incomplete or malformed inventory fails the project."""
    deployments, seen_tokens = [], set()
    page_token = None
    for _ in range(100):
        path = f"projects/{script_id}/deployments"
        if page_token:
            path += "?pageToken=" + urllib.parse.quote(page_token, safe="")
        page = api(path, tok)
        if (not isinstance(page, dict) or page.get("error")
                or not isinstance(page.get("deployments", []), list)):
            raise ValueError("invalid deployment inventory")
        deployments.extend(page.get("deployments", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            return deployments
        if not isinstance(page_token, str) or page_token in seen_tokens:
            raise ValueError("invalid or repeated inventory page token")
        seen_tokens.add(page_token)
    raise ValueError("deployment inventory exceeded page limit")


def audit(name, script_id, tok):
    print(f"\n=== {name}\n    scriptId {script_id}")
    try:
        deployments = list_deployments(script_id, tok)
    except Exception as exc:
        print(f"    INVENTORY FAILED ({type(exc).__name__})")
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
            if version == "HEAD" or urllib.parse.urlsplit(url).path.endswith("/dev"):
                print("      development endpoint: editor-only, excluded from anonymous readiness")
                continue
            if not dep_id or isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError("invalid versioned deployment identity")
            print(f"    - {dep_id[:28]}… @{version}  access={access}  executeAs={execute_as}")
            print(f"      {desc}")
            status, body = probe_anonymous(url)
            expected_service = EXPECTED_SERVICES.get(name)
            anon_ok = app_answered(status, body, expected_service)
            intercepted = any(m in (body or "") for m in GOOGLE_INTERSTITIALS)
            verdict = "expected health signature matched (build unverified)" if anon_ok else (
                "Google interstitial, NOT the app" if intercepted else "NOT ANONYMOUS-READY")
            print(f"      anonymous GET -> {status} — {verdict}")
            if not expected_service:
                print("      missing reviewed JSON health contract for this project")
            findings.append({
                "project": name, "deploymentId": dep_id, "version": version,
                "access": access, "executeAs": execute_as, "url": url,
                "anonymous_http": status, "anonymous_ok": anon_ok,
                "config_claims_anonymous": access == "ANYONE_ANONYMOUS",
                "ready": anon_ok and access == "ANYONE_ANONYMOUS" and execute_as == "USER_DEPLOYING",
            })
    return findings


def main():
    try:
        tok = token()
    except Exception as exc:
        print(f"::error::credential setup failed ({type(exc).__name__})")
        return 1
    targets = (list(ESTATE.items()) if len(sys.argv) == 1
               else [(next((n for n, s in ESTATE.items() if s == a), f"arg{i}"), a)
                     for i, a in enumerate(sys.argv[1:], 1)])
    all_findings = []
    missing = []
    for name, script_id in targets:
        try:
            findings = audit(name, script_id, tok)
        except Exception as exc:
            print(f"    PROJECT AUDIT FAILED ({type(exc).__name__})")
            findings = []
        if not findings:
            missing.append(name)
        all_findings.extend(findings)

    print("\n" + "=" * 72)
    lying = [f for f in all_findings if f["config_claims_anonymous"] and not f["anonymous_ok"]]
    ready = [f for f in all_findings if f["ready"]]
    unverified = [f for f in all_findings if not f["ready"]]
    print(f"web-app deployments audited      : {len(all_findings)}")
    print(f"configured anonymous targets with expected health signature: {len(ready)}")
    print(f"missing/incomplete project inventories: {len(missing)}")
    for name in missing:
        print(f"  ! {name}: no complete versioned web-app inventory")
    print(f"config claims ANYONE_ANONYMOUS but the app does not answer: {len(lying)}")
    for f in lying:
        print(f"  ! {f['project']} @{f['version']} -> HTTP {f['anonymous_http']}")
    if lying:
        print("\nThis is the gap the version assertion cannot see. A deploy that")
        print("cannot be reached anonymously cannot be verified from outside, so")
        print("the pipeline would be asserting against a door only it can open.")
    return 1 if missing or unverified or not all_findings else 0


if __name__ == "__main__":
    sys.exit(main())
