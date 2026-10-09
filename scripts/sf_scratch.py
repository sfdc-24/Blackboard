"""Scratch orgs for conference prototypes: provision, record, snapshot, delete.

Mr. Salam (2026-10-09): "scratch orgs can be used to build prototypes during
conferences and can be put into developer org upon payment/acceptance". The Dev
Hub is his OmniStudio developer org (00Dbm00000wK2ibEAC, CLI alias sfdc24web).
It has Org Shape and Scratch Org Snapshots on.

SHAPE, THEN SNAPSHOT, THEN ONE ORG PER SESSION:
  - The scratch definition is the dev org's SHAPE ("sourceOrg": the dev org id),
    so a prototype has the same features, settings and licences (OmniStudio
    included) as where it will be promoted. No hand-picked feature list.
  - For speed during a call, a scratch org is created from the "ConfBase"
    SNAPSHOT when it is Active (a shaped org plus the minimal baseline, already
    set up), and falls back to the shape otherwise. Create the snapshot ahead of
    time: it takes minutes; a call should never wait for setup.
  - LIMITS (this hub, read from /limits): 3 ACTIVE scratch orgs, 6 created PER DAY,
    5 active snapshots, 5 snapshots per day. Every org created from the snapshot
    still counts against the 6 a day. So: reuse the session's org if it is still
    active, keep orgs short-lived (DEFAULT_DAYS), delete them after the call, and
    refuse to create when either remaining count is 0.

    python scripts/sf_scratch.py limits
    python scripts/sf_scratch.py create --alias conf-acme [--days 7] [--prefer shape]
    python scripts/sf_scratch.py record --alias conf-acme      # {org_id, alias, instance_url, expires}; no token
    python scripts/sf_scratch.py attach --alias conf-acme --session <id> --url <controller>   # STUDIO_SESSION_TOKEN
    python scripts/sf_scratch.py snapshot --source conf-base --name ConfBase
    python scripts/sf_scratch.py snapshot-status --name ConfBase
    python scripts/sf_scratch.py delete --alias conf-acme

The sf CLI does the org work (laptop path). No token is printed by anything here.
On Cloud Run the same steps go through the JWT bearer flow (see the PR).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HUB_ALIAS = os.environ.get("SF_DEV_HUB_ALIAS", "sfdc24web")
HUB_ORG_ID = "00Dbm00000wK2ibEAC"
SNAPSHOT_NAME = "ConfBase"
DEFAULT_DAYS = 7
MAX_DAYS = 30
ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}")
SNAPSHOT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,14}")
ORG_NAME = "SFDC24 conference prototype"


class Refused(RuntimeError):
    pass


def sf_bin() -> str:
    return os.environ.get("SF_CLI") or shutil.which("sf") or "sf"


def _sf_cli_module():
    """The ONE sf CLI wrapper (cloud/studio-controller/workers/sf_cli.py), shared, not copied."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cloud", "studio-controller",
                        "workers", "sf_cli.py")
    spec = importlib.util.spec_from_file_location("studio_sf_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SF_CLI = _sf_cli_module()


def run_sf(args: list, runner=subprocess.run, cwd=None, timeout=900):
    """One sf CLI call through the shared wrapper; Refused with the CLI's own message."""
    try:
        return SF_CLI.SfCli(runner=runner, binary=sf_bin()).run(*args, cwd=cwd, timeout=timeout)
    except SF_CLI.SalesforceError as exc:
        raise Refused(str(exc)) from None


def query_hub(soql: str, runner=subprocess.run) -> list:
    return list((run_sf(["data", "query", "--query", soql, "--target-org", HUB_ALIAS], runner) or {})
                .get("records") or [])


def limits(runner=subprocess.run) -> dict:
    rows = run_sf(["org", "list", "limits", "--target-org", HUB_ALIAS], runner) or []
    pick = {}
    for row in rows:
        if row.get("name") in ("ActiveScratchOrgs", "DailyScratchOrgs", "ActiveOrgSnapshots", "DailyOrgSnapshots"):
            pick[row["name"]] = {"max": int(row.get("max") or 0), "remaining": int(row.get("remaining") or 0)}
    return pick


def plan_capacity(lims: dict, reusable: bool) -> dict:
    """Whether a new scratch org may be created now. Reuse wins; then both counts must allow it."""
    active = lims.get("ActiveScratchOrgs") or {}
    daily = lims.get("DailyScratchOrgs") or {}
    if reusable:
        return {"create": False, "reuse": True, "reason": "the session's scratch org is still active"}
    if int(active.get("remaining", 0)) <= 0:
        return {"create": False, "reuse": False,
                "reason": "all %d active scratch orgs are in use; delete one first" % active.get("max", 3)}
    if int(daily.get("remaining", 0)) <= 0:
        return {"create": False, "reuse": False,
                "reason": "today's %d scratch org creations are used up; try after 00:00 UTC" % daily.get("max", 6)}
    note = ""
    if int(daily["remaining"]) <= 1:
        note = "this is the last scratch org that can be created today"
    return {"create": True, "reuse": False, "reason": note,
            "after": {"active_remaining": int(active["remaining"]) - 1, "daily_remaining": int(daily["remaining"]) - 1}}


def snapshot_status(name: str = SNAPSHOT_NAME, runner=subprocess.run) -> str:
    if not SNAPSHOT_RE.fullmatch(name):
        raise Refused("not a snapshot name")
    rows = query_hub("SELECT Id, Status FROM OrgSnapshot WHERE SnapshotName = '%s' "
                     "ORDER BY CreatedDate DESC LIMIT 1" % name, runner)
    return str(rows[0].get("Status")) if rows else "Missing"


DEF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cloud", "studio-controller", "sf",
                        "project-scratch-def.json")


