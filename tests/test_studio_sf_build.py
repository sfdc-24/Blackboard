"""The Salesforce build lane (workers/sf_build.py), its targets (workers/sf_org.py), the
app routes behind STUDIO_SF_BUILD, and scripts/sf_scratch.py's limit handling.

Every org here is a fake that understands the deploy zip it is sent; every HTTP call is
mocked; nothing reaches Salesforce, Redis or a model. Phase order is the subject:
discuss -> options -> prototype -> build (scratch) -> display -> test -> promote.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import io
import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

import tests.test_studio_controller as base  # noqa: E402  (sets sys.path for app.*; not re-run here)
from app.core import StudioController  # noqa: E402
from app.state import StudioRepository  # noqa: E402
from workers import sf_build as B  # noqa: E402
from workers import sf_org as O  # noqa: E402
from workers import sf_plan as P  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sf_scratch as SC  # noqa: E402

NS = "{http://soap.sforce.com/2006/04/metadata}"
SECRET = "SECRET-TOKEN-9f8e7d"
SCRATCH_ID = "00DVA00000KPyv72AD"

SPEND_PLAN = {"title": "Agent spend", "objects": [
    {"api_name": "Agent_Spend__c", "label": "Agent Spend", "plural": "Agent Spend",
     "fields": [{"api_name": "Month__c", "label": "Month", "type": "Date"},
                {"api_name": "Run_Count__c", "label": "Run Count", "type": "Summary", "operation": "count",
                 "child": "Agent_Run__c"},
                {"api_name": "Total_Cost__c", "label": "Total Cost", "type": "Summary", "operation": "sum",
                 "child": "Agent_Run__c", "child_field": "Cost__c"}]},
    {"api_name": "Agent_Run__c", "label": "Agent Run", "plural": "Agent Runs",
     "fields": [{"api_name": "Agent_Spend__c", "label": "Agent Spend", "type": "MasterDetail", "ref": "Agent_Spend__c"},
                {"api_name": "Cost__c", "label": "Cost", "type": "Currency"},
                {"api_name": "Model__c", "label": "Model", "type": "Picklist", "values": ["Opus", "Sonnet"]}]}]}
LEAN_PLAN = {"title": "Usage log", "objects": [
    {"api_name": "Usage_Log__c", "label": "Usage Log", "plural": "Usage Logs",
     "fields": [{"api_name": "Tokens__c", "label": "Tokens", "type": "Number"}]}]}
ASK = "I'd like to see how I can model API usage and agent spend in salesforce"
PERSONAL = "jane.doe@example.com"


class FakeArchitect:
    def __init__(self):
        self.calls = []

    def discuss(self, ask, prior, built):
        self.calls.append(("discuss", ask))
        return {"architecture": {"title": "API usage and agent spend", "steps": [
            {"id": "api", "label": "API calls and agent runs", "detail": "the sources"},
            {"id": "capture", "label": "Capture", "detail": "record insert by the gateway"},
            {"id": "store", "label": "Storage", "detail": "custom objects"},
            {"id": "rollup", "label": "Roll-up and reporting", "detail": "roll-up summaries, reports"}],
            "edges": [{"from": "api", "to": "capture", "label": "usage event"},
                      {"from": "capture", "to": "store", "label": "insert"},
                      {"from": "store", "to": "rollup", "label": "roll-up"}]},
            "explanation": "Usage flows from the sources into custom objects and rolls up monthly.",
            "options": B.check_options([
                dict(option_id="a", label="Lean log", summary="One object.", limits="low", storage="low",
                     licences="none", complexity="low", time_to_build="minutes", recommended=False, plan=LEAN_PLAN),
                dict(option_id="b", label="Spend with runs", summary="Master-detail with roll-ups.",
                     limits="data storage grows per run", storage="2 KB per run", licences="none",
                     complexity="medium", time_to_build="minutes", recommended=True,
                     because="Roll-ups answer spend per month without reports.", plan=SPEND_PLAN)])[0]}

    def revise(self, plan, text, built):
        self.calls.append(("revise", text))
        new = copy.deepcopy(plan)
        if "remove" in text:
            new["objects"][1]["fields"].pop()
        else:
            new["objects"][1]["fields"].append({"api_name": "Tokens__c", "label": "Tokens", "type": "Number",
                                                "description": "", "precision": 18, "scale": 0})
        return {"plan": new, "summary": "Added Tokens to Agent Run."}


class FakeOrg:
    """An org that understands the deploy zips it is sent: describe and query answer from what
    was really deployed (and inserted), never from the plan."""

    def __init__(self, kind, host):
        self.kind, self.host = kind, host
        self.token = SECRET
        self.objects = {}           # api -> describe
        self.records = {}           # api -> [record]
        self.deploys = []
        self.result = {"done": True, "success": True, "status": "Succeeded", "errors": [], "deployed": 0}
        self.override = {}
        self.broken_rollup = False
        self.existing = set()
        self.calls = []
        self.permsets = []

    def links(self, api, record_id=""):
        return O.lightning_links(api, record_id, self.host)

    def verify(self):
        return {"org_id": "x"}

    def custom_objects(self):
        return sorted(set(self.objects) | self.existing)

    def describe(self, api):
        self.calls.append(("describe", api))
        if api in self.override:
            return self.override[api]
        if api not in self.objects:
            return {"name": api, "label": api, "custom": False, "fields": [
                {"name": "Id", "type": "id", "custom": False}] + [f for f in self.objects.get("_std_" + api, [])]}
        return self.objects[api]

    def deploy(self, zip_bytes, *, check_only, purge_on_delete=False):
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        self.deploys.append({"check_only": check_only, "purge": purge_on_delete, "files": sorted(z.namelist())})
        total = len(re.findall(r"<members>", z.read("package.xml").decode()))
        if "destructiveChanges.xml" in z.namelist():
            total = len(re.findall(r"<members>", z.read("destructiveChanges.xml").decode()))
        self.result = dict(self.result, total=total, deployed=total)
        if check_only or not self.result["success"]:
            return "0Af000000000001AAA"
        if "destructiveChanges.xml" in z.namelist():
            root = ET.fromstring(z.read("destructiveChanges.xml"))
            for t in root.findall(NS + "types"):
                for m in t.findall(NS + "members"):
                    if t.find(NS + "name").text == "CustomObject":
                        self.objects.pop(m.text, None)
                        self.records.pop(m.text, None)
            return "0Af000000000002AAA"
        for name in z.namelist():
            if name.startswith("objects/"):
                self._absorb(name[8:-7], ET.fromstring(z.read(name)))
        return "0Af000000000003AAA"

    def _absorb(self, api, root):
        new = root.find(NS + "label") is not None
        if new:
            self.objects[api] = {"name": api, "label": root.find(NS + "label").text,
                                 "labelPlural": root.find(NS + "pluralLabel").text, "custom": True,
                                 "fields": [{"name": "Id", "type": "id", "custom": False},
                                            {"name": "Name", "label": "Name", "type": "string", "nameField": True,
                                             "custom": False, "length": 80}]}
        holder = self.objects[api] if api in self.objects else self.objects.setdefault(
            api, {"name": api, "label": api, "custom": False, "fields": [{"name": "Id", "type": "id"}]})
        kinds = {"Text": "string", "Number": "double", "Currency": "currency", "Percent": "percent", "Date": "date",
                 "DateTime": "datetime", "Checkbox": "boolean", "Picklist": "picklist", "LongTextArea": "textarea",
                 "Lookup": "reference", "MasterDetail": "reference", "Summary": "double"}
        for f in root.findall(NS + "fields"):
            kind = f.find(NS + "type").text
            d = {"name": f.find(NS + "fullName").text, "label": f.find(NS + "label").text, "type": kinds[kind],
                 "custom": True}
            if kind in ("Lookup", "MasterDetail"):
                d["referenceTo"] = [f.find(NS + "referenceTo").text]
                d["relationshipOrder"] = 0 if kind == "MasterDetail" else None
            if kind == "Summary":
                d.update(calculated=True, calculatedFormula=None, _op=f.find(NS + "summaryOperation").text,
                         _key=f.find(NS + "summaryForeignKey").text,
                         _field=(f.find(NS + "summarizedField").text if f.find(NS + "summarizedField") is not None
                                 else ""))
            holder["fields"].append(d)

    def wait(self, job, budget_seconds=30, every=2):
        return dict(self.result, id=job, check_only=False)

    def status(self, job):
        return self.wait(job)

    def query(self, soql):
        self.calls.append(("query", soql))
        m = re.match(r"SELECT (.+?) FROM (\w+)(?: WHERE (\w+) != null)?", soql)
        cols, api, notnull = [c.strip() for c in m.group(1).split(",")], m.group(2), m.group(3)
        rows = [dict(r) for r in self.records.get(api, [])]
        if notnull:
            rows = [r for r in rows if r.get(notnull)]
        for f in (self.objects.get(api) or {}).get("fields", []):
            if f.get("_op"):
                child, link = f["_key"].split(".")
                for r in rows:
                    kids = [k for k in self.records.get(child, []) if k.get(link) == r["Id"]]
                    vals = [float(k.get(f["_field"].split(".")[1]) or 0) for k in kids] if f["_field"] else []
                    r[f["name"]] = {"count": len(kids), "sum": sum(vals)}.get(f["_op"]) + (1 if self.broken_rollup else 0)
        return [{c: r.get(c) for c in cols} for r in rows]

    def insert(self, records):
        ids = []
        for rec in records:
            api = rec["attributes"]["type"]
            rid = "a0%s%012d" % ("X", sum(len(v) for v in self.records.values()) + 1)
            row = {k: v for k, v in rec.items() if k != "attributes"}
            row["Id"] = rid
            row.setdefault("Name", "N-%s" % rid[-4:])
            self.records.setdefault(api, []).append(row)
            ids.append(rid)
        return ids

    def assign_permset(self, name):
        self.permsets.append(name)
        return "assigned"


class FakeTargets:
    def __init__(self, confirm=True):
        self.orgs = {"scratch": FakeOrg("scratch", "acme-dev-ed.scratch.my.salesforce.com"),
                     "devorg": FakeOrg("devorg", O.PINNED_HOST)}
        self.opened = []
        self.confirm = confirm
        self.forbid = set()

    def open(self, kind, record=None):
        if kind in self.forbid:
            raise AssertionError("the %s org must not be touched here" % kind)
        if kind == "scratch" and not record:
            raise O.OrgMismatch("no scratch org is attached")
        self.opened.append(kind)
        return self.orgs[kind]

    def attach(self, record):
        if not self.confirm:
            raise O.OrgMismatch("the Dev Hub does not confirm it")
        return {"org_id": record["org_id"], "alias": record["alias"], "expires": "2026-10-16"}


class ListSink:
    def __init__(self):
        self.records = []

    def publish(self, rec):
        self.records.append(rec)


class Harness:
    def __init__(self, targets=None):
        self.store = base.MemoryStore()
        self.repo = StudioRepository(self.store, clock=lambda: 1000.0)
        self.controller = StudioController(self.repo, base.CountingWorker(), clock=lambda: 1000.0,
                                           id_factory=base.IDs())
        self.targets = targets or FakeTargets()
        self.architect = FakeArchitect()
        self.sink = ListSink()
        self.logs = []
        self.views = []

        class Viewer:
            def request(inner, sid, req):
                self.views.append(req)
                return True

        self.lane = B.SfBuildLane(load=lambda sid: self.repo.load(sid).state, commit=self.controller.commit_lane,
                                  targets=self.targets, architect=self.architect, sink=self.sink, viewer=Viewer(),
                                  clock=lambda: 1_790_000_000.0, log=lambda e, **f: self.logs.append((e, f)))
        state, _ = self.controller.create_session("Prototype", "operator-subject", "c-1", "blank", False, None,
                                                  False, "salesforce_build")
        self.sid = state["session_id"]
        self.events = []

    def do(self, action, **kw):
        out = self.lane.handle(self.sid, dict(kw, action=action))
        self.events += out["events"]
        return out

    def state(self):
        return self.repo.load(self.sid).state

    def sf(self):
        return self.state()["sf_build"]

    def to_display(self, plan_option="b"):
        self.do("attach_scratch", org_id=SCRATCH_ID, alias="conf-acme")
        self.do("ask", text=ASK)
        self.do("options")
        self.do("pick", option_id=plan_option)
        self.do("build")
        return self.do("confirm", answer="yes")

    def types(self, events=None):
        return [e["type"] for e in (events if events is not None else self.events)]


class PhaseOrder(unittest.TestCase):
    def test_the_phases_go_in_order_and_each_refuses_out_of_turn(self):
        h = Harness()
        for action, kw in (("options", {}), ("pick", {"option_id": "b"}), ("build", {}), ("sample", {}),
                           ("test", {}), ("promote", {}), ("confirm", {"answer": "yes"}), ("undo", {})):
            with self.assertRaises(B.LaneRefused, msg=action):
                h.do(action, **kw)
        h.do("attach_scratch", org_id=SCRATCH_ID, alias="conf-acme")
        self.assertEqual("idle", h.sf()["phase"])
        h.do("ask", text=ASK)
        self.assertEqual(("discuss", "drawn"), (h.sf()["phase"], h.sf()["step"]))
        for action in ("build", "test", "promote"):
            with self.assertRaises(B.LaneRefused):
                h.do(action)
        h.do("options")
        self.assertEqual("options", h.sf()["phase"])
        with self.assertRaises(B.LaneRefused):
            h.do("build")                                       # no build before the prototype
        h.do("pick", option_id="b")
        self.assertEqual("prototype", h.sf()["phase"])
        with self.assertRaises(B.LaneRefused):
            h.do("test")                                        # no test before a build
        self.assertEqual([], h.targets.orgs["scratch"].deploys)
        phases = [r["phase"] for r in h.sink.records]
        self.assertEqual(["idle", "discuss", "discuss", "options", "prototype", "prototype"], phases)

    def test_discuss_draws_a_titled_flow_and_options_come_as_a_decision_batch(self):
        h = Harness()
        h.do("ask", text=ASK)
        patch = next(e for e in h.events if e["type"] == "artifact.patch")
        node = patch["payload"]["ops"][-1]["node"]
        self.assertEqual(("section", "heading"), (node["kind"], node["children"][0]["kind"]))
        self.assertIn("process-step", [c["kind"] for c in node["children"]])
        self.assertIn("edge", [c["kind"] for c in node["children"]])
        h.do("options")
        batch = next(e for e in h.events if e["type"] == "decision.batch")["payload"]
        design = batch["questions"][0]
        self.assertEqual(["a", "b"], [o["option_id"] for o in design["options"]])
        rec = [o for o in design["options"] if o.get("recommended")]
        self.assertEqual(["b"], [o["option_id"] for o in rec])
        self.assertIn("Trade-offs", rec[0]["consequence"])
        self.assertIn("licences: none", rec[0]["consequence"])
        self.assertTrue(rec[0]["recommended_because"])

    def test_the_prototype_never_calls_an_org(self):
        h = Harness()
        h.targets.forbid = {"scratch", "devorg"}
        h.do("ask", text=ASK)
        h.do("options")
        h.do("pick", option_id="b")
        h.do("revise", text="add tokens to the run")
        self.assertEqual([], h.targets.opened)
        model = [e for e in h.events if e["type"] == "model.updated"][-1]["payload"]["model"]
        types = [f["type"] for o in model["objects"] for f in o["fields"]]
        self.assertTrue(types and all(t.endswith("- proposed") for t in types))
        self.assertIn("Tokens (Tokens__c)", [f["name"] for o in model["objects"] for f in o["fields"]])
        labels = [op["node"]["label"] for e in h.events if e["type"] == "artifact.patch"
                  for op in e["payload"]["ops"] if op["op"] == "insert_child"]
        self.assertTrue(any("Agent Spend - Record Page" in x for x in labels), labels)
        self.assertTrue(any("Agent Runs - List View" in x for x in labels), labels)


class Guards(unittest.TestCase):
    def test_build_only_from_the_prototype_phase(self):
        h = Harness()
        h.to_display()
        state = h.state()
        sf = state["sf_build"]
        sf["plan"]["objects"][1]["fields"].append({"api_name": "Tokens__c", "label": "Tokens", "type": "Number",
                                                   "description": "", "precision": 18, "scale": 0})
        for phase in ("discuss", "options", "display", "test"):
            sf["phase"] = phase
            h.repo.save(h.sid, state, h.repo.load(h.sid).token)
            state = h.state()
            sf = state["sf_build"]
            with self.assertRaises(B.LaneRefused, msg=phase):
                h.do("build")
        self.assertEqual(2, len(h.targets.orgs["scratch"].deploys))

    def test_the_lane_refuses_a_visitor_session_whatever_the_route(self):
        h = Harness()
        state = h.state()
        state["visitor_subject"], state["operator_subject"] = "visitor-subject", ""
        h.repo.save(h.sid, state, h.repo.load(h.sid).token)
        with self.assertRaises(B.LaneRefused) as caught:
            h.do("ask", text=ASK)
        self.assertEqual(403, caught.exception.status)
        self.assertEqual([], h.architect.calls)

    def test_a_built_object_dropped_from_the_plan_is_refused(self):
        plan = P.validate_plan(SPEND_PLAN)
        built = P.record_built(None, plan, P.build_package(plan)["change"])
        fewer = copy.deepcopy(SPEND_PLAN)
        fewer["objects"] = [o for o in fewer["objects"] if o["api_name"] == "Agent_Spend__c"]
        for f in list(fewer["objects"][0]["fields"]):
            if f["type"] == "Summary":
                fewer["objects"][0]["fields"].remove(f)
        with self.assertRaises(P.PlanError) as caught:
            P.delta(P.validate_plan(fewer), built)
        self.assertIn("undo", str(caught.exception))


class BuildLive(unittest.TestCase):
    def test_a_build_needs_an_attached_scratch_org_and_validates_before_it_asks(self):
        h = Harness()
        h.do("ask", text=ASK)
        h.do("options")
        h.do("pick", option_id="b")
        with self.assertRaises(B.LaneRefused) as caught:
            h.do("build")
        self.assertIn("scratch", str(caught.exception))
        h.do("attach_scratch", org_id=SCRATCH_ID, alias="conf-acme")
        h.do("build")
        scratch = h.targets.orgs["scratch"]
        self.assertEqual([True], [d["check_only"] for d in scratch.deploys])        # checkOnly first, only
        self.assertEqual("confirm_asked", h.sf()["step"])
        asked = [e for e in h.events if e["type"] == "question.asked"][-1]["payload"]["question"]
        self.assertEqual("Build it in Salesforce now?", asked["prompt"])
        self.assertEqual({"yes", "no"}, {o["option_id"] for o in asked["options"]})

    def test_nothing_is_deployed_without_the_in_session_yes(self):
        h = Harness()
        h.do("attach_scratch", org_id=SCRATCH_ID, alias="conf-acme")
        h.do("ask", text=ASK)
        h.do("options")
        h.do("pick", option_id="b")
        h.do("build")
        scratch = h.targets.orgs["scratch"]
        with self.assertRaises(B.LaneRefused):
            h.do("confirm", answer="yes", question_id="sfb-confirm-999")       # a stale question
        h.do("confirm", answer="no")
        self.assertEqual([True], [d["check_only"] for d in scratch.deploys])
        self.assertEqual("prototype", h.sf()["phase"])
        with self.assertRaises(B.LaneRefused):
            h.do("confirm", answer="yes")                                       # the no is final
        h.do("build")
        # a plan changed after validation is never deployed on the old yes
        state = h.state()
        state["sf_build"]["plan_hash"] = "changed"
        h.repo.save(h.sid, state, h.repo.load(h.sid).token)
        with self.assertRaises(B.LaneRefused):
            h.do("confirm", answer="yes")
        self.assertEqual([True, True], [d["check_only"] for d in scratch.deploys])

    def test_the_yes_deploys_to_the_scratch_org_never_the_developer_org(self):
        h = Harness()
        h.targets.forbid = {"devorg"}
        h.to_display()
        self.assertEqual([True, False], [d["check_only"] for d in h.targets.orgs["scratch"].deploys])
        self.assertEqual([], h.targets.orgs["devorg"].deploys)
        self.assertEqual("display", h.sf()["phase"])
        self.assertNotIn("devorg", h.targets.opened)
        self.assertEqual(["Conf_Build_Access"], h.targets.orgs["scratch"].permsets)
        actions = [(f["action"], f["target"], f["result"]) for e, f in h.logs if e == "studio.sf_build_audit"]
        self.assertEqual([("validate", "scratch", "succeeded"), ("deploy", "scratch", "succeeded")], actions)
        for e, f in h.logs:
            if e == "studio.sf_build_audit":
                self.assertEqual({"session_id", "action", "target", "plan_hash", "components", "result"}, set(f))
        self.assertEqual(2, len(h.sf()["audit"]))

    def test_display_uses_describe_and_query_results_not_the_plan(self):
        h = Harness()
        h.do("attach_scratch", org_id=SCRATCH_ID, alias="conf-acme")
        h.do("ask", text=ASK)
        h.do("options")
        h.do("pick", option_id="b")
        h.do("build")
        org = h.targets.orgs["scratch"]
        # What the org reports differs from the plan: a field the plan never had, one it lacks.
        org.override["Agent_Run__c"] = {"name": "Agent_Run__c", "label": "Agent Run", "labelPlural": "Agent Runs",
                                        "custom": True, "fields": [
                                            {"name": "Name", "label": "Run Name", "type": "string", "nameField": True,
                                             "length": 80},
                                            {"name": "Org_Only__c", "label": "Org Only", "type": "string",
                                             "custom": True, "length": 12}]}
        org.records["Agent_Run__c"] = [{"Id": "a0R000000000001AAA", "Name": "RUN-FROM-ORG", "Org_Only__c": "live"}]
        h.do("confirm", answer="yes")
        model = [e for e in h.events if e["type"] == "model.updated"][-1]["payload"]["model"]
        run = next(o for o in model["objects"] if o["id"] == "Agent_Run__c")
        names = [f["name"] for f in run["fields"]]
        self.assertIn("Org Only (Org_Only__c)", names)
        self.assertNotIn("Cost (Cost__c)", names)                      # in the plan, not in the org
        self.assertTrue(all(f["type"].endswith("- built") for f in run["fields"]))
        self.assertTrue(model["domain"].startswith("Built in your org"))
        listing = [op["node"] for e in h.events if e["type"] == "artifact.patch" for op in e["payload"]["ops"]
                   if op["op"] == "insert_child" and op["node"]["id"] == "sfb-list-Agent_Run__c"][-1]
        cards = listing["children"][-1]["children"]
        self.assertEqual("RUN-FROM-ORG", cards[0]["label"])
        self.assertIn("Org Only: live", cards[0]["detail"])
        self.assertIn("lightning/r/Agent_Run__c/a0R000000000001AAA/view", cards[0]["detail"])
        self.assertIn(("describe", "Agent_Run__c"), org.calls)
        confirm = " ".join(e["payload"]["text"] for e in h.events if e["type"] == "confirm")
        self.assertIn("acme-dev-ed.scratch.lightning.force.com/lightning/setup/ObjectManager/Agent_Spend__c", confirm)
        self.assertEqual({"object", "list"}, {v["kind"] for v in h.views})

    def test_sample_data_then_the_tests_pass_and_roll_ups_are_checked_by_query(self):
        h = Harness()
        h.to_display()
        h.do("sample")
        org = h.targets.orgs["scratch"]
        self.assertEqual(3, len(org.records["Agent_Spend__c"]))
        self.assertEqual(10, len(org.records["Agent_Run__c"]))
        self.assertTrue(all(r["Agent_Spend__c"] for r in org.records["Agent_Run__c"]))
        h.do("test")
        test = h.sf()["test"]
        self.assertTrue(test["passed"], test["checks"])
        names = [c["name"] for c in test["checks"]]
        self.assertIn("Roll-up Agent Spend.Total Cost computes", names)
        self.assertIn("Agent Run.Agent Spend links to Agent_Spend__c", names)
        self.assertEqual("test_passed", h.sink.records[-1]["result"])
        self.assertEqual("test", h.sink.records[-1]["phase"])
        section = [op["node"] for e in h.events if e["type"] == "artifact.patch" for op in e["payload"]["ops"]
                   if op["op"] == "insert_child" and op["node"]["id"] == "sfb-test"][-1]
        self.assertTrue(section["label"].startswith("Acceptance tests - %d of %d passed" % (len(names), len(names))))

    def test_undo_deploys_the_stored_destructive_changes_after_its_own_yes(self):
        h = Harness()
        h.to_display()
        org = h.targets.orgs["scratch"]
        h.do("undo")
        self.assertEqual("undo_asked", h.sf()["step"])
        self.assertEqual(2, len(org.deploys))
        h.do("confirm", answer="yes")
        last = org.deploys[-1]
        self.assertEqual((False, True), (last["check_only"], last["purge"]))
        self.assertIn("destructiveChanges.xml", last["files"])
        self.assertEqual({}, h.sf()["built"]["objects"])
        self.assertEqual("prototype", h.sf()["phase"])
        self.assertNotIn("Agent_Spend__c", org.objects)


class Promote(unittest.TestCase):
    def _tested(self, broken=False):
        h = Harness()
        h.to_display()
        h.targets.orgs["scratch"].broken_rollup = broken
        h.do("test")
        return h

    def test_promote_is_refused_before_a_passing_test(self):
        h = Harness()
        h.to_display()
        with self.assertRaises(B.LaneRefused) as caught:
            h.do("promote")
        self.assertIn("test", str(caught.exception))
        failing = self._tested(broken=True)
        self.assertFalse(failing.sf()["test"]["passed"])
        with self.assertRaises(B.LaneRefused):
            failing.do("promote")
        self.assertEqual([], failing.targets.orgs["devorg"].deploys)

    def test_promote_needs_the_command_then_validates_then_waits_for_the_yes(self):
        h = self._tested()
        self.assertTrue(h.sf()["test"]["passed"])
        dev = h.targets.orgs["devorg"]
        self.assertEqual([], dev.deploys)                                   # passing alone promotes nothing
        h.do("promote")
        self.assertEqual([True], [d["check_only"] for d in dev.deploys])   # validated first
        self.assertEqual(("promote", "promote_confirm_asked"), (h.sf()["phase"], h.sf()["step"]))
        asked = [e for e in h.events if e["type"] == "question.asked"][-1]["payload"]["question"]
        self.assertEqual("Promote it to your developer org now?", asked["prompt"])
        h.do("confirm", answer="yes")
        self.assertEqual([True, False], [d["check_only"] for d in dev.deploys])
        self.assertIn("Agent_Spend__c", dev.objects)
        self.assertEqual({"Agent_Spend__c", "Agent_Run__c"}, set(h.sf()["dev_built"]["objects"]))
        self.assertIn("promote", [r["phase"] for r in h.sink.records])
        self.assertEqual("promote_read_back", h.sink.records[-1]["result"])
        builds = [(b["target"], bool(b["destructive_xml"])) for b in h.sf()["builds"]]
        self.assertEqual([("scratch", True), ("devorg", True)], builds)
        h.do("undo", target="devorg")
        h.do("confirm", answer="yes")
        self.assertEqual((False, False), (dev.deploys[-1]["check_only"], dev.deploys[-1]["purge"]))
        self.assertNotIn("Agent_Spend__c", dev.objects)

    def test_a_promote_never_overwrites_what_the_developer_org_already_has(self):
        h = self._tested()
        h.targets.orgs["devorg"].existing = {"Agent_Spend__c"}
        h.do("promote")
        self.assertEqual([], h.targets.orgs["devorg"].deploys)
        self.assertEqual("test", h.sf()["phase"])
        self.assertIn("already exist", [e for e in h.events if e["type"] == "progress"][-1]["payload"]["text"])

    def test_spoken_words_reach_the_right_step(self):
        sf = {"phase": "test", "step": "passed", "pending": None}
        self.assertEqual("promote", B.classify("approved", sf)["action"])
        self.assertEqual("promote", B.classify("this is finalized, promote it to the developer org", sf)["action"])
        self.assertEqual("test", B.classify("run the tests", dict(sf, phase="display"))["action"])
        self.assertEqual({"action": "undo", "target": "devorg"}, B.classify("undo the promote", sf))
        self.assertEqual({"action": "undo", "target": "scratch"}, B.classify("undo the last build", sf))
        self.assertEqual("confirm", B.classify("yes", dict(sf, step="promote_confirm_asked"))["action"])
        self.assertEqual("build", B.classify("ok build it", dict(sf, phase="prototype", step=""))["action"])
        self.assertEqual("pick", B.classify("go with option b", dict(sf, phase="options", options=[]))["action"])
        self.assertEqual("status", B.classify("anything", dict(sf, pending={"deploy_id": "x"}))["action"])

    def test_a_refusal_said_aloud_is_spoken_back(self):
        h = Harness()
        out = h.lane.handle(h.sid, {"action": "say", "text": "show me the options", "item_id": "it-1"})
        self.assertEqual(["confirm"], h.types(out["events"]))
        self.assertIn("options come after the architecture", out["events"][0]["payload"]["text"])
        again = h.lane.handle(h.sid, {"action": "say", "text": "show me the options", "item_id": "it-1"})
        self.assertTrue(again["deduplicated"])


class NoSecretsNoPII(unittest.TestCase):
    def test_no_token_or_session_appears_in_any_event_record_or_view(self):
        h = Harness()
        h.lane.handle(h.sid, {"action": "attach_scratch", "org_id": SCRATCH_ID, "alias": "conf-acme"})
        h.do("ask", text=ASK + " - I am " + PERSONAL)
        h.do("options")
        h.do("pick", option_id="b")
        h.do("build")
        h.do("confirm", answer="yes")
        h.do("sample")
        h.do("test")
        h.do("promote")
        h.do("confirm", answer="yes")
        blob = json.dumps({"events": h.events, "sink": h.sink.records, "views": h.views,
                           "logs": h.logs, "state": h.state()["sf_build"]})
        for marker in (SECRET, "sid=", "frontdoor", "Bearer", "access_token"):
            self.assertNotIn(marker, blob)
        sink_blob = json.dumps(h.sink.records + [f for _, f in h.logs])
        self.assertNotIn(PERSONAL, sink_blob)                         # no PII in the sink or the audit
        self.assertNotIn("agent spend in salesforce", sink_blob)
        for rec in h.sink.records:
            self.assertEqual({"session_id", "room", "seq", "phase", "plan_hash", "objects", "components", "result",
                              "at"}, set(rec))
        self.assertEqual(list(range(1, len(h.sink.records) + 1)), [int(r["seq"]) for r in h.sink.records])


class FakeCli:
    """A fake `sf` binary: answers by command, records every call (never a network)."""

    def __init__(self, hub_rows=None, scratch_id=SCRATCH_ID, dev_id=O.PINNED_ORG_ID,
                 dev_host=O.PINNED_HOST, scratch_host="acme-dev-ed.scratch.my.salesforce.com"):
        self.calls = []
        self.hub_rows = hub_rows if hub_rows is not None else [
            {"ScratchOrg": SCRATCH_ID[:15], "ExpirationDate": "2999-01-01"}]
        self.ids = {"sfdc24web": (dev_id, dev_host), "conf-acme": (scratch_id, scratch_host)}

    def __call__(self, argv, **kw):
        args = argv[1:-1]
        self.calls.append(args)
        org = args[args.index("--target-org") + 1] if "--target-org" in args else ""
        if args[:2] == ["org", "display"]:
            org_id, host = self.ids[org]
            result = {"id": org_id, "instanceUrl": "https://" + host, "accessToken": SECRET}
        elif args[:2] == ["data", "query"]:
            result = {"records": self.hub_rows if "ActiveScratchOrg" in args[3] else []}
        elif args[:2] == ["sobject", "describe"]:
            result = {"name": args[3]}
        elif args[:3] == ["project", "deploy", "start"]:
            result = {"id": "0Af000000000001AAA"}
        elif args[:3] == ["project", "deploy", "report"]:
            result = {"id": "0Af000000000001AAA", "status": "Failed", "done": True, "success": False,
                      "numberComponentsTotal": 2, "details": {"componentFailures": [
                          {"componentType": "CustomField", "fullName": "A__c.B__c", "problem": "bad type"}]}}
            return Proc({"status": 1, "message": "Deploy failed.", "result": result})
        elif args[:3] == ["org", "assign", "permset"]:
            return Proc({"status": 1, "message": "Duplicate PermissionSetAssignment"})
        else:
            result = {}
        return Proc({"status": 0, "result": result})


class Proc:
    def __init__(self, out):
        self.stdout, self.stderr = json.dumps(out), ""


class Targets(unittest.TestCase):
    def cli(self, fake):
        from workers.sf_cli import SfCli
        return SfCli(runner=fake, binary="sf")

    def test_the_wrapper_runs_only_allowlisted_commands_and_never_a_credential_one(self):
        from workers.sf_cli import SalesforceError
        cli = self.cli(FakeCli())
        for argv in (("org", "auth", "show-access-token"), ("org", "display", "--verbose"), ("apex", "run"),
                     ("org", "delete", "sandbox"), ("data", "delete", "record"), ("project", "retrieve", "start")):
            with self.assertRaises(SalesforceError):
                cli.run(*argv, "--target-org", "conf-acme")

    def test_the_developer_org_is_pinned_by_alias_identity_and_host(self):
        fake = FakeCli(dev_id="00D000000000001AAA")
        with self.assertRaises(O.OrgMismatch):
            O.Targets(self.cli(fake)).open("devorg").describe("Lead")
        self.assertFalse(any(c[:2] == ["sobject", "describe"] for c in fake.calls))
        fake = FakeCli(dev_host="evil-dev-ed.develop.my.salesforce.com")
        with self.assertRaises(O.OrgMismatch):
            O.Targets(self.cli(fake)).open("devorg")
        good = O.Targets(self.cli(FakeCli())).open("devorg")
        self.assertEqual("Lead", good.describe("Lead")["name"])

    def test_a_scratch_target_is_refused_unless_the_hub_confirms_it(self):
        row = {"ScratchOrg": SCRATCH_ID[:15], "ExpirationDate": "2999-01-01"}
        for rows in ([], [dict(row, ExpirationDate="2020-01-01")], [dict(row, ScratchOrg="00DVA00000ZZZZZ")]):
            fake = FakeCli(hub_rows=rows)
            with self.assertRaises(O.OrgMismatch):
                O.Targets(self.cli(fake)).attach({"org_id": SCRATCH_ID, "alias": "conf-acme"})
            # the scratch org itself was never asked anything
            self.assertFalse(any("conf-acme" in c for c in fake.calls), fake.calls)
        with self.assertRaises(O.OrgMismatch):
            O.Targets(self.cli(FakeCli())).attach({"org_id": O.PINNED_ORG_ID, "alias": "sfdc24web"})
        with self.assertRaises(O.OrgMismatch):        # the alias is some other org than the one offered
            O.Targets(self.cli(FakeCli(scratch_id="00DVA00000QQQQQ2AD", hub_rows=[
                {"ScratchOrg": "00DVA00000KPyv7", "ExpirationDate": "2999-01-01"}]))).attach(
                {"org_id": SCRATCH_ID, "alias": "conf-acme"})
        with self.assertRaises(O.OrgMismatch):        # a scratch alias that resolves to a non-scratch host
            O.Targets(self.cli(FakeCli(scratch_host=O.PINNED_HOST))).attach(
                {"org_id": SCRATCH_ID, "alias": "conf-acme"})
        ok = O.Targets(self.cli(FakeCli())).attach({"org_id": SCRATCH_ID, "alias": "conf-acme"})
        self.assertEqual({"org_id": SCRATCH_ID, "alias": "conf-acme", "expires": "2999-01-01"}, ok)

    def test_deploys_go_through_sf_project_deploy_with_dry_run_for_check_only(self):
        fake = FakeCli()
        org = O.Targets(self.cli(fake)).open("scratch", {"org_id": SCRATCH_ID, "alias": "conf-acme"})
        org.deploy(b"zip", check_only=True)
        org.deploy(b"zip", check_only=False, purge_on_delete=True)
        starts = [c for c in fake.calls if c[:3] == ["project", "deploy", "start"]]
        self.assertIn("--dry-run", starts[0])
        self.assertNotIn("--dry-run", starts[1])
        self.assertIn("--purge-on-delete", starts[1])
        for c in starts:
            self.assertEqual("conf-acme", c[c.index("--target-org") + 1])
            self.assertTrue(c[c.index("--metadata-dir") + 1].endswith("deploy.zip"))
            self.assertIn("--async", c)
        report = org.status("0Af000000000001AAA")
        self.assertEqual((True, False, ["CustomField A__c.B__c: bad type"]),
                         (report["done"], report["success"], report["errors"]))
        self.assertEqual("already", org.assign_permset("Conf_Build_Access"))
        self.assertNotIn(SECRET, json.dumps(report))


class ScratchScript(unittest.TestCase):
    def test_capacity_reuses_then_respects_the_active_and_daily_limits(self):
        lims = {"ActiveScratchOrgs": {"max": 3, "remaining": 2}, "DailyScratchOrgs": {"max": 6, "remaining": 5}}
        self.assertTrue(SC.plan_capacity(lims, reusable=True)["reuse"])
        ok = SC.plan_capacity(lims, reusable=False)
        self.assertTrue(ok["create"])
        self.assertEqual({"active_remaining": 1, "daily_remaining": 4}, ok["after"])
        full = dict(lims, ActiveScratchOrgs={"max": 3, "remaining": 0})
        self.assertIn("active", SC.plan_capacity(full, reusable=False)["reason"])
        self.assertFalse(SC.plan_capacity(full, reusable=False)["create"])
        spent = dict(lims, DailyScratchOrgs={"max": 6, "remaining": 0})
        self.assertFalse(SC.plan_capacity(spent, reusable=False)["create"])
        self.assertIn("today", SC.plan_capacity(spent, reusable=False)["reason"])
        last = dict(lims, DailyScratchOrgs={"max": 6, "remaining": 1})
        self.assertIn("last", SC.plan_capacity(last, reusable=False)["reason"])

    def runner(self, limits, snapshot="Active", known=False):
        calls = []

        def run(args, **kw):
            calls.append(args[1:])
            if args[1:3] == ["org", "display"]:
                if not known:
                    return Proc({"status": 1, "message": "No authorization information found"})
                return Proc({"status": 0, "result": {"id": SCRATCH_ID, "instanceUrl": "https://x", "accessToken": SECRET,
                                                     "expirationDate": "2026-10-16"}})
            if args[1:4] == ["org", "list", "limits"]:
                return Proc({"status": 0, "result": [{"name": k, "max": v["max"], "remaining": v["remaining"]}
                                                      for k, v in limits.items()]})
            if args[1:3] == ["data", "query"]:
                q = args[4]
                if "OrgSnapshot" in q:
                    return Proc({"status": 0, "result": {"records": [{"Status": snapshot}]}})
                return Proc({"status": 0, "result": {"records": []}})
            if args[1:4] == ["org", "create", "scratch"]:
                known_holder[0] = True
                return Proc({"status": 0, "result": {}})
            return Proc({"status": 0, "result": {}})
        known_holder = [known]
        return run, calls

    def test_create_refuses_at_the_limits_without_asking_for_an_org(self):
        run, calls = self.runner({"ActiveScratchOrgs": {"max": 3, "remaining": 3},
                                  "DailyScratchOrgs": {"max": 6, "remaining": 0}})
        with self.assertRaises(SC.Refused):
            SC.create("conf-acme", runner=run)
        self.assertFalse(any(c[:3] == ["org", "create", "scratch"] for c in calls))

    def test_the_definition_is_the_snapshot_or_the_shape_never_a_feature_list(self):
        self.assertEqual({"orgName": SC.ORG_NAME, "snapshot": "ConfBase"}, SC.scratch_definition(True))
        self.assertEqual({"orgName": SC.ORG_NAME, "sourceOrg": "00Dbm00000wK2ibEAC"}, SC.scratch_definition(False))
        self.assertNotIn("features", json.dumps(SC.scratch_definition(False)))

    def test_record_never_carries_the_token_and_delete_never_touches_the_hub(self):
        run, _ = self.runner({}, known=True)
        rec = SC.record("conf-acme", runner=run)
        self.assertEqual({"org_id", "alias", "instance_url", "expires"}, set(rec))
        self.assertNotIn(SECRET, json.dumps(rec))
        with self.assertRaises(SC.Refused):
            SC.delete(SC.HUB_ALIAS, runner=run)
        with self.assertRaises(SC.Refused):
            SC.delete("bad alias!", runner=run)


class AppRoutes(unittest.TestCase):
    origin = base.TalkLaneTests.origin

    def make(self, **overrides):
        self.store = base.MemoryStore()
        self.email_sender = base.EmailSender()
        self.now = [1000.0]
        self.targets = FakeTargets()
        self.sink = ListSink()
        parts = {"targets": self.targets, "architect": FakeArchitect(), "sink": self.sink}
        return base.create_app(settings=base.settings(**overrides), store=self.store, worker=base.CountingWorker(),
                               clock=lambda: self.now[0], id_factory=base.IDs(), voice_client=base.FakeVoiceClient(),
                               email_sender=self.email_sender, talk_client=base.FakeTalk(),
                               sf_build=parts if overrides.get("sf_build_enabled") else None)

    def sign_in(self, client, email="operator@example.com"):
        started = client.post("/v1/auth/start", headers=self.origin, json={
            "email": email, "client_key": "browser-instance-1234567890"})
        code = self.email_sender.calls[-1][1]
        return client.post("/v1/auth/verify", headers=self.origin, json={
            "challenge_id": started.json()["challenge_id"], "email": email, "code": code,
            "client_key": "browser-instance-1234567890"}).json()["token"]

    def session(self, client, token, topic="salesforce_build", cid="sf-1"):
        return client.post("/v1/session", headers={**self.origin, "Authorization": "Bearer " + token},
                           json={"creation_id": cid, "start": "blank", "topic": topic})

    def test_flag_off_is_unchanged(self):
        from workers.topics import TOPICS
        with TestClient(self.make()) as client:
            self.assertNotIn("sf_build", client.get("/health").json()["features"])
            token = self.sign_in(client)
            refused = self.session(client, token)
            self.assertEqual(400, refused.status_code)
            before = sorted(t for t in TOPICS if t != "salesforce_build")
            self.assertEqual("topic must be one of: %s" % ", ".join(before), refused.json()["detail"])
            created = self.session(client, token, topic="conference", cid="c-2").json()
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            route = client.post("/v1/session/%s/sf-build" % created["session_id"], headers=headers,
                                json={"action": "ask", "text": "x"})
            self.assertEqual(404, route.status_code)
        with self.assertRaises(RuntimeError):
            base.settings(sf_redis_enabled=True).validate()

    def test_an_operator_removed_from_the_allowlist_loses_the_lane(self):
        with TestClient(self.make(sf_build_enabled=True)) as client:
            created = self.session(client, self.sign_in(client)).json()
        later = base.create_app(settings=base.settings(sf_build_enabled=True, operator_emails=("other@example.com",)),
                                store=self.store, worker=base.CountingWorker(), clock=lambda: self.now[0],
                                id_factory=base.IDs(), voice_client=base.FakeVoiceClient(),
                                email_sender=self.email_sender, talk_client=base.FakeTalk(),
                                sf_build={"targets": self.targets, "architect": FakeArchitect(), "sink": self.sink})
        sid = created["session_id"]
        headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
        with TestClient(later) as client:
            route = client.post("/v1/session/%s/sf-build" % sid, headers=headers, json={"action": "ask", "text": ASK})
            self.assertEqual(403, route.status_code, route.text)
            said = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "c-1", "session_id": sid, "type": "utterance", "expected_version": 0,
                "transcript": ASK, "item_id": "i-1"})
            self.assertEqual(403, said.status_code, said.text)
        self.assertNotIn("sf_build", StudioRepository(self.store).load(sid).state)

    def test_visitors_never_reach_the_lane(self):
        with TestClient(self.make(sf_build_enabled=True, public_visitors=True)) as client:
            visitor = self.sign_in(client, "visitor@example.org")
            refused = self.session(client, visitor)
            self.assertEqual(403, refused.status_code, refused.text)
            created = self.session(client, visitor, topic="salesforce_data", cid="v-2").json()
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}
            route = client.post("/v1/session/%s/sf-build" % created["session_id"], headers=headers,
                                json={"action": "ask", "text": "build me something"})
            self.assertEqual(403, route.status_code)
        self.assertEqual([], self.targets.opened)

    def test_an_operator_session_runs_the_lane_through_commands_and_the_route(self):
        with TestClient(self.make(sf_build_enabled=True)) as client:
            self.assertEqual({"redis": False}, client.get("/health").json()["features"]["sf_build"])
            created = self.session(client, self.sign_in(client)).json()
            sid = created["session_id"]
            headers = {**self.origin, "Authorization": "Bearer " + created["token"]}

            def say(text, n):
                return client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                    "command_id": "cmd-%d" % n, "session_id": sid, "type": "utterance", "expected_version": 0,
                    "transcript": text, "item_id": "item-%d" % n})

            first = say(ASK, 1)
            self.assertEqual(200, first.status_code, first.text)
            self.assertIn("artifact.patch", [e["type"] for e in first.json()["events"]])
            batch = say("show me the options", 2).json()["events"]
            design = next(e for e in batch if e["type"] == "decision.batch")["payload"]
            picked = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-3", "session_id": sid, "type": "answer_batch", "expected_version": 0,
                "batch_id": design["batch_id"], "answers": [
                    {"question_id": design["questions"][0]["question_id"], "option_id": "b"},
                    {"question_id": design["questions"][1]["question_id"], "option_id": "model"}]})
            self.assertEqual(200, picked.status_code, picked.text)
            self.assertIn("model.updated", [e["type"] for e in picked.json()["events"]])
            no_scratch = say("build it", 4).json()["events"]
            self.assertIn("scratch org", no_scratch[-1]["payload"]["text"])
            attach = client.post("/v1/session/%s/sf-build" % sid, headers=headers,
                                 json={"action": "attach_scratch", "org_id": SCRATCH_ID, "alias": "conf-acme"})
            self.assertEqual(200, attach.status_code, attach.text)
            say("build it", 5)
            self.assertEqual([True], [d["check_only"] for d in self.targets.orgs["scratch"].deploys])
            state = StudioRepository(self.store).load(sid).state
            qid = state["sf_build"]["pending"]["question_id"]
            yes = client.post("/v1/session/%s/commands" % sid, headers=headers, json={
                "command_id": "cmd-6", "session_id": sid, "type": "answer", "expected_version": 0,
                "question_id": qid, "option_id": "yes"})
            self.assertEqual(200, yes.status_code, yes.text)
            self.assertEqual([True, False], [d["check_only"] for d in self.targets.orgs["scratch"].deploys])
            analyze = client.post("/v1/session/%s/analyze" % sid, headers=headers, json={"text": "x"})
            self.assertEqual(503, analyze.status_code)
            bad = client.post("/v1/session/%s/sf-build" % sid, headers=headers, json={"action": "deploy_now"})
            self.assertEqual(400, bad.status_code)
            early = client.post("/v1/session/%s/sf-build" % sid, headers=headers, json={"action": "promote"})
            self.assertEqual(409, early.status_code)
            events = client.get("/v1/session/%s/events?once=true" % sid, headers=headers).text
        self.assertNotIn(SECRET, events)
        self.assertIn("display", [r["phase"] for r in self.sink.records])


if __name__ == "__main__":
    unittest.main()
