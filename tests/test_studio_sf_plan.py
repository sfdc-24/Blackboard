"""The Salesforce build PLAN (workers/sf_plan.py), its metadata, the sink and the org-view seam.

Pure: no org, no model, no network. A plan is data a model may PROPOSE and only
sf_plan may turn into metadata; everything outside the allowlist is refused.
"""
from __future__ import annotations

import copy
import io
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))

from workers import sf_plan as P  # noqa: E402
from workers import sf_sink as S  # noqa: E402
from workers import sf_view as V  # noqa: E402
from workers import sf_views as VS  # noqa: E402

SMOKE = {"title": "Conference smoke", "objects": [{
    "api_name": "Conf_Smoke__c", "label": "Conf Smoke", "plural": "Conf Smokes",
    "fields": [{"api_name": "Note__c", "label": "Note", "type": "Text", "length": 80},
               {"api_name": "Stage__c", "label": "Stage", "type": "Picklist", "values": ["New", "Done"]}]}]}

SPEND = {"title": "Agent spend", "objects": [
    {"api_name": "Agent_Spend__c", "label": "Agent Spend", "plural": "Agent Spend",
     "name_field": {"type": "AutoNumber", "label": "Spend Number", "format": "SP-{0000}"},
     "fields": [{"api_name": "Month__c", "label": "Month", "type": "Date"},
                {"api_name": "Run_Count__c", "label": "Run Count", "type": "Summary", "operation": "count",
                 "child": "Agent_Run__c"},
                {"api_name": "Total_Cost__c", "label": "Total Cost", "type": "Summary", "operation": "sum",
                 "child": "Agent_Run__c", "child_field": "Cost__c"}]},
    {"api_name": "Agent_Run__c", "label": "Agent Run", "plural": "Agent Runs",
     "fields": [{"api_name": "Agent_Spend__c", "label": "Agent Spend", "type": "MasterDetail", "ref": "Agent_Spend__c"},
                {"api_name": "Cost__c", "label": "Cost", "type": "Currency"},
                {"api_name": "Account__c", "label": "Account", "type": "Lookup", "ref": "Account"}]},
    {"api_name": "Account", "fields": [{"api_name": "AI_Budget__c", "label": "AI Budget", "type": "Currency"}]}]}

GOLDEN_OBJECT = '''<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <deploymentStatus>Deployed</deploymentStatus>
    <enableActivities>false</enableActivities>
    <enableReports>true</enableReports>
    <fields>
        <fullName>Note__c</fullName>
        <externalId>false</externalId>
        <label>Note</label>
        <length>80</length>
        <required>false</required>
        <type>Text</type>
        <unique>false</unique>
    </fields>
    <fields>
        <fullName>Stage__c</fullName>
        <label>Stage</label>
        <required>false</required>
        <type>Picklist</type>
        <valueSet>
            <restricted>true</restricted>
            <valueSetDefinition>
                <sorted>false</sorted>
                <value>
                    <fullName>New</fullName>
                    <default>false</default>
                    <label>New</label>
                </value>
                <value>
                    <fullName>Done</fullName>
                    <default>false</default>
                    <label>Done</label>
                </value>
            </valueSetDefinition>
        </valueSet>
    </fields>
    <label>Conf Smoke</label>
    <listViews>
        <fullName>All</fullName>
        <columns>NAME</columns>
        <columns>Note__c</columns>
        <columns>Stage__c</columns>
        <filterScope>Everything</filterScope>
        <label>All</label>
    </listViews>
    <nameField>
        <label>Conf Smoke Name</label>
        <type>Text</type>
    </nameField>
    <pluralLabel>Conf Smokes</pluralLabel>
    <sharingModel>ReadWrite</sharingModel>
</CustomObject>
'''

GOLDEN_PACKAGE = '''<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>Conf_Smoke__c.Note__c</members>
        <members>Conf_Smoke__c.Stage__c</members>
        <name>CustomField</name>
    </types>
    <types>
        <members>Conf_Smoke__c</members>
        <name>CustomObject</name>
    </types>
    <types>
        <members>Conf_Smoke__c</members>
        <name>CustomTab</name>
    </types>
    <types>
        <members>Conf_Smoke__c.All</members>
        <name>ListView</name>
    </types>
    <types>
        <members>Conf_Build_Access</members>
        <name>PermissionSet</name>
    </types>
    <version>62.0</version>
</Package>
'''

GOLDEN_DESTRUCTIVE = '''<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>Conf_Smoke__c</members>
        <name>CustomObject</name>
    </types>
    <types>
        <members>Conf_Smoke__c</members>
        <name>CustomTab</name>
    </types>
</Package>
'''

