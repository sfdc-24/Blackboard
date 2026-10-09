"""What the build lane draws, in canvas kinds the page already renders.

Nothing here talks to an org or a model: it turns a checked plan (PROTOTYPE) or
the org's own describe and query answers (DISPLAY) into contract nodes and
model.updated payloads.

    architecture  a section whose first child is a heading, then process-step
                  and edge nodes in flow order (the conference canvas's diagram)
    record page   "<Object> - Record Page": highlights, details and related cards
    list view     "<Objects> - List View": a list of cards, one per row
    data model    model.updated: objects, fields, relationships, each marked
                  proposed or built (in the existing closed fields - see the PR
                  for the optional "status" the site contract could add)
"""
from __future__ import annotations

import datetime as _dt
import re

TEXT_MAX = 600
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,80}")
ARCH_ID = "sfb-arch"


def _t(value, cap: int = TEXT_MAX) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= cap else text[:cap - 3].rstrip() + "..."


def _id(*parts) -> str:
    raw = "-".join(str(p) for p in parts)
    clean = re.sub(r"[^A-Za-z0-9._:-]", "-", raw)[:80]
    return clean


# ---------------------------------------------------------------------------
# DISCUSS: the architecture flow

def architecture_node(arch: dict) -> dict:
    """heading, then each step followed by the edges that leave it: the order IS the flow."""
    title = _t(arch.get("title") or "Solution architecture", 120)
    children = [{"id": _id(ARCH_ID, "h"), "kind": "heading", "label": title}]
    steps = arch.get("steps") or []
    edges = list(arch.get("edges") or [])
    labels = {s["id"]: s["label"] for s in steps}
    used = set()
    for i, step in enumerate(steps):
        children.append({"id": _id(ARCH_ID, "s", step["id"]), "kind": "process-step",
                         "label": _t(step["label"], 120), "detail": _t(step.get("detail", ""))})
        for j, edge in enumerate(edges):
            if j in used or edge["from"] != step["id"]:
                continue
            used.add(j)
            children.append(_edge(j, edge, labels))
    for j, edge in enumerate(edges):
        if j not in used:
            children.append(_edge(j, edge, labels))
    return {"id": ARCH_ID, "kind": "section", "label": title, "children": children[:60]}


def _edge(j: int, edge: dict, labels: dict) -> dict:
    detail = "%s -> %s" % (labels.get(edge["from"], edge["from"]), labels.get(edge["to"], edge["to"]))
    if edge.get("detail"):
        detail += ", " + edge["detail"]
    return {"id": _id(ARCH_ID, "e", j), "kind": "edge", "label": _t(edge.get("label") or "flows to", 120),
            "detail": _t(detail)}


# ---------------------------------------------------------------------------
# Field types in words, and plausible values

def type_text(f: dict) -> str:
    kind = f["type"]
    if kind == "Text":
        return "Text(%d)" % f["length"]
    if kind in ("Number", "Currency", "Percent"):
        return "%s(%d,%d)" % (kind, f["precision"], f["scale"])
    if kind == "Picklist":
        return _t("Picklist: " + ", ".join(f["values"]), 120)
    if kind == "LongTextArea":
        return "Long Text Area(%d)" % f["length"]
    if kind == "Lookup":
        return "Lookup(%s)" % f["ref"]
    if kind == "MasterDetail":
        return "Master-Detail(%s)" % f["ref"]
    if kind == "Summary":
        target = f["child"] + ("." + f["child_field"] if f.get("child_field") else "")
        return "Roll-Up %s(%s)" % (f["operation"].upper(), target)
    return kind


def api_value(f: dict, i: int, today: _dt.date):
    """A value Salesforce accepts for field f on sample row i (1-based); None to leave it out."""
    kind = f["type"]
    if kind == "Text":
        return ("%s %d" % (f["label"], i))[:f["length"]]
    if kind == "Number":
        return round(1250 * i + 37 * (i % 3), min(f["scale"], 2))
    if kind == "Currency":
        return round(18.5 * i + 0.25 * (i % 4), min(f["scale"], 2))
    if kind == "Percent":
        return round(5.0 * i % 100, min(f["scale"], 2))
    if kind == "Date":
        return (today - _dt.timedelta(days=7 * (i - 1))).isoformat()
    if kind == "DateTime":
        return (today - _dt.timedelta(days=i - 1)).isoformat() + "T09:%02d:00.000Z" % (i % 60)
    if kind == "Checkbox":
        return i % 2 == 1
    if kind == "Picklist":
        return f["values"][(i - 1) % len(f["values"])]
    if kind == "LongTextArea":
        return "Sample note %d for the prototype." % i
    return None                                   # lookups, master-detail and roll-ups are not typed values