def scratch_definition(use_snapshot: bool, snapshot: str = SNAPSHOT_NAME) -> dict:
    """The dev org's shape (cloud/studio-controller/sf/project-scratch-def.json: "sourceOrg"), or the
    ConfBase snapshot built from it. Never a hand-picked feature list."""
    with open(DEF_FILE, encoding="utf-8") as fh:
        shape = json.load(fh)
    if shape.get("sourceOrg") != HUB_ORG_ID or "features" in shape or "edition" in shape:
        raise Refused("project-scratch-def.json must be the shape of %s and nothing else" % HUB_ORG_ID)
    if use_snapshot:
        return {"orgName": shape["orgName"], "snapshot": snapshot}
    return dict(shape)


def active_org(alias: str, runner=subprocess.run) -> dict | None:
    try:
        rec = record(alias, runner)
    except Refused:
        return None
    # ActiveScratchOrg holds a row only while the org is active (it has no Status column).
    rows = query_hub("SELECT Id, ExpirationDate FROM ActiveScratchOrg WHERE ScratchOrg = '%s' LIMIT 1"
                     % rec["org_id"][:15], runner)
    return rec if rows else None


def record(alias: str, runner=subprocess.run) -> dict:
    """{org_id, alias, instance_url, expires} for a scratch org the CLI knows. Never the token."""
    if not ALIAS_RE.fullmatch(alias or ""):
        raise Refused("not an alias")
    out = run_sf(["org", "display", "--target-org", alias], runner, timeout=120) or {}
    if not out.get("id"):
        raise Refused("the CLI does not know %s" % alias)
    return {"org_id": out["id"], "alias": alias, "instance_url": out.get("instanceUrl", ""),
            "expires": out.get("expirationDate", "")}