GOLDEN_TAB = '''<?xml version="1.0" encoding="UTF-8"?>
<CustomTab xmlns="http://soap.sforce.com/2006/04/metadata">
    <customObject>true</customObject>
    <motif>Custom57: Building</motif>
</CustomTab>
'''


def plan_with(**change):
    raw = copy.deepcopy(SMOKE)
    raw["objects"][0].update(change)
    return raw


class Refusals(unittest.TestCase):
    def refused(self, raw, *words):
        with self.assertRaises(P.PlanError) as caught:
            P.validate_plan(raw)
        for word in words:
            self.assertIn(word, str(caught.exception))
        return str(caught.exception)

    def test_the_smoke_and_the_spend_plans_pass(self):
        self.assertEqual("Conf_Smoke__c", P.validate_plan(SMOKE)["objects"][0]["api_name"])
        self.assertEqual(3, len(P.validate_plan(SPEND)["objects"]))

    def test_apex_triggers_flows_profiles_sharing_remote_sites_are_refused(self):
        for key in ("apex", "apexClass", "trigger", "flows", "profiles", "sharingRules", "remoteSites"):
            raw = copy.deepcopy(SMOKE)
            raw[key] = [{"name": "X"}]
            self.refused(raw, "Apex")
            obj = plan_with(**{key: "x"})
            self.refused(obj, key)

    def test_an_agent_is_a_documented_future_type_never_executed(self):
        self.refused(dict(copy.deepcopy(SMOKE), agents=[{"name": "Spend Assistant"}]), "sf agent create",
                     "future component type")

    def test_a_delete_is_refused(self):
        self.refused(dict(copy.deepcopy(SMOKE), delete=["Account"]), "delete")
        self.refused(plan_with(destructive=True), "destructive")
        field = copy.deepcopy(SMOKE)
        field["objects"][0]["fields"][0]["delete"] = True
        self.refused(field, "delete")

    def test_field_types_outside_the_allowlist_are_refused(self):
        for kind in ("Formula", "ApexTrigger", "Html", "EncryptedText", "Email", None):
            raw = copy.deepcopy(SMOKE)
            raw["objects"][0]["fields"][0]["type"] = kind
            self.refused(raw, "not allowed")

    def test_standard_objects_only_gain_custom_fields_on_the_five(self):
        for obj in ("Account", "Contact", "Lead", "Opportunity", "Case"):
            P.validate_plan({"title": "x", "objects": [{"api_name": obj, "fields": [
                {"api_name": "Score__c", "label": "Score", "type": "Number"}]}]})
            self.refused({"title": "x", "objects": [{"api_name": obj, "label": "Renamed", "fields": [
                {"api_name": "Score__c", "label": "Score", "type": "Number"}]}]}, "only adding custom fields")
            self.refused({"title": "x", "objects": [{"api_name": obj, "fields": []}]}, "no fields")
        for obj in ("Campaign", "User", "Task", "Product2", "Organization"):
            self.refused({"title": "x", "objects": [{"api_name": obj, "fields": [
                {"api_name": "Score__c", "label": "Score", "type": "Number"}]}]}, "cannot be changed")
        self.refused({"title": "x", "objects": [{"api_name": "Account", "fields": [
            {"api_name": "Parent__c", "label": "P", "type": "MasterDetail", "ref": "Account"}]}]}, "detail")

    def test_too_many_objects_or_fields_are_refused(self):
        many = {"title": "x", "objects": [{"api_name": "O%d__c" % i, "label": "O %d" % i, "plural": "Os %d" % i}
                                          for i in range(P.MAX_OBJECTS + 1)]}
        self.refused(many, "1 to 6 objects")
        fields = [{"api_name": "F%d__c" % i, "label": "F %d" % i, "type": "Checkbox"} for i in range(P.MAX_FIELDS + 1)]
        self.refused(plan_with(fields=fields), "at most 60 fields")
        self.refused({"title": "x", "objects": []}, "1 to 6 objects")

    def test_bad_names_are_refused(self):
        for bad in ("Smoke", "Smoke__C", "Conf__Smoke__c", "1Smoke__c", "Smoke___c", "Sm oke__c", "_Smoke__c",
                    "Smoke__x", "A" * 38 + "__c", "Smoke__c__c", "", None):
            self.refused(plan_with(api_name=bad))
            raw = copy.deepcopy(SMOKE)
            raw["objects"][0]["fields"][0]["api_name"] = bad
            self.refused(raw)
        for label in ("", " Lead", "<b>x</b>", "a&b", "x" * 41, 7):
            self.refused(plan_with(label=label), "label")

    def test_references_are_checked(self):
        raw = copy.deepcopy(SPEND)
        raw["objects"][1]["fields"][0]["ref"] = "Nowhere__c"
        self.refused(raw, "master")
        raw = copy.deepcopy(SPEND)
        raw["objects"][1]["fields"][2]["ref"] = "Campaign"
        self.refused(raw, "neither in the plan")
        raw = copy.deepcopy(SPEND)
        raw["objects"][0]["fields"][2]["child_field"] = "Missing__c"
        self.refused(raw, "Missing__c")
        raw = copy.deepcopy(SPEND)
        del raw["objects"][1]["fields"][0]                          # no master-detail: no roll-up
        self.refused(raw, "master-detail")
        cyc = {"title": "x", "objects": [
            {"api_name": "A__c", "label": "A", "plural": "As", "fields": [
                {"api_name": "B__c", "label": "B", "type": "MasterDetail", "ref": "B__c"}]},
            {"api_name": "B__c", "label": "B", "plural": "Bs", "fields": [
                {"api_name": "A__c", "label": "A", "type": "MasterDetail", "ref": "A__c"}]}]}
        self.refused(cyc, "cycle")