def shown(f: dict, value) -> str:
    if value is None or value == "":
        return "-"
    if f.get("type") == "Currency" and isinstance(value, (int, float)):
        return "$%s" % "{:,.2f}".format(value)
    if f.get("type") == "Percent" and isinstance(value, (int, float)):
        return "%s%%" % value
    if f.get("type") == "Checkbox" or isinstance(value, bool):
        return "Yes" if value else "No"
    return _t(value, 120)


def sample_name(obj: dict, i: int) -> str:
    nf = obj.get("name_field") or {}
    if nf.get("type") == "AutoNumber":
        fmt = nf["format"]
        zeros = re.search(r"\{(0+)\}", fmt).group(1)
        return re.sub(r"\{0+\}", str(i).zfill(len(zeros)), fmt)
    return "%s %d" % (obj.get("label") or obj["api_name"], i)


def prototype_rows(obj: dict, plan: dict, n: int, today: _dt.date) -> list:
    """Plausible rows for the mock: (name, [(label, shown value), ...])."""
    by_name = {o["api_name"]: o for o in plan["objects"]}
    rows = []
    for i in range(1, n + 1):
        cells = []
        for f in obj["fields"]:
            if f["type"] in ("Lookup", "MasterDetail"):
                parent = by_name.get(f["ref"])
                value = sample_name(parent, 1 + (i - 1) % 3) if parent and parent["api_name"].endswith("__c") \
                    else {"Account": "Acme Corp", "Contact": "Jordan Lee", "Lead": "Sam Rivera",
                          "Opportunity": "Acme - Renewal", "Case": "00001026", "User": "Owner"}.get(f["ref"], "-")
            elif f["type"] == "Summary":
                value = str(3 + i) if f["operation"] == "count" else "$%s" % "{:,.2f}".format(55.5 * i)
            else:
                value = shown(f, api_value(f, i, today))
            cells.append((f["label"], value))
        rows.append((sample_name(obj, i), cells))
    return rows


# ---------------------------------------------------------------------------
# PROTOTYPE and DISPLAY: the record page and the list view

def main_objects(plan: dict) -> tuple:
    """The record page object (the first new object) and the list view object (the first new
    object that points at it, else the same one)."""
    new = [o for o in plan["objects"] if o["api_name"].endswith("__c")]
    if not new:
        return None, None
    main = new[0]
    child = next((o for o in new if any(f["type"] in ("Lookup", "MasterDetail") and f["ref"] == main["api_name"]
                                         for f in o["fields"])), main)
    return main, child


def record_page_node(obj_label: str, api: str, name: str, cells: list, related: list, *, built: bool) -> dict:
    sid = _id("sfb-rec", api)
    tag = "built - live from the org" if built else "prototype - not built yet"
    title = _t("%s - Record Page (%s)" % (obj_label, tag), 200)
    details = [{"id": _id(sid, "f", i), "kind": "text", "label": _t("%s: %s" % (label, value), 200)}
               for i, (label, value) in enumerate(cells[:14])]
    children = [
        {"id": _id(sid, "h"), "kind": "heading", "label": title},
        {"id": _id(sid, "hl"), "kind": "card", "label": _t(name, 120),
         "detail": _t("Highlights: " + " | ".join("%s: %s" % c for c in cells[:3]) if cells else obj_label)},
        {"id": _id(sid, "edit"), "kind": "button", "label": "Edit"},
        {"id": _id(sid, "clone"), "kind": "button", "label": "Clone"},
        {"id": _id(sid, "det"), "kind": "card", "label": "Details", "children": details or [
            {"id": _id(sid, "f", 0), "kind": "text", "label": "No custom fields yet"}]},
    ]
    if related:
        children.append({"id": _id(sid, "rel"), "kind": "card", "label": "Related", "children": [
            {"id": _id(sid, "r", i), "kind": "text", "label": _t(r, 200)} for i, r in enumerate(related[:6])]})
    return {"id": sid, "kind": "section", "label": title, "children": children}


