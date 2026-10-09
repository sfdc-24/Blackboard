"""The Salesforce orgs the conference build lane may touch: TARGETS, and nothing else.

Two kinds of target (Mr. Salam, 2026-10-09: "scratch orgs can be used to build
prototypes during conferences and can be put into developer org upon
payment/acceptance"):

  scratch  THE DEFAULT for every conference build. Accepted only when the Dev Hub
           (the pinned developer org) confirms it: an ActiveScratchOrg row in the hub
           whose ScratchOrg is that org's id and whose expiration has not passed.
  devorg   the owner's OmniStudio developer org, 00Dbm00000wK2ibEAC, on its own My
           Domain - used ONLY to PROMOTE a tested prototype. It is also the Dev Hub.

Everything goes through the sf CLI (workers/sf_cli.py) with the CLI's own auth: no
REST client, no token handling here. IDENTITY BEFORE ANYTHING: every client first
asks the CLI which org its alias is (`sf org display`) and refuses unless it is the
expected org on the expected host - and, for a scratch org, unless the hub vouches
for it - before one describe, query, import or deploy.

The surface: describe(object), the custom objects, a query the lane builds from
names the org returned, a sample-data import (`sf data import tree`), one permission
set assignment, and `sf project deploy start --metadata-dir` (dry-run or not, async)
with `sf project deploy report`. A metadata delete is only the stored
destructiveChanges.xml of a build this lane made (undo).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import tempfile
import time

try:  # the app and the image import this module as part of the workers package
    from workers.sf_cli import SalesforceError, SfCli
except ImportError:  # loaded from its file (tests)
    import importlib.util as _util
    _spec = _util.spec_from_file_location(
        "studio_sf_cli", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sf_cli.py"))
    _cli = _util.module_from_spec(_spec)
    _spec.loader.exec_module(_cli)
    SalesforceError, SfCli = _cli.SalesforceError, _cli.SfCli

PINNED_ORG_ID = "00Dbm00000wK2ibEAC"
PINNED_HOST = "dbm00000wk2ibeac-dev-ed.develop.my.salesforce.com"
HUB_ALIAS = os.environ.get("SF_DEV_HUB_ALIAS") or "sfdc24web"
SCRATCH_SUFFIX = ".scratch.my.salesforce.com"
ORG_ID_RE = re.compile(r"00D[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?")
SOBJECT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,42}")
JOB_ID_RE = re.compile(r"0Af[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?")
RECORD_ID_RE = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}")
DONE = ("Succeeded", "SucceededPartial", "Failed", "Canceled")


class OrgMismatch(PermissionError):
    """The org that answered is not the target. Nothing else was called."""


def lightning_host(my_domain_host: str) -> str:
    """x.develop.my.salesforce.com -> x.develop.lightning.force.com (and the scratch equivalent)."""
    if not my_domain_host.endswith(".my.salesforce.com"):
        raise ValueError("not a My Domain host")
    return my_domain_host[: -len(".my.salesforce.com")] + ".lightning.force.com"


def lightning_links(api_name: str, record_id: str = "", host: str = PINNED_HOST) -> dict:
    base = "https://" + lightning_host(host)
    links = {"object_manager": "%s/lightning/setup/ObjectManager/%s/Details/view" % (base, api_name),
             "list": "%s/lightning/o/%s/list?filterName=All" % (base, api_name)}
    if record_id and RECORD_ID_RE.fullmatch(record_id):
        links["record"] = "%s/lightning/r/%s/%s/view" % (base, api_name, record_id)
    return links


def _host(url: str) -> str:
    return re.sub(r"^https://", "", str(url or "")).split("/")[0].lower()


class CliOrg:
    """One org, reached by its sf CLI alias, after its identity is proven."""

    kind = ""

    def __init__(self, alias: str, expected_org_id: str, cli: SfCli | None = None, *, clock=time.monotonic,
                 sleep=time.sleep):
        if not ALIAS_RE.fullmatch(alias or "") or not ORG_ID_RE.fullmatch(expected_org_id or ""):
            raise OrgMismatch("a target needs its CLI alias and org id")
        self.alias, self.org_id = alias, expected_org_id
        self.cli = cli or SfCli()
        self._clock, self._sleep = clock, sleep
        self._verified = False
        self.host = ""

    def _sf(self, *args, **kw):
        if not self._verified:
            self.verify()
        return self.cli.run(*args, "--target-org", self.alias, **kw)

    def _check_host(self, host: str) -> None:
        raise NotImplementedError

    def _before_identity(self) -> None:
        pass

    def verify(self) -> dict:
        if self._verified:
            return {"org_id": self.org_id}
        self._before_identity()
        shown = self.cli.run("org", "display", "--target-org", self.alias) or {}
        if str(shown.get("id") or "")[:15] != self.org_id[:15]:
            raise OrgMismatch("CLI alias %s is not organization %s" % (self.alias, self.org_id[:15]))
        host = _host(shown.get("instanceUrl"))
        self._check_host(host)
        self.host = host
        self._verified = True
        return {"org_id": self.org_id}

    def links(self, api_name: str, record_id: str = "") -> dict:
        return lightning_links(api_name, record_id, self.host)

    # -- reads -------------------------------------------------------------
    def describe(self, sobject: str) -> dict:
        if not isinstance(sobject, str) or not SOBJECT_RE.fullmatch(sobject):
            raise ValueError("not an object API name")
        return self._sf("sobject", "describe", "--sobject", sobject) or {}

    def custom_objects(self) -> list:
        return sorted(str(n) for n in (self._sf("sobject", "list", "--sobject", "custom") or []))

    def query(self, soql: str) -> list:
        """The lane builds `soql` only from names the org's describe returned."""
        return list((self._sf("data", "query", "--query", soql) or {}).get("records") or [])

    # -- data: the fallback sample loader and the one permission set ---------
    def insert(self, records: list) -> list:
        """`sf data import tree` for up to 200 records of one object; their ids, in order. (Sample
        data normally arrives through Headless 360; this is the fallback.)"""
        if not 1 <= len(records) <= 200:
            raise ValueError("insert 1 to 200 records")
        tree = {"records": []}
        for i, rec in enumerate(records):
            row = dict(rec)
            row["attributes"] = {"type": rec["attributes"]["type"], "referenceId": "r%d" % (i + 1)}
            tree["records"].append(row)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            path = os.path.join(folder, "records.json")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                json.dump(tree, fh)
            out = self._sf("data", "import", "tree", "--files", path) or []
        ids = {str(r.get("refId")): r.get("id") for r in out if isinstance(r, dict)}
        return [ids.get("r%d" % (i + 1)) for i in range(len(records))]

    def assign_permset(self, name: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
            raise ValueError("not a permission set name")
        try:
            self._sf("org", "assign", "permset", "--name", name)
            return "assigned"
        except SalesforceError as exc:
            if "duplicate" in str(exc).lower() or "already" in str(exc).lower():
                return "already"
            raise

    # -- metadata: sf project deploy --------------------------------------
    def deploy(self, zip_bytes: bytes, *, check_only: bool, purge_on_delete: bool = False) -> str:
        """`sf project deploy start --metadata-dir <zip> --async`; the job id. --dry-run is checkOnly."""
        flags = ["--single-package", "--async"]
        if check_only:
            flags.append("--dry-run")
        if purge_on_delete:
            flags.append("--purge-on-delete")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            path = os.path.join(folder, "deploy.zip")
            with open(path, "wb") as fh:
                fh.write(zip_bytes)
            out = self._sf("project", "deploy", "start", "--metadata-dir", path, *flags) or {}
        job = str(out.get("id") or "")
        if not JOB_ID_RE.fullmatch(job):
            raise SalesforceError("the CLI returned no deploy id")
        return job

    def status(self, job: str) -> dict:
        if not JOB_ID_RE.fullmatch(str(job)):
            raise ValueError("not a deploy id")
        return summarize(self._sf("project", "deploy", "report", "--job-id", job) or {})

    def wait(self, job: str, budget_seconds: float = 30.0, every: float = 3.0) -> dict:
        """Poll until done or the budget is spent; a result with done=False is still running."""
        deadline = self._clock() + budget_seconds
        while True:
            result = self.status(job)
            if result["done"] or self._clock() + every > deadline:
                return result
            self._sleep(every)


class DevOrg(CliOrg):
    """The owner's developer org: the PROMOTE target, and the Dev Hub."""

    kind = "devorg"

    def __init__(self, cli: SfCli | None = None, alias: str = HUB_ALIAS, **kw):
        super().__init__(alias, PINNED_ORG_ID, cli, **kw)

    def _check_host(self, host: str) -> None:
        if host != PINNED_HOST:
            raise OrgMismatch("the developer org target is pinned to %s" % PINNED_HOST)

    def confirm_scratch(self, org_id: str, today: _dt.date | None = None) -> dict | None:
        """The hub's ActiveScratchOrg row for org_id, if it has not expired; else None.
        (ActiveScratchOrg holds a row only while the org is active; it has no Status column.)"""
        if not isinstance(org_id, str) or not ORG_ID_RE.fullmatch(org_id):
            return None
        rows = self.query("SELECT Id, ScratchOrg, ExpirationDate FROM ActiveScratchOrg "
                          "WHERE ScratchOrg = '%s' LIMIT 1" % org_id[:15])
        if not rows:
            return None
        row = rows[0]
        today = today or _dt.datetime.now(_dt.timezone.utc).date()
        try:
            expires = _dt.date.fromisoformat(str(row.get("ExpirationDate"))[:10])
        except ValueError:
            return None
        if str(row.get("ScratchOrg") or "")[:15] != org_id[:15] or expires < today:
            return None
        return {"org_id": org_id, "expires": expires.isoformat()}


class ScratchOrg(CliOrg):
    """A scratch org the Dev Hub vouches for: the BUILD target of every conference prototype."""

    kind = "scratch"

    def __init__(self, record: dict, hub: DevOrg, cli: SfCli | None = None, **kw):
        record = record or {}
        super().__init__(str(record.get("alias") or ""), str(record.get("org_id") or ""), cli, **kw)
        if self.org_id[:15] == PINNED_ORG_ID[:15]:
            raise OrgMismatch("the developer org is never a scratch target")
        self.hub = hub
        self.expires = ""

    def _before_identity(self) -> None:
        # The hub first: a scratch org the hub does not vouch for is never touched.
        confirmed = self.hub.confirm_scratch(self.org_id)
        if not confirmed:
            raise OrgMismatch("the Dev Hub does not confirm scratch org %s as active" % self.org_id[:15])
        self.expires = confirmed["expires"]

    def _check_host(self, host: str) -> None:
        if not host.endswith(SCRATCH_SUFFIX):
            raise OrgMismatch("%s is not a scratch org My Domain" % host)


class Targets:
    """The target registry: {name, org_id, instance (from the CLI), auth (the CLI's)} for
    "scratch" (the default build target) and "devorg" (promote only)."""

    def __init__(self, cli: SfCli | None = None):
        self.cli = cli or SfCli()

    def hub(self) -> DevOrg:
        org = DevOrg(self.cli)
        org.verify()
        return org

    def open(self, kind: str, scratch_record: dict | None = None):
        if kind == "devorg":
            return self.hub()
        if kind == "scratch":
            if not scratch_record:
                raise OrgMismatch("no scratch org is attached to this session")
            org = ScratchOrg(scratch_record, self.hub(), self.cli)
            org.verify()
            return org
        raise OrgMismatch("unknown target %r" % kind)

    def attach(self, record: dict) -> dict:
        """A scratch org offered for this session, checked with the hub and the CLI before it is recorded."""
        org = ScratchOrg({"org_id": (record or {}).get("org_id"), "alias": (record or {}).get("alias")},
                         self.hub(), self.cli)
        org.verify()
        return {"org_id": org.org_id, "alias": org.alias, "expires": org.expires}


def summarize(r: dict) -> dict:
    """A deploy report in a closed shape: status, counts and Salesforce's exact errors."""
    r = r or {}
    failures = ((r.get("details") or {}).get("componentFailures") or [])
    if isinstance(failures, dict):
        failures = [failures]
    errors = []
    for f in failures[:10]:
        if isinstance(f, dict):
            errors.append(("%s %s: %s" % (f.get("componentType") or "", f.get("fullName") or "",
                                          f.get("problem") or "")).strip()[:300])
    if r.get("errorMessage"):
        errors.append(str(r["errorMessage"])[:300])
    status = str(r.get("status") or "Pending")
    return {"id": str(r.get("id") or ""), "status": status,
            "done": bool(r.get("done")) or status in DONE,
            "success": status == "Succeeded" and r.get("success") is not False,
            "deployed": int(r.get("numberComponentsDeployed") or 0),
            "total": int(r.get("numberComponentsTotal") or 0),
            "failed": int(r.get("numberComponentErrors") or 0),
            "check_only": bool(r.get("checkOnly")),
            "errors": errors}


__all__ = ["CliOrg", "DevOrg", "ScratchOrg", "Targets", "OrgMismatch", "SalesforceError", "PINNED_ORG_ID",
           "PINNED_HOST", "HUB_ALIAS", "lightning_links", "lightning_host", "summarize"]