class Metadata(unittest.TestCase):
    def test_golden_object_package_tab_and_destructive(self):
        pkg = P.build_package(P.validate_plan(SMOKE))
        self.assertEqual(GOLDEN_OBJECT, pkg["files"]["objects/Conf_Smoke__c.object"])
        self.assertEqual(GOLDEN_PACKAGE, pkg["package_xml"])
        self.assertEqual(GOLDEN_PACKAGE, pkg["files"]["package.xml"])
        self.assertEqual(GOLDEN_TAB, pkg["files"]["tabs/Conf_Smoke__c.tab"])
        self.assertEqual(GOLDEN_DESTRUCTIVE, pkg["destructive_xml"])
        self.assertEqual(6, pkg["components"])

    def test_detail_objects_take_their_masters_sharing_and_rollups_name_the_link(self):
        pkg = P.build_package(P.validate_plan(SPEND))
        run = pkg["files"]["objects/Agent_Run__c.object"]
        self.assertIn("<sharingModel>ControlledByParent</sharingModel>", run)
        spend = pkg["files"]["objects/Agent_Spend__c.object"]
        self.assertIn("<sharingModel>ReadWrite</sharingModel>", spend)
        self.assertIn("<summaryForeignKey>Agent_Run__c.Agent_Spend__c</summaryForeignKey>", spend)
        self.assertIn("<summarizedField>Agent_Run__c.Cost__c</summarizedField>", spend)
        self.assertIn("<displayFormat>SP-{0000}</displayFormat>", spend)
        account = pkg["files"]["objects/Account.object"]
        self.assertNotIn("<label>Account</label>", account)          # fields only on a standard object
        self.assertNotIn("<sharingModel>", account)

    def test_the_permission_set_grants_crud_and_fls_on_what_the_plan_creates_only(self):
        perm = P.build_package(P.validate_plan(SPEND))["files"]["permissionsets/Conf_Build_Access.permissionset"]
        for obj in ("Agent_Spend__c", "Agent_Run__c"):
            self.assertIn("<object>%s</object>" % obj, perm)
            self.assertIn("<tab>%s</tab>" % obj, perm)
        self.assertNotIn("<object>Account</object>", perm)
        self.assertIn("<field>Account.AI_Budget__c</field>", perm)
        self.assertNotIn("Agent_Run__c.Agent_Spend__c</field>", perm)   # master-detail is always required
        self.assertIn("<editable>false</editable>\n        <field>Agent_Spend__c.Total_Cost__c</field>", perm)
        self.assertIn("<modifyAllRecords>false</modifyAllRecords>", perm)

    def test_destructive_changes_mirror_the_package(self):
        """Every component the build creates is undone, except what goes with its object
        (the new object's own fields and list view) and the kept permission set."""
        for raw in (SMOKE, SPEND):
            pkg = P.build_package(P.validate_plan(raw))
            new = set(pkg["new_objects"])
            expected = {"CustomObject": set(pkg["types"].get("CustomObject", [])),
                        "CustomTab": set(pkg["types"].get("CustomTab", [])),
                        "CustomField": {f for f in pkg["types"].get("CustomField", []) if f.split(".")[0] not in new}}
            got = {k: set(v) for k, v in pkg["undo_types"].items()}
            self.assertEqual({k: v for k, v in expected.items() if v}, got)
            self.assertEqual(set(pkg["types"]["ListView"]), {o + ".All" for o in new})
            self.assertNotIn("PermissionSet", pkg["destructive_xml"])
            undo = zipfile.ZipFile(io.BytesIO(P.destructive_zip(pkg["destructive_xml"])))
            self.assertEqual({"package.xml", "destructiveChanges.xml"}, set(undo.namelist()))
            self.assertNotIn("<types>", undo.read("package.xml").decode())

    def test_the_zip_holds_exactly_the_files_and_is_stable(self):
        pkg = P.build_package(P.validate_plan(SPEND))
        first, second = P.package_zip(pkg), P.package_zip(P.build_package(P.validate_plan(SPEND)))
        self.assertEqual(first, second)
        names = set(zipfile.ZipFile(io.BytesIO(first)).namelist())
        self.assertEqual(set(pkg["files"]), names)
        self.assertIn("package.xml", names)

    def test_a_later_plan_deploys_adds_only_and_refuses_removals_and_changes(self):
        plan = P.validate_plan(SMOKE)
        first = P.build_package(plan)
        built = P.record_built(None, plan, first["change"])
        with self.assertRaises(P.PlanError):
            P.build_package(plan, built)                                  # nothing new
        more = copy.deepcopy(SMOKE)
        more["objects"][0]["fields"].append({"api_name": "Score__c", "label": "Score", "type": "Number"})
        second = P.build_package(P.validate_plan(more), built)
        self.assertEqual(["Conf_Smoke__c.Score__c"], second["new_fields"])
        self.assertEqual([], second["new_objects"])
        self.assertEqual({"CustomField": ["Conf_Smoke__c.Score__c"]}, second["undo_types"])
        self.assertNotIn("<label>Conf Smoke</label>", second["files"]["objects/Conf_Smoke__c.object"])
        fewer = copy.deepcopy(SMOKE)
        fewer["objects"][0]["fields"].pop()
        with self.assertRaises(P.PlanError) as caught:
            P.delta(P.validate_plan(fewer), built)
        self.assertIn("undo", str(caught.exception))
        changed = copy.deepcopy(SMOKE)
        changed["objects"][0]["fields"][0]["length"] = 90
        with self.assertRaises(P.PlanError):
            P.delta(P.validate_plan(changed), built)
        self.assertEqual(P.empty_built(), P.forget_built(built, first["change"]))


