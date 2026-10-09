"""The Salesforce build PLAN: an allowlisted, typed spec, and the metadata it becomes.

A plan is data a model may PROPOSE and only this module may turn into metadata.
It is closed: every key is known, every name is checked, every type is on the
allowlist below. Anything else - Apex, triggers, flows, profiles, sharing rules,
remote sites, a delete, a change to a standard object beyond ADDING a custom
field to Account, Contact, Lead, Opportunity or Case - is refused with a reason.

    plan = validate_plan(raw)              # the normalized plan, or PlanError
    pkg = build_package(plan, built)       # package.xml, files, destructiveChanges.xml
    zip_bytes = package_zip(pkg)           # what the Metadata API deploys
    undo_zip = destructive_zip(pkg)        # the rollback, stored with the session

`built` is what earlier builds in this session created ({"objects": {...},
"fields": {...}}); a later plan deploys only what is new (adds only). Removing
or changing something already built is refused here: that is what undo is for.

Plan shape (normalized; every optional key filled in, so the hash is stable):
    {"title": "Agent spend",
     "objects": [
        {"api_name": "Agent_Spend__c", "label": "Agent Spend", "plural": "Agent Spend",
         "description": "", "name_field": {"type": "AutoNumber", "label": "Spend Number",
         "format": "SP-{0000}"},
         "fields": [{"api_name": "Month__c", "label": "Month", "type": "Date", "description": ""}]},
        {"api_name": "Account", "fields": [ ... ]}          # an extension: fields only
     ]}
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from xml.sax.saxutils import escape

API_VERSION = "62.0"
NS = "http://soap.sforce.com/2006/04/metadata"
PERMSET = "Conf_Build_Access"
PERMSET_LABEL = "Conf Build Access"
EXTENDABLE = ("Account", "Contact", "Lead", "Opportunity", "Case")
LOOKUP_STANDARD = EXTENDABLE + ("User",)
MASTER_STANDARD = ("Account", "Contact", "Opportunity", "Case")
MAX_OBJECTS = 6
MAX_FIELDS = 60
MAX_MASTER_DETAIL = 2
TAB_MOTIF = "Custom57: Building"
# A stem of letters, digits and single underscores, then __c; 40 characters in all.
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*__c")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ,.()/:%#+-]*")
FORMAT_RE = re.compile(r"[A-Za-z0-9 -]{0,20}\{0{1,10}\}[A-Za-z0-9 -]{0,10}")
FIELD_TYPES = ("Text", "Number", "Currency", "Percent", "Date", "DateTime", "Checkbox",
               "Picklist", "LongTextArea", "Lookup", "MasterDetail", "Summary")
TYPE_KEYS = {
    "Text": {"length"}, "Number": {"precision", "scale"}, "Currency": {"precision", "scale"},
    "Percent": {"precision", "scale"}, "Date": set(), "DateTime": set(), "Checkbox": {"default"},
    "Picklist": {"values"}, "LongTextArea": {"length"}, "Lookup": {"ref"}, "MasterDetail": {"ref"},
    "Summary": {"operation", "child", "child_field"},
}
COMMON_FIELD_KEYS = {"api_name", "label", "type", "description"}
NEW_OBJECT_KEYS = {"api_name", "label", "plural", "description", "name_field", "fields"}
SUMMARY_OPS = ("count", "sum", "min", "max")
# Words that name what a plan may never carry. Used only to word the refusal:
# the closed shape refuses them whatever they are called.
FORBIDDEN = ("apex", "class", "trigger", "flow", "process", "workflow", "profile", "sharing",
             "remote", "delete", "destructive", "validation", "layout", "permission", "user")


# Documented FUTURE component types: named so a plan carrying one is refused with the path it
# will take, never executed today. An agent goes through Agentforce DX in the sf CLI, against the
# session's scratch org - never custom code.
FUTURE_TYPES = {
    "agents": "an agent is a future component type, not built yet: it will go through Agentforce DX "
              "(sf agent generate agent-spec, sf agent create, sf agent preview / sf agent test) in the "
              "scratch org, which needs Einstein1AIPlatform and the agent platform settings",
}


class PlanError(ValueError):
    """The plan is refused; the message says why, in words the owner can act on."""


def _closed(value: dict, allowed: set, where: str) -> None:
    extra = sorted(set(value) - allowed)
    if not extra:
        return
    named = [k for k in extra if any(word in str(k).lower() for word in FORBIDDEN)]
    if named:
        raise PlanError("%s: %s refused - a plan never carries Apex, triggers, flows, profiles, sharing "
                        "rules, remote sites, layouts or deletes" % (where, ", ".join(map(str, named))))
    raise PlanError("%s has keys that are not allowed: %s" % (where, ", ".join(map(str, extra))))


def _label(value, where: str, cap: int = 40) -> str:
    if (not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= cap
            or not LABEL_RE.fullmatch(value)):
        raise PlanError("%s must be 1-%d plain characters (letters, digits, spaces, , . ( ) / : %% # + -)"
                        % (where, cap))
    return value


def _description(value, where: str) -> str:
    if value in (None, ""):
        return ""
    return _label(value, where + " description", 255)


def _api_name(value, where: str) -> str:
    if not isinstance(value, str) or len(value) > 40 or not NAME_RE.fullmatch(value):
        raise PlanError("%s: %r is not a custom API name (letters, digits and single underscores, "
                        "ending __c, at most 40 characters)" % (where, value))
    return value


def _int(value, lo: int, hi: int, where: str) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise PlanError("%s must be a whole number from %d to %d" % (where, lo, hi))
    return value


def _field(raw, obj: str, extension: bool) -> dict:
    where = "%s field" % obj
    if not isinstance(raw, dict):
        raise PlanError(where + " must be an object")
    kind = raw.get("type")
    if kind not in FIELD_TYPES:
        raise PlanError("%s %r: type %r is not allowed (allowed: %s)"
                        % (where, raw.get("api_name"), kind, ", ".join(FIELD_TYPES)))
    _closed(raw, COMMON_FIELD_KEYS | TYPE_KEYS[kind], where + " %r" % raw.get("api_name"))
    name = _api_name(raw.get("api_name"), where)
    where = "%s.%s" % (obj, name)
    out = {"api_name": name, "label": _label(raw.get("label"), where + " label"), "type": kind,
           "description": _description(raw.get("description"), where)}
    if kind == "Text":
        out["length"] = _int(raw.get("length", 255), 1, 255, where + " length")
    elif kind in ("Number", "Currency", "Percent"):
        default = 2 if kind != "Number" else 0
        out["precision"] = _int(raw.get("precision", 18), 1, 18, where + " precision")
        out["scale"] = _int(raw.get("scale", default), 0, min(out["precision"], 6), where + " scale")
    elif kind == "Checkbox":
        default = raw.get("default", False)
        if type(default) is not bool:
            raise PlanError(where + " default must be true or false")
        out["default"] = default
    elif kind == "Picklist":
        values = raw.get("values")
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            raise PlanError(where + " needs 1 to 50 picklist values")
        clean = [_label(v, where + " value") for v in values]
        if len({v.lower() for v in clean}) != len(clean):
            raise PlanError(where + " has duplicate picklist values")
        out["values"] = clean
    elif kind == "LongTextArea":
        out["length"] = _int(raw.get("length", 32768), 256, 131072, where + " length")
    elif kind in ("Lookup", "MasterDetail"):
        ref = raw.get("ref")
        if not isinstance(ref, str) or not ref:
            raise PlanError(where + " needs ref, the object it points to")
        if kind == "MasterDetail" and extension:
            raise PlanError(where + ": a standard object cannot be the detail of a master-detail")
        out["ref"] = ref
    elif kind == "Summary":
        op = raw.get("operation")
        if op not in SUMMARY_OPS:
            raise PlanError(where + " operation must be one of " + ", ".join(SUMMARY_OPS))
        out["operation"] = op
        out["child"] = _api_name(raw.get("child"), where + " child")
        child_field = raw.get("child_field", "")
        if op == "count":
            if child_field not in ("", None):
                raise PlanError(where + ": a count roll-up takes no child_field")
            out["child_field"] = ""
        else:
            out["child_field"] = _api_name(child_field, where + " child_field")
    return out


def validate_plan(raw) -> dict:
    """The normalized plan, or PlanError naming the first thing refused."""
    if not isinstance(raw, dict):
        raise PlanError("a plan must be an object")
    for key, why in FUTURE_TYPES.items():
        if key in raw:
            raise PlanError(why)
    _closed(raw, {"title", "objects"}, "plan")
    title = _label(raw.get("title"), "plan title", 80)
    objects = raw.get("objects")
    if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_OBJECTS:
        raise PlanError("a plan has 1 to %d objects" % MAX_OBJECTS)
    out, seen = [], set()
    total_fields = 0
    for raw_obj in objects:
        if not isinstance(raw_obj, dict):
            raise PlanError("each object must be an object")
        api = raw_obj.get("api_name")
        if not isinstance(api, str):
            raise PlanError("each object needs an api_name")
        if api.lower() in seen:
            raise PlanError("object %s appears twice" % api)
        seen.add(api.lower())
        if not api.endswith("__c"):
            if api not in EXTENDABLE:
                raise PlanError("standard object %s cannot be changed; only custom fields may be added "
                                "to %s" % (api, ", ".join(EXTENDABLE)))
            _closed(raw_obj, {"api_name", "fields"}, "standard object %s (only adding custom fields "
                                                     "is allowed)" % api)
            obj = {"api_name": api, "fields": []}
            extension = True
        else:
            _closed(raw_obj, NEW_OBJECT_KEYS, "object %s" % api)
            _api_name(api, "object")
            label = _label(raw_obj.get("label"), "object %s label" % api)
            nf = raw_obj.get("name_field") or {"type": "Text", "label": (label + " Name")[:40]}
            if not isinstance(nf, dict):
                raise PlanError("object %s name_field must be an object" % api)
            _closed(nf, {"type", "label", "format"}, "object %s name_field" % api)
            if nf.get("type") not in ("Text", "AutoNumber"):
                raise PlanError("object %s name field must be Text or AutoNumber" % api)
            name_field = {"type": nf["type"], "label": _label(nf.get("label"), "object %s name field label" % api)}
            if nf["type"] == "AutoNumber":
                fmt = nf.get("format")
                if not isinstance(fmt, str) or not FORMAT_RE.fullmatch(fmt):
                    raise PlanError("object %s AutoNumber format must look like AB-{0000}" % api)
                name_field["format"] = fmt
            elif "format" in nf:
                raise PlanError("object %s: only an AutoNumber name field has a format" % api)
            obj = {"api_name": api, "label": label,
                   "plural": _label(raw_obj.get("plural"), "object %s plural label" % api),
                   "description": _description(raw_obj.get("description"), "object %s" % api),
                   "name_field": name_field, "fields": []}
            extension = False
        fields = raw_obj.get("fields") if "fields" in raw_obj else []
        if not isinstance(fields, list):
            raise PlanError("object %s fields must be a list" % api)
        if extension and not fields:
            raise PlanError("standard object %s is listed with no fields to add" % api)
        names = set()
        for raw_field in fields:
            field = _field(raw_field, api, extension)
            if field["api_name"].lower() in names:
                raise PlanError("%s.%s appears twice" % (api, field["api_name"]))
            names.add(field["api_name"].lower())
            obj["fields"].append(field)
        total_fields += len(obj["fields"])
        out.append(obj)
    if total_fields > MAX_FIELDS:
        raise PlanError("a plan has at most %d fields; this one has %d" % (MAX_FIELDS, total_fields))
    plan = {"title": title, "objects": out}
    _check_references(plan)
    return plan


def _check_references(plan: dict) -> None:
    by_name = {o["api_name"]: o for o in plan["objects"]}
    new = {name for name, o in by_name.items() if name.endswith("__c")}
    masters: dict = {}
    rel_names: dict = {}
    for obj in plan["objects"]:
        md = 0
        for f in obj["fields"]:
            where = "%s.%s" % (obj["api_name"], f["api_name"])
            if f["type"] == "Lookup":
                if f["ref"] not in new and f["ref"] not in LOOKUP_STANDARD:
                    raise PlanError("%s looks up %s, which is neither in the plan nor one of %s"
                                    % (where, f["ref"], ", ".join(LOOKUP_STANDARD)))
            elif f["type"] == "MasterDetail":
                md += 1
                if f["ref"] == obj["api_name"]:
                    raise PlanError(where + ": an object cannot be its own master")
                if f["ref"] not in new and f["ref"] not in MASTER_STANDARD:
                    raise PlanError("%s: the master %s must be in the plan or one of %s"
                                    % (where, f["ref"], ", ".join(MASTER_STANDARD)))
                masters.setdefault(obj["api_name"], []).append(f["ref"])
            if f["type"] in ("Lookup", "MasterDetail"):
                rel = relationship_name(obj["api_name"], f["api_name"])
                key = (f["ref"], rel.lower())
                if key in rel_names:
                    raise PlanError(where + ": its relationship name collides with " + rel_names[key])
                rel_names[key] = where
        if md > MAX_MASTER_DETAIL:
            raise PlanError("%s has more than %d master-detail fields" % (obj["api_name"], MAX_MASTER_DETAIL))
    # No master-detail cycle (A detail of B detail of A).
    def climb(name, path):
        for parent in masters.get(name, []):
            if parent in path:
                raise PlanError("master-detail cycle through " + " -> ".join(path + [parent]))
            climb(parent, path + [parent])
    for name in masters:
        climb(name, [name])
    for obj in plan["objects"]:
        for f in obj["fields"]:
            if f["type"] != "Summary":
                continue
            where = "%s.%s" % (obj["api_name"], f["api_name"])
            child = by_name.get(f["child"])
            if child is None or not f["child"].endswith("__c"):
                raise PlanError(where + ": the roll-up child must be a new object in the plan")
            links = [c for c in child["fields"] if c["type"] == "MasterDetail" and c["ref"] == obj["api_name"]]
            if len(links) != 1:
                raise PlanError("%s: %s needs exactly one master-detail to %s for a roll-up"
                                % (where, f["child"], obj["api_name"]))
            if f["operation"] != "count":
                target = next((c for c in child["fields"] if c["api_name"] == f["child_field"]), None)
                kinds = ("Number", "Currency", "Percent") + (("Date", "DateTime") if f["operation"] != "sum" else ())
                if target is None or target["type"] not in kinds:
                    raise PlanError("%s: %s.%s must be a %s field" % (where, f["child"], f["child_field"],
                                                                      "/".join(kinds)))


def relationship_name(obj: str, field: str) -> str:
    return (obj[:-3] + "_" + field[:-3])[:40].rstrip("_")


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Increments: a later plan deploys only what is new; nothing built is removed
# or changed outside undo.

def empty_built() -> dict:
    return {"objects": {}, "fields": {}}


def delta(plan: dict, built: dict | None) -> dict:
    """What this plan adds to what is already built: {"objects": [new object dicts with only
    their new fields], "new_objects": [...], "new_fields": ["Obj.Field", ...]}. PlanError when
    the plan drops or changes anything built."""
    built = built or empty_built()
    b_objects, b_fields = built.get("objects") or {}, built.get("fields") or {}
    in_plan_objects = {o["api_name"]: o for o in plan["objects"]}
    in_plan_fields = {"%s.%s" % (o["api_name"], f["api_name"]): f for o in plan["objects"] for f in o["fields"]}
    for name, header in b_objects.items():
        current = in_plan_objects.get(name)
        if current is None:
            raise PlanError("%s is already built and is not in this plan; removing it is only through undo" % name)
        if _header(current) != header:
            raise PlanError("%s is already built; changing its label, name field or description needs "
                            "undo first" % name)
    for name, definition in b_fields.items():
        if name not in in_plan_fields:
            raise PlanError("%s is already built and is not in this plan; removing it is only through undo" % name)
        if in_plan_fields[name] != definition:
            raise PlanError("%s is already built; changing it needs undo first" % name)
    objects, new_objects, new_fields = [], [], []
    for obj in plan["objects"]:
        fields = [f for f in obj["fields"] if "%s.%s" % (obj["api_name"], f["api_name"]) not in b_fields]
        is_new = obj["api_name"].endswith("__c") and obj["api_name"] not in b_objects
        if not is_new and not fields:
            continue
        part = dict(obj, fields=fields)
        objects.append(part)
        if is_new:
            new_objects.append(obj["api_name"])
        new_fields.extend("%s.%s" % (obj["api_name"], f["api_name"]) for f in fields)
    return {"objects": objects, "new_objects": new_objects, "new_fields": new_fields}


def _header(obj: dict) -> dict:
    return {k: obj[k] for k in ("label", "plural", "description", "name_field") if k in obj}


def record_built(built: dict | None, plan: dict, change: dict) -> dict:
    """`built` after `change` (a delta of `plan`) deployed."""
    out = {"objects": dict((built or {}).get("objects") or {}), "fields": dict((built or {}).get("fields") or {})}
    by_name = {o["api_name"]: o for o in plan["objects"]}
    for name in change["new_objects"]:
        out["objects"][name] = _header(by_name[name])
    for full in change["new_fields"]:
        obj, field = full.split(".", 1)
        out["fields"][full] = next(f for f in by_name[obj]["fields"] if f["api_name"] == field)
    return out


def forget_built(built: dict, change: dict) -> dict:
    """`built` after the build that made `change` was undone."""
    out = {"objects": dict(built.get("objects") or {}), "fields": dict(built.get("fields") or {})}
    gone = set(change.get("new_objects") or [])
    for name in gone:
        out["objects"].pop(name, None)
    for full in list(out["fields"]):
        if full in (change.get("new_fields") or []) or full.split(".", 1)[0] in gone:
            out["fields"].pop(full, None)
    return out


# ---------------------------------------------------------------------------
# Metadata XML. Element order follows the Metadata API WSDL sequence.

def _el(tag: str, value) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    return "<%s>%s</%s>" % (tag, escape(str(value)), tag)


def _doc(root: str, body: list, indent: str = "    ") -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<%s xmlns="%s">' % (root, NS)]
    lines.extend(indent + line for line in body)
    lines.append("</%s>" % root)
    return "\n".join(lines) + "\n"


def _block(tag: str, inner: list) -> list:
    return ["<%s>" % tag] + ["    " + line for line in inner] + ["</%s>" % tag]


def field_xml(obj: str, f: dict) -> list:
    """One <fields> block, in WSDL order."""
    kind = f["type"]
    inner = [_el("fullName", f["api_name"])]
    if kind == "Checkbox":
        inner.append(_el("defaultValue", f["default"]))
    if kind == "Lookup":
        inner.append(_el("deleteConstraint", "SetNull"))
    if f.get("description"):
        inner.append(_el("description", f["description"]))
    if kind in ("Text", "Number"):
        inner.append(_el("externalId", False))
    inner.append(_el("label", f["label"]))
    if kind in ("Text", "LongTextArea"):
        inner.append(_el("length", f["length"]))
    if kind in ("Number", "Currency", "Percent"):
        inner.append(_el("precision", f["precision"]))
    if kind in ("Lookup", "MasterDetail"):
        inner.append(_el("referenceTo", f["ref"]))
        inner.append(_el("relationshipLabel", _plural_label(obj, f)))
        inner.append(_el("relationshipName", relationship_name(obj, f["api_name"])))
    if kind == "MasterDetail":
        inner.append(_el("relationshipOrder", f.get("_order", 0)))
        inner.append(_el("reparentableMasterDetail", False))
    if kind in ("Text", "Number", "Currency", "Percent", "Date", "DateTime", "Picklist", "Lookup"):
        inner.append(_el("required", False))
    if kind in ("Number", "Currency", "Percent"):
        inner.append(_el("scale", f["scale"]))
    if kind == "Summary":
        if f["operation"] != "count":
            inner.append(_el("summarizedField", "%s.%s" % (f["child"], f["child_field"])))
        inner.append(_el("summaryForeignKey", "%s.%s" % (f["child"], f["_link"])))
        inner.append(_el("summaryOperation", f["operation"]))
    inner.append(_el("type", kind))
    if kind in ("Text", "Number"):
        inner.append(_el("unique", False))
    if kind == "Picklist":
        values = []
        for v in f["values"]:
            values += _block("value", [_el("fullName", v), _el("default", False), _el("label", v)])
        inner += _block("valueSet", [_el("restricted", True)]
                        + _block("valueSetDefinition", [_el("sorted", False)] + values))
    if kind == "LongTextArea":
        inner.append(_el("visibleLines", 3))
    if kind == "MasterDetail":
        inner.append(_el("writeRequiresMasterRead", False))
    return _block("fields", inner)


def _plural_label(obj: str, f: dict) -> str:
    stem = obj[:-3].replace("_", " ")
    return (stem + "s")[:40] if not stem.endswith("s") else stem[:40]


def _prepared_fields(obj: dict, plan: dict) -> list:
    """Fields with the derived values the XML needs (MD order, roll-up link field)."""
    by_name = {o["api_name"]: o for o in plan["objects"]}
    out, md = [], 0
    for f in obj["fields"]:
        f = dict(f)
        if f["type"] == "MasterDetail":
            f["_order"] = md
            md += 1
        if f["type"] == "Summary":
            child = by_name[f["child"]]
            f["_link"] = next(c["api_name"] for c in child["fields"]
                              if c["type"] == "MasterDetail" and c["ref"] == obj["api_name"])
        out.append(f)
    return out


def list_columns(obj: dict) -> list:
    return ["NAME"] + [f["api_name"] for f in obj["fields"] if f["type"] != "LongTextArea"][:5]


def object_xml(obj: dict, plan: dict, *, new: bool, fields: list | None = None) -> str:
    """objects/<Name>.object: the whole object when new, else only the fields being added."""
    full = next(o for o in plan["objects"] if o["api_name"] == obj["api_name"])
    prepared = {f["api_name"]: f for f in _prepared_fields(full, plan)}
    adding = [prepared[f["api_name"]] for f in (fields if fields is not None else obj["fields"])]
    body: list = []
    if not new:
        for f in adding:
            body += field_xml(obj["api_name"], f)
        return _doc("CustomObject", body)
    detail = any(f["type"] == "MasterDetail" for f in full["fields"])
    body.append(_el("deploymentStatus", "Deployed"))
    if full["description"]:
        body.append(_el("description", full["description"]))
    body.append(_el("enableActivities", False))
    body.append(_el("enableReports", True))
    for f in adding:
        body += field_xml(obj["api_name"], f)
    body.append(_el("label", full["label"]))
    body += _block("listViews", [_el("fullName", "All")]
                   + [_el("columns", c) for c in list_columns(full)]
                   + [_el("filterScope", "Everything"), _el("label", "All")])
    nf = full["name_field"]
    name_inner = []
    if nf["type"] == "AutoNumber":
        name_inner.append(_el("displayFormat", nf["format"]))
    name_inner += [_el("label", nf["label"]), _el("type", nf["type"])]
    body += _block("nameField", name_inner)
    body.append(_el("pluralLabel", full["plural"]))
    # A detail object's sharing is its master's: Salesforce refuses ReadWrite there.
    body.append(_el("sharingModel", "ControlledByParent" if detail else "ReadWrite"))
    return _doc("CustomObject", body)


def tab_xml() -> str:
    return _doc("CustomTab", [_el("customObject", True), _el("motif", TAB_MOTIF)])


def permset_xml(plan: dict) -> str:
    """Conf_Build_Access: CRUD on every new object, FLS on every created field, the tabs
    visible. Cumulative over the whole plan, so a later build never narrows it."""
    fields, objects, tabs = [], [], []
    for obj in plan["objects"]:
        for f in obj["fields"]:
            if f["type"] == "MasterDetail":
                continue                    # always required: FLS cannot be set on it
            fields += _block("fieldPermissions", [
                _el("editable", f["type"] != "Summary"),
                _el("field", "%s.%s" % (obj["api_name"], f["api_name"])),
                _el("readable", True)])
        if obj["api_name"].endswith("__c"):
            objects += _block("objectPermissions", [
                _el("allowCreate", True), _el("allowDelete", True), _el("allowEdit", True),
                _el("allowRead", True), _el("modifyAllRecords", False),
                _el("object", obj["api_name"]), _el("viewAllRecords", False)])
            tabs += _block("tabSettings", [_el("tab", obj["api_name"]), _el("visibility", "Visible")])
    body = [_el("description", "Access to what the conference build lane created. Safe to keep.")]
    body += fields
    body.append(_el("hasActivationRequired", False))
    body.append(_el("label", PERMSET_LABEL))
    body += objects
    body += tabs
    return _doc("PermissionSet", body)


def manifest_xml(types: dict, *, root: str = "Package", version: bool = True) -> str:
    body: list = []
    for name in sorted(types):
        members = sorted(types[name])
        if members:
            body += _block("types", [_el("members", m) for m in members] + [_el("name", name)])
    if version:
        body.append(_el("version", API_VERSION))
    return _doc(root, body)


def build_package(plan: dict, built: dict | None = None) -> dict:
    """Everything one build deploys, and the destructiveChanges.xml that undoes exactly it.

    The destructive manifest mirrors the package: every component this build creates,
    except what goes with its object (the new object's own fields and list view) and the
    permission set, which is kept (it is harmless and may predate this session)."""
    change = delta(plan, built)
    if not change["objects"]:
        raise PlanError("nothing new to build: everything in this plan is already in the org")
    files: dict = {}
    types: dict = {"CustomObject": [], "CustomField": [], "ListView": [], "CustomTab": [],
                   "PermissionSet": [PERMSET]}
    undo: dict = {"CustomObject": [], "CustomField": [], "CustomTab": []}
    for obj in change["objects"]:
        name = obj["api_name"]
        is_new = name in change["new_objects"]
        files["objects/%s.object" % name] = object_xml(obj, plan, new=is_new)
        for f in obj["fields"]:
            types["CustomField"].append("%s.%s" % (name, f["api_name"]))
            if not is_new:
                undo["CustomField"].append("%s.%s" % (name, f["api_name"]))
        if is_new:
            types["CustomObject"].append(name)
            types["ListView"].append(name + ".All")
            types["CustomTab"].append(name)
            files["tabs/%s.tab" % name] = tab_xml()
            undo["CustomObject"].append(name)
            undo["CustomTab"].append(name)
    files["permissionsets/%s.permissionset" % PERMSET] = permset_xml(plan)
    package = manifest_xml(types)
    destructive = manifest_xml(undo, version=False)
    files["package.xml"] = package
    components = sum(len(v) for v in types.values())
    return {"files": files, "package_xml": package, "destructive_xml": destructive,
            "types": {k: sorted(v) for k, v in types.items() if v},
            "undo_types": {k: sorted(v) for k, v in undo.items() if v},
            "components": components, "new_objects": change["new_objects"],
            "new_fields": change["new_fields"], "change": change,
            "plan_hash": plan_hash(plan),
            "package_hash": hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()}


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[path].encode("utf-8"))
    return buf.getvalue()


def package_zip(pkg: dict) -> bytes:
    return _zip(pkg["files"])


def destructive_zip(destructive_xml: str) -> bytes:
    """The rollback as the Metadata API takes it: an empty package.xml beside it."""
    return _zip({"package.xml": manifest_xml({}), "destructiveChanges.xml": destructive_xml})


__all__ = ["PlanError", "validate_plan", "plan_hash", "delta", "record_built", "forget_built", "empty_built",
           "build_package", "package_zip", "destructive_zip", "relationship_name", "list_columns",
           "API_VERSION", "PERMSET", "EXTENDABLE", "MAX_OBJECTS", "MAX_FIELDS", "FIELD_TYPES"]