def create(alias: str, days: int = DEFAULT_DAYS, prefer: str = "snapshot", runner=subprocess.run,
           clock=time.monotonic) -> dict:
    if not ALIAS_RE.fullmatch(alias or ""):
        raise Refused("not an alias")
    if not 1 <= int(days) <= MAX_DAYS:
        raise Refused("days must be 1 to %d" % MAX_DAYS)
    existing = active_org(alias, runner)
    plan = plan_capacity(limits(runner), existing is not None)
    if plan["reuse"]:
        return dict(existing, reused=True, source="existing", seconds=0)
    if not plan["create"]:
        raise Refused(plan["reason"])
    use_snapshot = prefer == "snapshot" and snapshot_status(runner=runner) == "Active"
    definition = scratch_definition(use_snapshot)
    # The CLI can keep a handle on the project folder after it exits (Windows): never fail on cleanup.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as project:
        with open(os.path.join(project, "sfdx-project.json"), "w", encoding="utf-8", newline="") as fh:
            json.dump({"packageDirectories": [{"path": "force-app", "default": True}],
                       "sourceApiVersion": "62.0"}, fh)
        os.makedirs(os.path.join(project, "force-app"), exist_ok=True)
        def_path = os.path.join(project, "project-scratch-def.json")
        with open(def_path, "w", encoding="utf-8", newline="") as fh:
            json.dump(definition, fh)
        started = clock()
        run_sf(["org", "create", "scratch", "--definition-file", def_path, "--target-dev-hub", HUB_ALIAS,
                "--alias", alias, "--duration-days", str(int(days)), "--wait", "30"], runner, cwd=project,
               timeout=2400)
        seconds = int(clock() - started)
    rec = record(alias, runner)
    return dict(rec, reused=False, source="snapshot " + SNAPSHOT_NAME if use_snapshot else "shape of " + HUB_ORG_ID,
                seconds=seconds, note=plan["reason"])


def delete(alias: str, runner=subprocess.run) -> dict:
    if not ALIAS_RE.fullmatch(alias or "") or alias == HUB_ALIAS:
        raise Refused("refusing to delete %r" % alias)
    run_sf(["org", "delete", "scratch", "--target-org", alias, "--no-prompt"], runner, timeout=600)
    return {"deleted": alias}


def snapshot(source_alias: str, name: str = SNAPSHOT_NAME, runner=subprocess.run) -> dict:
    """An OrgSnapshot of a shaped, baselined scratch org (created in the hub through the data API)."""
    if not SNAPSHOT_RE.fullmatch(name):
        raise Refused("not a snapshot name")
    rec = record(source_alias, runner)
    out = run_sf(["data", "create", "record", "--sobject", "OrgSnapshot", "--target-org", HUB_ALIAS, "--values",
                  "SnapshotName='%s' SourceOrg='%s' Content='MetadataData' Description='SFDC24 conference base'"
                  % (name, rec["org_id"][:15])], runner, timeout=300) or {}
    return {"snapshot": name, "id": out.get("id"), "source": source_alias}


def attach(alias: str, session_id: str, url: str, token: str, opener=urllib.request.urlopen) -> dict:
    """Record the scratch org in the controller's session (the lane re-checks it with the hub)."""
    rec = record(alias)
    body = json.dumps({"action": "attach_scratch", "org_id": rec["org_id"], "alias": alias}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/session/%s/sf-build" % session_id, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                          "Origin": "https://www.sfdc24.com"})
    with opener(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("limits")
    c = sub.add_parser("create")
    c.add_argument("--alias", required=True)
    c.add_argument("--days", type=int, default=DEFAULT_DAYS)
    c.add_argument("--prefer", choices=("snapshot", "shape"), default="snapshot")
    for name in ("record", "delete"):
        sub.add_parser(name).add_argument("--alias", required=True)
    a = sub.add_parser("attach")
    a.add_argument("--alias", required=True)
    a.add_argument("--session", required=True)
    a.add_argument("--url", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--source", required=True)
    s.add_argument("--name", default=SNAPSHOT_NAME)
    sub.add_parser("snapshot-status").add_argument("--name", default=SNAPSHOT_NAME)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "limits":
            out = limits()
        elif args.cmd == "create":
            out = create(args.alias, args.days, args.prefer)
        elif args.cmd == "record":
            out = record(args.alias)
        elif args.cmd == "delete":
            out = delete(args.alias)
        elif args.cmd == "attach":
            token = os.environ.get("STUDIO_SESSION_TOKEN", "")
            if not token:
                raise Refused("set STUDIO_SESSION_TOKEN to the session's token")
            out = attach(args.alias, args.session, args.url, token)
        elif args.cmd == "snapshot":
            out = snapshot(args.source, args.name)
        else:
            out = {"snapshot": args.name, "status": snapshot_status(args.name)}
    except Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