class Views(unittest.TestCase):
    def test_the_proposed_model_marks_everything_proposed_and_fits_the_contract(self):
        model = VS.proposed_model(P.validate_plan(SPEND))
        self.assertTrue(model["domain"].startswith("Proposed:"))
        self.assertEqual({"domain", "objects", "relationships", "findings"}, set(model))
        for obj in model["objects"]:
            self.assertEqual({"id", "name", "standard", "purpose", "fields"}, set(obj))
            for f in obj["fields"]:
                self.assertTrue(f["type"].endswith("- proposed"), f)
        kinds = {(r["from"], r["to"], r["kind"]) for r in model["relationships"]}
        self.assertIn(("Agent_Run__c", "Agent_Spend__c", "master-detail"), kinds)
        self.assertIn(("Agent_Run__c", "Account", "lookup"), kinds)

    def test_the_prototype_draws_a_record_page_and_a_list_view_in_rendered_kinds(self):
        import datetime
        nodes = VS.prototype_nodes(P.validate_plan(SPEND), datetime.date(2026, 10, 9))
        self.assertEqual(["sfb-rec-Agent_Spend__c", "sfb-list-Agent_Run__c"], [n["id"] for n in nodes])
        self.assertIn("Agent Spend - Record Page", nodes[0]["label"])
        self.assertIn("Agent Runs - List View", nodes[1]["label"])
        allowed = {"section", "heading", "card", "list", "text", "button"}

        def walk(n):
            self.assertIn(n["kind"], allowed)
            for c in n.get("children") or []:
                walk(c)
        for n in nodes:
            self.assertEqual("heading", n["children"][0]["kind"])
            walk(n)

    def test_the_architecture_is_a_titled_flow(self):
        node = VS.architecture_node({"title": "Agent spend flow", "steps": [
            {"id": "api", "label": "API calls", "detail": "d"}, {"id": "cap", "label": "Capture", "detail": "d"},
            {"id": "store", "label": "Store", "detail": "d"}],
            "edges": [{"from": "api", "to": "cap", "label": "usage event"},
                      {"from": "cap", "to": "store", "label": "insert"}]})
        kinds = [c["kind"] for c in node["children"]]
        self.assertEqual(["heading", "process-step", "edge", "process-step", "edge", "process-step"], kinds)
        self.assertEqual("API calls -> Capture", node["children"][2]["detail"])