def list_view_node(plural: str, api: str, rows: list, *, built: bool, note: str = "") -> dict:
    sid = _id("sfb-list", api)
    tag = "built - records from the org" if built else "prototype - sample values"
    title = _t("%s - List View (%s)" % (plural, tag), 200)
    cards = [{"id": _id(sid, "r", i), "kind": "card", "label": _t(name, 120),
              "detail": _t(" | ".join("%s: %s" % c for c in cells[:5]))}
             for i, (name, cells) in enumerate(rows[:10])]
    children = [{"id": _id(sid, "h"), "kind": "heading", "label": title},
                {"id": _id(sid, "meta"), "kind": "text",
                 "label": _t(note or "All - %d item%s - sorted by Name" % (len(rows), "" if len(rows) == 1 else "s"))}]
    children.append({"id": _id(sid, "l"), "kind": "list", "label": "All", "children": cards or [
        {"id": _id(sid, "empty"), "kind": "text", "label": "No records yet - say \"add sample data\""}]})
    return {"id": sid, "kind": "section", "label": title, "children": children}


def prototype_nodes(plan: dict, today: _dt.date) -> list:
    main, child = main_objects(plan)
    if main is None:
        return []
    rows = prototype_rows(main, plan, 1, today)
    related = []
    for o in plan["objects"]:
        for f in o["fields"]:
            if f["type"] in ("Lookup", "MasterDetail") and f["ref"] == main["api_name"]:
                related.append("%s (3)" % o.get("plural", o["api_name"]))
    nodes = [record_page_node(main["label"], main["api_name"], rows[0][0], rows[0][1], related, built=False)]
    nodes.append(list_view_node(child["plural"], child["api_name"], prototype_rows(child, plan, 3, today),
                                built=False))
    return nodes


def replace_ops(artifact: dict, nodes: list, drop_prefixes=("sfb-rec", "sfb-list")) -> list:
    """Ops that remove this lane's old sections of the same family and insert the new ones."""
    present = [c.get("id") for c in artifact.get("children") or []]
    new_ids = {n["id"] for n in nodes}
    ops = []
    for cid in present:
        if cid in new_ids or any(str(cid).startswith(p) for p in drop_prefixes):
            ops.append({"op": "remove", "node_id": cid})
    for node in nodes:
        ops.append({"op": "insert_child", "node_id": artifact["id"], "node": node})
    return ops


# ---------------------------------------------------------------------------
# The data model, proposed (from the plan) and built (from the org's describe)

STANDARD_LABEL = {"Account": "Account", "Contact": "Contact", "Lead": "Lead", "Opportunity": "Opportunity",
                  "Case": "Case", "User": "User"}


def proposed_model(plan: dict, built: dict | None = None) -> dict:
    built = built or {"objects": {}, "fields": {}}
    objects, rels, ids = [], [], set()
    for o in plan["objects"]:
        new = o["api_name"].endswith("__c")
        state = "built" if (not new or o["api_name"] in built["objects"]) else "proposed"
        fields = []
        for f in o["fields"]:
            mark = "built" if "%s.%s" % (o["api_name"], f["api_name"]) in built["fields"] else "proposed"
            fields.append({"name": _t("%s (%s)" % (f["label"], f["api_name"]), 120),
                           "type": _t("%s - %s" % (type_text(f), mark), 160)})
            if f["type"] in ("Lookup", "MasterDetail"):
                rels.append({"from": o["api_name"], "to": f["ref"],
                             "kind": "master-detail" if f["type"] == "MasterDetail" else "lookup",
                             "label": _t(f["label"], 80)})
        if new:
            purpose = ("PROPOSED - not built yet. " if state == "proposed" else "BUILT in the org. ") + o["description"]
            name = "%s (%s)" % (o["label"], o["api_name"])
        else:
            purpose = "Standard object - only the custom fields below are added."
            name = "%s (standard)" % o["api_name"]
        objects.append({"id": o["api_name"], "name": _t(name, 120), "standard": not new,
                        "purpose": _t(purpose, 300), "fields": fields})
        ids.add(o["api_name"])
    for r in rels:
        if r["to"] not in ids:
            ids.add(r["to"])
            objects.append({"id": r["to"], "name": "%s (standard)" % STANDARD_LABEL.get(r["to"], r["to"]),
                            "standard": True, "purpose": "Standard object - unchanged.", "fields": []})
    proposed = sum(1 for o in plan["objects"] for f in o["fields"]
                   if "%s.%s" % (o["api_name"], f["api_name"]) not in built["fields"])
    findings = ["PROPOSED: %d field%s not built yet - nothing in the org has changed for them."
                % (proposed, "" if proposed == 1 else "s") if proposed else "Everything in this plan is built.",
                "Say \"build it\" to validate in the org (check only) and then confirm the deploy."]
    return {"domain": _t("Proposed: " + plan["title"], 120), "objects": objects, "relationships": rels,
            "findings": findings}


SF_TYPE = {"string": "Text", "double": "Number", "currency": "Currency", "percent": "Percent", "date": "Date",
           "datetime": "Date/Time", "boolean": "Checkbox", "picklist": "Picklist", "textarea": "Text Area",
           "reference": "Lookup", "id": "Id", "int": "Number", "email": "Email", "phone": "Phone", "url": "URL"}


def describe_type(d: dict) -> str:
    kind = d.get("type") or ""
    base = SF_TYPE.get(kind, kind)
    if kind == "string" and d.get("autoNumber"):
        return "Auto Number"
    if kind == "string":
        return "Text(%s)" % d.get("length")
    if kind == "textarea" and (d.get("length") or 0) > 255:
        return "Long Text Area(%s)" % d.get("length")
    if kind in ("double", "currency", "percent"):
        if d.get("calculated") and d.get("calculatedFormula") is None:
            return "Roll-Up Summary (%s)" % SF_TYPE.get(kind, kind)
        return "%s(%s,%s)" % (base, d.get("precision"), d.get("scale"))
    if kind == "reference":
        target = ",".join(d.get("referenceTo") or [])
        return ("Master-Detail(%s)" if d.get("relationshipOrder") is not None else "Lookup(%s)") % target
    if kind == "picklist":
        values = [v.get("value") for v in d.get("picklistValues") or [] if v.get("active", True)]
        return _t("Picklist: " + ", ".join(str(v) for v in values), 120)
    return base


def built_model(title: str, describes: list, observed_at: str) -> dict:
    """The data model as the ORG answered it: every object and field from describe, none from the plan."""
    objects, rels, ids = [], [], set()
    total = 0
    for d in describes:
        api = d.get("name")
        custom_fields = [f for f in d.get("fields") or [] if f.get("custom")]
        name_field = next((f for f in d.get("fields") or [] if f.get("nameField")), None)
        fields = []
        if name_field and d.get("custom"):
            fields.append({"name": _t("%s (%s)" % (name_field.get("label"), name_field.get("name")), 120),
                           "type": _t(describe_type(name_field) + " - built", 160)})
        for f in custom_fields:
            fields.append({"name": _t("%s (%s)" % (f.get("label"), f.get("name")), 120),
                           "type": _t(describe_type(f) + " - built", 160)})
            if f.get("type") == "reference" and f.get("referenceTo"):
                rels.append({"from": api, "to": f["referenceTo"][0],
                             "kind": "master-detail" if f.get("relationshipOrder") is not None else "lookup",
                             "label": _t(f.get("label"), 80)})
        total += len(custom_fields)
        objects.append({"id": api, "name": _t("%s (%s)" % (d.get("label"), api), 120),
                        "standard": not d.get("custom"),
                        "purpose": _t("BUILT - read back from the org at %s." % observed_at
                                      if d.get("custom") else
                                      "Standard object - its custom fields as the org reports them.", 300),
                        "fields": fields[:40]})
        ids.add(api)
    for r in rels:
        if r["to"] not in ids:
            ids.add(r["to"])
            objects.append({"id": r["to"], "name": "%s (standard)" % r["to"], "standard": True,
                            "purpose": "Standard object - unchanged.", "fields": []})
    return {"domain": _t("Built in your org: " + title, 120), "objects": objects, "relationships": rels,
            "findings": ["Read back from the org with describe at %s: %d object%s, %d custom field%s."
                         % (observed_at, len(describes), "" if len(describes) == 1 else "s", total,
                            "" if total == 1 else "s")]}


def describe_rows(describe: dict, records: list, links) -> list:
    """List-view rows from REAL records: (name + link, [(label, value), ...])."""
    labels = {f.get("name"): f.get("label") for f in describe.get("fields") or []}
    rows = []
    for rec in records[:10]:
        cells = [(labels.get(k, k), shown({}, v)) for k, v in rec.items()
                 if k not in ("attributes", "Id", "Name") and not isinstance(v, dict)]
        name = str(rec.get("Name") or rec.get("Id"))
        link = links(describe.get("name"), str(rec.get("Id") or "")).get("record")
        if link:
            cells.append(("Open", link))
        rows.append((name, cells))
    return rows


__all__ = ["architecture_node", "prototype_nodes", "replace_ops", "proposed_model", "built_model",
           "record_page_node", "list_view_node", "describe_rows", "api_value", "sample_name", "type_text",
           "main_objects", "ARCH_ID"]