class SinkAndView(unittest.TestCase):
    def test_a_record_is_a_closed_flat_string_map(self):
        rec = S.record("s-1", 3, "build", "validated", plan_hash="h", objects=["A__c", "B__c"], components=5, now=0)
        self.assertEqual(set(S.FIELDS), set(rec))
        self.assertTrue(all(isinstance(v, str) for v in rec.values()))
        self.assertEqual("A__c,B__c", rec["objects"])
        self.assertEqual("1970-01-01T00:00:00Z", rec["at"])

    def test_the_redis_sink_writes_the_documented_keys_with_a_ttl(self):
        calls = []

        class Pipe:
            def __getattr__(self, name):
                return lambda *a, **k: calls.append((name, a, k))

        class Client:
            def pipeline(self, transaction=False):
                return Pipe()

        S.RedisSink(Client()).publish(S.record("s-1", 1, "options", "options_offered", now=0))
        names = [c[0] for c in calls]
        self.assertEqual(["xadd", "expire", "hset", "expire", "execute"], names)
        self.assertEqual("conf:sf:s-1:events", calls[0][1][0])
        self.assertEqual("conf:sf:s-1:state", calls[2][1][0])
        self.assertEqual(7 * 24 * 3600, calls[1][1][1])
        self.assertEqual(500, calls[0][2]["maxlen"])

    def test_a_redis_failure_never_reaches_the_lane_and_never_logs_the_host(self):
        import contextlib
        out = io.StringIO()
        sink = S.RedisSink(environ={"REDIS_HOST": "10.9.8.7", "REDIS_CA_CERT_PATH": "", "REDIS_AUTH_FILE": ""})
        with contextlib.redirect_stdout(out):
            sink.publish(S.record("s-1", 1, "build", "deployed", now=0))
        self.assertEqual(1, sink.failures)
        self.assertNotIn("10.9.8.7", out.getvalue())

    def test_the_view_url_for_each_kind(self):
        host = "acme-dev-ed.scratch.my.salesforce.com"
        self.assertEqual("https://acme-dev-ed.scratch.lightning.force.com/lightning/setup/ObjectManager/"
                         "Agent_Spend__c/Details/view", V.view_url(host, "object", "Agent_Spend__c"))
        self.assertEqual("https://acme-dev-ed.scratch.lightning.force.com/lightning/o/Agent_Run__c/list"
                         "?filterName=All", V.view_url(host, "list", "Agent_Run__c"))
        self.assertEqual("https://acme-dev-ed.scratch.lightning.force.com/lightning/r/Agent_Run__c/"
                         "a01000000000001AAA/view", V.view_url(host, "record", "Agent_Run__c", "a01000000000001AAA"))
        for bad in (("evil.example.com", "object", "A__c", ""), (host, "record", "A__c", ""),
                    (host, "setup", "A__c", ""), (host, "object", "A__c; x", "")):
            with self.assertRaises(ValueError):
                V.view_url(*bad)

    def test_a_view_never_carries_a_session(self):
        with self.assertRaises(ValueError):
            V.view_request("scratch", "object", "x", "https://x.scratch.lightning.force.com/secur/frontdoor.jsp?sid=1")
        for image in ("https://x.example.com/p.png?sid=abc", "javascript:alert(1)", ""):
            with self.assertRaises(ValueError):
                V.org_view_event("scratch", "record", "x", image, "t")
        ok = V.org_view_event("scratch", "list", "Runs", "data:image/png;base64," + "A" * 40, "2026-10-09T07:00:00Z")
        self.assertEqual({"target", "kind", "label", "image", "at"}, set(ok))

    def test_captures_are_throttled_to_one_every_three_seconds(self):
        now = [0.0]
        sent = []
        viewer = V.ThrottledViewer(lambda sid, req: sent.append(req), clock=lambda: now[0])
        req = V.view_request("scratch", "object", "x", "https://a.scratch.lightning.force.com/lightning/o/A__c/list")
        self.assertTrue(viewer.request("s", req))
        now[0] = 2.9
        self.assertFalse(viewer.request("s", req))
        self.assertTrue(viewer.request("other", req))
        now[0] = 3.1
        self.assertTrue(viewer.request("s", req))
        self.assertEqual(3, len(sent))


if __name__ == "__main__":
    unittest.main()
