"""Build the two-page SFDC24 / Blackboard architecture brief.

The PDF is deliberately evidence-aware: LIVE, CURRENT, PARTIAL, REVIEW, DARK,
HELD, and TARGET are not interchangeable. It reads and validates the
machine-readable semantic contract in the accepted 2026-09-25 ADR (snapshot
refreshed by its 2026-09-26 addendum), then binds that contract's digest into
the PDF. The result is small enough to use as the Drive architecture directive.
The 2026-09-25 edition stays committed as a historical artifact; this script
builds the current edition only.

Every evidence claim on the page comes from the ADR's claims block, where each
line cites its source (the recorded fact list, a repository path, or a
Blackboard row). Review records carry every Codex verdict; component records
carry each page-2 component's single status, evidence, promotion test, gate
and dependencies, and draw both its box and its ledger row. The build fails on
an unrendered record, a verdict stated in free text, an unknown or cyclic
dependency, or a LIVE/CURRENT component that depends on anything blocked.

Clean-checkout dependency install:
    python -m pip install -r tools/requirements-architecture-pdf.txt
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import TABLOID, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph


ROOT = Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "ADR-20260925-BLACKBOARD-MINIBUS-MULTIAGENT-CONTROL-PLANE.md"
OUT = ROOT / "docs" / "SFDC24-BLACKBOARD-ARCHITECTURE-20260926.pdf"
PAGE_W, PAGE_H = landscape(TABLOID)

CONTRACT_START = "<!-- architecture-pdf-contract:start -->"
CONTRACT_END = "<!-- architecture-pdf-contract:end -->"
CONTRACT_KEYS = {
    "schema_version",
    "facts_refreshed_label",
    "facts_refreshed_iso",
    "production_controller_revision",
    "production_controller_label",
    "production_traffic_percent",
    "site_commit",
    "site_commit_label",
    "advisor_enabled",
    "converspan_production",
    "required_adr_phrases",
    "required_pdf_phrases",
}
RENDERED_TEXT: list[str] = []


def reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict:
    """Keep the ADR contract closed: duplicate keys are ambiguous, not last-wins."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate architecture PDF contract key: " + key)
        result[key] = value
    return result


def validate_contract(contract: dict, adr_prose: str) -> None:
    """Validate types, accepted state, and anchors outside the JSON contract."""
    if not isinstance(contract, dict) or set(contract) != CONTRACT_KEYS:
        raise RuntimeError("architecture PDF contract keys do not match the closed schema")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 1:
        raise RuntimeError("unsupported architecture PDF contract schema")
    if type(contract["production_traffic_percent"]) is not int or not (
            0 <= contract["production_traffic_percent"] <= 100):
        raise RuntimeError("production traffic percent must be an integer from 0 to 100")
    if contract["advisor_enabled"] is not False:
        raise RuntimeError("this accepted snapshot requires the advisor to remain disabled")
    if contract["converspan_production"] != "HELD":
        raise RuntimeError("this accepted snapshot requires Converspan production to remain held")
    for key in ("facts_refreshed_label", "facts_refreshed_iso",
                "production_controller_revision", "production_controller_label",
                "site_commit", "site_commit_label"):
        if not isinstance(contract[key], str) or not contract[key].strip() or len(contract[key]) > 120:
            raise RuntimeError(f"invalid architecture PDF contract value: {key}")
    for key in ("required_adr_phrases", "required_pdf_phrases"):
        phrases = contract[key]
        if (not isinstance(phrases, list) or not phrases
                or any(not isinstance(item, str) or not item or len(item) > 300
                       for item in phrases)):
            raise RuntimeError(f"invalid architecture PDF contract phrase list: {key}")
        if len(set(phrases)) != len(phrases):
            raise RuntimeError(f"duplicate architecture PDF contract phrase: {key}")
    missing = [phrase for phrase in contract["required_adr_phrases"]
               if phrase not in adr_prose]
    if missing:
        raise RuntimeError("ADR semantic drift; missing prose anchors: " + repr(missing))


def load_contract() -> tuple[dict, str]:
    """Read the closed semantic contract from the ADR and verify its anchors."""
    adr_text = ADR.read_text(encoding="utf-8")
    if adr_text.count(CONTRACT_START) != 1 or adr_text.count(CONTRACT_END) != 1:
        raise RuntimeError("ADR must contain exactly one architecture PDF contract")
    before_contract, remainder = adr_text.split(CONTRACT_START, 1)
    raw, after_contract = remainder.split(CONTRACT_END, 1)
    raw = raw.strip()
    if not raw.startswith("```json") or not raw.endswith("```"):
        raise RuntimeError("architecture PDF contract must be a fenced JSON object")
    contract = json.loads(
        raw[len("```json"): -len("```")].strip(),
        object_pairs_hook=reject_duplicate_json_keys,
    )
    # Contract-contained strings cannot satisfy an ADR prose anchor.
    validate_contract(contract, before_contract + after_contract)
    canonical = json.dumps(contract, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return contract, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


CONTRACT, CONTRACT_SHA256 = load_contract()

CLAIMS_START = "<!-- architecture-pdf-claims:start -->"
CLAIMS_END = "<!-- architecture-pdf-claims:end -->"
CLAIM_SOURCE = " | Source: "
FIELD = " || "
FORBIDDEN_IN_CLAIM = "<>&"   # ReportLab paragraph markup
COMPONENT_STATUSES = ("LIVE", "CURRENT", "PARTIAL", "NO-GO", "DARK", "HELD", "TARGET")
VERDICTS = ("GO", "NO-GO", "PENDING")
GATES = tuple("G%d" % n for n in range(1, 10))
BLOCKED_STATUSES = ("NO-GO", "HELD", "DARK", "TARGET")
VERDICT_WORDS = {"GO": "Codex GO", "NO-GO": "Codex NO-GO", "PENDING": "Codex verdict pending"}
VERDICT_SHORT = {"GO": "GO", "NO-GO": "NO-GO", "PENDING": "verdict pending"}
# A Codex verdict is stated only by a review record, never in free text or a
# note, so the two pages cannot carry "pending" beside "NO-GO" for one head.
# "requires Codex GO" states a requirement, not a verdict, and is allowed.
VERDICT_IN_TEXT = re.compile(r"\bNO-GO\b|\bpending\b|(?<!requires )\bCodex GO\b|\bGO at\b",
                             re.IGNORECASE)
VERDICT_TEXT_EXEMPT = {"p1.legend"}   # the legend defines the words


def _check_text(key: str, text: str) -> None:
    if not text:
        raise RuntimeError("architecture PDF record has an empty field: " + key)
    if any(ch in text for ch in FORBIDDEN_IN_CLAIM):
        raise RuntimeError("architecture PDF record contains paragraph markup: " + key)


def load_records(adr_text: str) -> tuple[dict, dict, dict]:
    """Read the ADR claims block into (claims, reviews, components)."""
    if adr_text.count(CLAIMS_START) != 1 or adr_text.count(CLAIMS_END) != 1:
        raise RuntimeError("ADR must contain exactly one architecture PDF claims block")
    block = adr_text.split(CLAIMS_START, 1)[1].split(CLAIMS_END, 1)[0]
    claims: dict[str, str] = {}
    reviews: dict[str, dict] = {}
    components: dict[str, dict] = {}
    for line in block.strip().splitlines():
        if not line.startswith("- `") or line.count("`") < 2 or CLAIM_SOURCE not in line:
            raise RuntimeError("malformed architecture PDF record line: " + line[:80])
        key, rest = line[3:].split("`", 1)
        body, source = rest.strip().split(CLAIM_SOURCE, 1)
        body = body.strip()
        if not key or key in claims or key in reviews or key in components:
            raise RuntimeError("missing or duplicate architecture PDF record key: " + key)
        if not source.strip():
            raise RuntimeError("architecture PDF record needs a source: " + key)
        if key.startswith("r."):
            fields = [f.strip() for f in body.split(FIELD)]
            if len(fields) != 6:
                raise RuntimeError("review record needs 6 fields: " + key)
            ref, subject, head, verdict, row, note = fields
            for text in (ref, subject, head, row, note):
                _check_text(key, text)
            if verdict not in VERDICTS:
                raise RuntimeError("review verdict must be one of %s: %s" % (VERDICTS, key))
            if VERDICT_IN_TEXT.search(note):
                raise RuntimeError("a review note may not state a Codex verdict: " + key)
            reviews[key] = {"ref": ref, "subject": subject, "head": head,
                            "verdict": verdict, "row": row, "note": note}
        elif key.startswith("c."):
            fields = [f.strip() for f in body.split(FIELD)]
            if len(fields) != 7:
                raise RuntimeError("component record needs 7 fields: " + key)
            status, title, head, now, promote, gate, depends = fields
            for text in (title, head, now, promote, gate, depends):
                _check_text(key, text)
            if status not in COMPONENT_STATUSES:
                raise RuntimeError("component status must be one of %s: %s" % (COMPONENT_STATUSES, key))
            for text in (now, promote):
                if VERDICT_IN_TEXT.search(text):
                    raise RuntimeError("component text may not state a Codex verdict: " + key)
            components[key] = {"status": status, "title": title, "head": head, "now": now,
                               "promote": promote, "gate": gate,
                               "depends": [d.strip() for d in depends.split(",") if d.strip()]}
        elif key.startswith(("p1.", "p2.")):
            _check_text(key, body)
            if key not in VERDICT_TEXT_EXEMPT and VERDICT_IN_TEXT.search(body):
                raise RuntimeError("free text may not state a Codex verdict: " + key)
            claims[key] = body
        else:
            raise RuntimeError("unknown architecture PDF record kind: " + key)
    validate_records(reviews, components)
    return claims, reviews, components


def validate_records(reviews: dict, components: dict) -> None:
    """One status per component, closed dependencies and no contradictions."""
    seen: dict[tuple[str, str], str] = {}
    for key, review in reviews.items():
        ident = (review["ref"], review["head"])
        if ident in seen:
            raise RuntimeError("two review records for one head: %s and %s" % (seen[ident], key))
        seen[ident] = key
    for key, comp in components.items():
        for dep in comp["depends"]:
            if dep not in GATES and dep not in reviews and dep not in components:
                raise RuntimeError("component %s depends on unknown %s" % (key, dep))
        review_deps = [reviews[d] for d in comp["depends"] if d in reviews]
        comp_deps = [components[d] for d in comp["depends"] if d in components]
        if comp["status"] in ("LIVE", "CURRENT"):
            if any(r["verdict"] != "GO" for r in review_deps):
                raise RuntimeError("%s is %s but depends on a review that is not GO" % (key, comp["status"]))
            if any(c["status"] in BLOCKED_STATUSES for c in comp_deps):
                raise RuntimeError("%s is %s but depends on a blocked component" % (key, comp["status"]))
        if comp["status"] == "NO-GO" and not any(r["verdict"] == "NO-GO" for r in review_deps):
            raise RuntimeError("%s is NO-GO without a NO-GO review" % key)
    # No dependency cycles among components.
    state: dict[str, int] = {}

    def visit(key: str) -> None:
        if state.get(key) == 1:
            raise RuntimeError("component dependency cycle through " + key)
        if state.get(key) == 2:
            return
        state[key] = 1
        for dep in components[key]["depends"]:
            if dep in components:
                visit(dep)
        state[key] = 2

    for key in components:
        visit(key)


CLAIMS, REVIEWS, COMPONENTS = load_records(ADR.read_text(encoding="utf-8"))
RECORDS_SHA256 = hashlib.sha256(json.dumps(
    {"claims": CLAIMS, "reviews": REVIEWS, "components": COMPONENTS},
    ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
USED: dict[str, set] = {"claims": set(), "reviews": set(), "boxes": set(), "rows": set()}


def CLAIM(key: str) -> str:
    """The ADR's free-text claim for key, recorded as rendered."""
    if key not in CLAIMS:
        raise RuntimeError("PDF renders a claim the ADR does not list: " + key)
    USED["claims"].add(key)
    return CLAIMS[key]


def REVIEW(key: str) -> str:
    """A review record in full: subject, head, verdict and note."""
    if key not in REVIEWS:
        raise RuntimeError("PDF renders a review the ADR does not list: " + key)
    USED["reviews"].add(key)
    r = REVIEWS[key]
    return "%s at %s: %s. %s" % (r["subject"], r["head"], VERDICT_WORDS[r["verdict"]], r["note"])


def REVIEW_SHORT(key: str) -> str:
    if key not in REVIEWS:
        raise RuntimeError("PDF renders a review the ADR does not list: " + key)
    USED["reviews"].add(key)
    r = REVIEWS[key]
    return "%s at %s: %s" % (r["ref"], r["head"], VERDICT_SHORT[r["verdict"]])


def COMPONENT_BOX(key: str) -> tuple[str, str, str]:
    """(title, body, status) for a page-2 box, with dependency verdicts."""
    comp = COMPONENTS[key]
    USED["boxes"].add(key)
    body = comp["now"]
    verdicts = [REVIEW_SHORT(d) for d in comp["depends"] if d in REVIEWS]
    if verdicts:
        body += " " + "; ".join(verdicts) + "."
    return comp["title"], body, comp["status"]


def COMPONENT_ROW(key: str) -> tuple[str, str, str]:
    """(title, promotion test and gate, status) for the promotion ledger."""
    comp = COMPONENTS[key]
    USED["rows"].add(key)
    return comp["title"], "%s Gate: %s." % (comp["promote"], comp["gate"]), comp["status"]


def check_all_rendered() -> None:
    missing = {
        "claims": sorted(set(CLAIMS) - USED["claims"]),
        "reviews": sorted(set(REVIEWS) - USED["reviews"]),
        "component boxes": sorted(set(COMPONENTS) - USED["boxes"]),
        "ledger rows": sorted(set(COMPONENTS) - USED["rows"]),
    }
    missing = {k: v for k, v in missing.items() if v}
    if missing:
        raise RuntimeError("ADR records not rendered in the PDF: " + repr(missing))


def track(text: str) -> None:
    RENDERED_TEXT.append(str(text))

INK = colors.HexColor("#10243E")
MUTED = colors.HexColor("#526177")
LIGHT = colors.HexColor("#F5F7FA")
PANEL = colors.white
BORDER = colors.HexColor("#CAD3DF")
BLUE = colors.HexColor("#2368A2")
BLUE_SOFT = colors.HexColor("#EAF3FB")
GREEN = colors.HexColor("#267A55")
GREEN_SOFT = colors.HexColor("#E8F5ED")
ORANGE = colors.HexColor("#B76213")
ORANGE_SOFT = colors.HexColor("#FFF0E1")
PURPLE = colors.HexColor("#6949A0")
PURPLE_SOFT = colors.HexColor("#F1ECF8")
RED = colors.HexColor("#A64141")
RED_SOFT = colors.HexColor("#FBECEC")
GRAY_SOFT = colors.HexColor("#EDF1F5")
REVIEW_SOFT = colors.HexColor("#FFF6D8")
DARK_SOFT = colors.HexColor("#DFE4EA")

STATUS_FILL = {
    "LIVE": GREEN_SOFT,
    "CURRENT": GREEN_SOFT,
    "PARTIAL": ORANGE_SOFT,
    "HELD": RED_SOFT,
    "TARGET": BLUE_SOFT,
    "OPTIONAL": PURPLE_SOFT,
    "REVIEW": REVIEW_SOFT,   # an open PR with an exact-head review pending
    "DARK": DARK_SOFT,       # merged to main and switched off
}


def paragraph(c: canvas.Canvas, text: str, x: float, top: float, width: float,
              font_size: float = 8.0, leading: float | None = None,
              color=INK, bold: bool = False, align=TA_LEFT,
              max_height: float = 1000) -> float:
    track(text)
    style = ParagraphStyle(
        name="architecture",
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=font_size,
        leading=leading or font_size * 1.23,
        textColor=color,
        alignment=align,
        spaceBefore=0,
        spaceAfter=0,
        allowWidows=0,
        allowOrphans=0,
    )
    p = Paragraph(text, style)
    _, height = p.wrap(width, max_height)
    p.drawOn(c, x, top - height)
    return height


def pill(c: canvas.Canvas, x: float, y: float, label: str, fill=None,
         text_color=INK, font_size: float = 6.5) -> float:
    track(label)
    fill = fill or STATUS_FILL.get(label, GRAY_SOFT)
    width = stringWidth(label, "Helvetica-Bold", font_size) + 11
    c.setFillColor(fill)
    c.roundRect(x, y, width, 13, 6.5, stroke=0, fill=1)
    c.setFillColor(text_color)
    c.setFont("Helvetica-Bold", font_size)
    c.drawCentredString(x + width / 2, y + 3.4, label)
    return width


def box(c: canvas.Canvas, x: float, y: float, w: float, h: float,
        title: str, body: str, fill=PANEL, accent=BLUE,
        status: str | None = None, title_size: float = 8.4,
        body_size: float = 7.0, radius: float = 8) -> None:
    body_size = max(body_size, 7.0)
    c.setFillColor(fill)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.75)
    c.roundRect(x, y, w, h, radius, stroke=1, fill=1)
    c.setFillColor(accent)
    c.roundRect(x, y + h - 5, w, 5, radius, stroke=0, fill=1)
    top = y + h - 12
    paragraph(c, title, x + 9, top, w - 18, title_size,
              title_size * 1.13, INK, True, TA_LEFT, h)
    paragraph(c, body, x + 9, top - title_size * 1.55, w - 18, body_size,
              body_size * 1.24, MUTED, False, TA_LEFT, h)
    if status:
        pill(c, x + 9, y + 4, status)


def section_label(c: canvas.Canvas, x: float, y: float, label: str,
                  width: float) -> None:
    track(label)
    c.setFillColor(colors.HexColor("#E7ECF2"))
    c.roundRect(x, y, width, 17, 8.5, stroke=0, fill=1)
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(x + 8, y + 5, label.upper())


def arrow(c: canvas.Canvas, x1: float, y1: float, x2: float, y2: float,
          color=BLUE, width: float = 1.25, dashed: bool = False,
          label: str | None = None, label_at: tuple[float, float] | None = None) -> None:
    c.saveState()
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(width)
    if dashed:
        c.setDash(4, 2)
    c.line(x1, y1, x2, y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    head = 5.5
    path = c.beginPath()
    path.moveTo(x2, y2)
    path.lineTo(x2 + head * math.cos(angle + math.pi * 0.84),
                y2 + head * math.sin(angle + math.pi * 0.84))
    path.lineTo(x2 + head * math.cos(angle - math.pi * 0.84),
                y2 + head * math.sin(angle - math.pi * 0.84))
    path.close()
    c.drawPath(path, stroke=0, fill=1)
    c.restoreState()
    if label:
        track(label)
        lx, ly = label_at or ((x1 + x2) / 2, (y1 + y2) / 2)
        label_width = stringWidth(label, "Helvetica-Bold", 6.5) + 9
        c.setFillColor(colors.white)
        c.roundRect(lx - label_width / 2, ly - 5, label_width, 11, 4,
                    stroke=0, fill=1)
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawCentredString(lx, ly - 1.7, label)


def ortho_arrow(c: canvas.Canvas, points: list[tuple[float, float]], color=BLUE,
                width: float = 1.2, dashed: bool = False,
                label: str | None = None,
                label_at: tuple[float, float] | None = None) -> None:
    c.saveState()
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(width)
    if dashed:
        c.setDash(4, 2)
    path = c.beginPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    c.drawPath(path, stroke=1, fill=0)
    x1, y1 = points[-2]
    x2, y2 = points[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    head = 5.5
    tip = c.beginPath()
    tip.moveTo(x2, y2)
    tip.lineTo(x2 + head * math.cos(angle + math.pi * 0.84),
               y2 + head * math.sin(angle + math.pi * 0.84))
    tip.lineTo(x2 + head * math.cos(angle - math.pi * 0.84),
               y2 + head * math.sin(angle - math.pi * 0.84))
    tip.close()
    c.drawPath(tip, stroke=0, fill=1)
    c.restoreState()
    if label:
        track(label)
        lx, ly = label_at or points[len(points) // 2]
        label_width = stringWidth(label, "Helvetica-Bold", 6.5) + 9
        c.setFillColor(colors.white)
        c.roundRect(lx - label_width / 2, ly - 5, label_width, 11, 4,
                    stroke=0, fill=1)
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawCentredString(lx, ly - 1.7, label)


def connector_label(c: canvas.Canvas, x: float, y: float, label: str,
                    color=BLUE) -> None:
    """Draw a connector label after nodes so no node can obscure the chip."""
    track(label)
    label_width = stringWidth(label, "Helvetica-Bold", 6.5) + 10
    c.setFillColor(colors.white)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.35)
    c.roundRect(x - label_width / 2, y - 5.5, label_width, 12, 4,
                stroke=1, fill=1)
    c.setFillColor(color)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawCentredString(x, y - 1.8, label)


def header(c: canvas.Canvas, title: str, subtitle: str, page_label: str) -> None:
    track(title)
    track(subtitle)
    track(page_label)
    c.setFillColor(LIGHT)
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 19)
    c.drawString(28, PAGE_H - 34, title)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8.0)
    c.drawString(28, PAGE_H - 50, subtitle)
    c.setFillColor(BLUE_SOFT)
    c.roundRect(PAGE_W - 152, PAGE_H - 56, 124, 25, 12, stroke=0, fill=1)
    c.setFillColor(BLUE)
    c.setFont("Helvetica-Bold", 8.0)
    c.drawCentredString(PAGE_W - 90, PAGE_H - 47, page_label)


def principle_strip(c: canvas.Canvas, entries: list[tuple[str, str, object, object]]) -> None:
    gap = 7
    x = 28
    y = PAGE_H - 91
    width = (PAGE_W - 56 - gap * (len(entries) - 1)) / len(entries)
    for title, body, fill, accent in entries:
        track(title)
        track(body)
        c.setFillColor(fill)
        c.roundRect(x, y, width, 27, 7, stroke=0, fill=1)
        c.setFillColor(accent)
        c.setFont("Helvetica-Bold", 6.6)
        c.drawString(x + 8, y + 15.5, title)
        c.setFillColor(INK)
        c.setFont("Helvetica", 6.5)
        c.drawString(x + 8, y + 6.2, body)
        x += width + gap


def panel_shell(c: canvas.Canvas) -> tuple[tuple[float, float, float, float],
                                           tuple[float, float, float, float]]:
    left = (28, 72, 836, 615)
    right = (878, 72, 318, 615)
    for x, y, w, h in (left, right):
        c.setFillColor(PANEL)
        c.setStrokeColor(BORDER)
        c.roundRect(x, y, w, h, 12, stroke=1, fill=1)
    return left, right


def tech_row(c: canvas.Canvas, x: float, top: float, w: float,
             title: str, why: str, status: str, accent=BLUE,
             row_h: float = 41, body_offset: float = 16) -> float:
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.45)
    c.line(x, top - row_h, x + w, top - row_h)
    pill(c, x, top - 16, status)
    paragraph(c, title, x + 58, top - 3, w - 58, 7.5, 8.2, INK, True)
    paragraph(c, why, x + 58, top - body_offset, w - 58, 7.0, 7.9, MUTED)
    return top - row_h


def tech_panel_header(c: canvas.Canvas, x: float, y: float, w: float, h: float,
                      title: str, body: str, rows_offset: float = 70) -> float:
    paragraph(c, title, x + 15, y + h - 17, w - 30, 11.5, 13, INK, True)
    paragraph(c, body, x + 15, y + h - 38, w - 30, 7.0, 8.1, MUTED)
    return y + h - rows_offset


def footer(c: canvas.Canvas, page_no: int, left: str, right: str) -> None:
    track(left)
    track(right)
    track(f"{page_no} / 2")
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 6.5)
    c.drawString(28, 34, left)
    c.drawRightString(PAGE_W - 28, 34, right)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawRightString(PAGE_W - 28, 50, f"{page_no} / 2")


def draw_current(c: canvas.Canvas) -> None:
    header(
        c,
        "SFDC24 + Blackboard - current operating architecture",
        f"Evidence-bound snapshot through {CONTRACT['facts_refreshed_label']}: what is live, what was run, and what is still held",
        "CURRENT STATE",
    )
    principle_strip(c, [
        ("PRODUCTION CONTAINED", CLAIM("p1.prod"), GREEN_SOFT, GREEN),
        ("ONE AUDIO ARBITER", "Realtime host plus queued TTS; one serialized audible turn", BLUE_SOFT, BLUE),
        ("ONE ARTIFACT WRITER", "Parallel lanes propose; the controller validates and commits", PURPLE_SOFT, PURPLE),
        ("EVIDENCE LEVELS STAY SEPARATE", "Source, served bytes, provider success and E2E are distinct", ORANGE_SOFT, ORANGE),
    ])
    (_, _, _, _), (rx, ry, rw, rh) = panel_shell(c)

    section_label(c, 43, 655, "User and channel surfaces", 186)
    section_label(c, 43, 552, "Live browser session", 151)
    section_label(c, 43, 438, "Bounded work and commit", 171)
    section_label(c, 43, 316, "Durable coordination and delivery", 220)

    # Paths are drawn first; their labels are redrawn after the nodes so they
    # remain legible and cannot imply a false Blackboard->Pipedream->GCS chain.
    arrow(c, 172, 588, 302, 548, BLUE, 1.35)
    arrow(c, 230, 520, 215, 520, GREEN, 1.35)
    arrow(c, 215, 503, 230, 503, GREEN, 1.35)
    arrow(c, 390, 513, 410, 513, BLUE, 1.35)
    arrow(c, 688, 520, 708, 520, ORANGE, 1.2)
    ortho_arrow(c, [(770, 475), (770, 463), (310, 463), (310, 475)],
                ORANGE, 1.2)

    # Controller dispatches bounded work; every result returns to controller.
    for start, end, color in (
        ((465, 475), (151, 428), PURPLE),
        ((535, 475), (356, 428), BLUE),
        ((605, 475), (548, 428), GREEN),
    ):
        arrow(c, *start, *end, color, 1.05)
        arrow(c, *end, *start, color, 0.9)
    arrow(c, 650, 475, 748, 428, BLUE, 1.35)

    # Ordered revisions persist independently; publication is a governed
    # destination, not the last hop of a durable-system pipeline.
    arrow(c, 748, 352, 562, 306, BLUE, 1.25)
    ortho_arrow(c, [(800, 352), (800, 330), (752, 330), (752, 306)],
                PURPLE, 1.1, True)
    ortho_arrow(c, [(835, 390), (850, 390), (850, 570), (250, 570), (250, 588)],
                BLUE, 1.0)

    # Control and channel relationships are deliberately separate.
    ortho_arrow(c, [(160, 306), (160, 340), (400, 340), (400, 475)],
                ORANGE, 1.15, True)
    ortho_arrow(c, [(382, 588), (382, 306)], GREEN, 1.0, True)
    arrow(c, 285, 270, 270, 270, GREEN, 1.0, True)
    ortho_arrow(c, [(557, 588), (557, 320), (220, 320), (220, 306)],
                PURPLE, 1.0, True)
    ortho_arrow(c, [(748, 588), (748, 560), (650, 560), (650, 548)],
                ORANGE, 1.0, True)

    # Surface layer (descriptions unchanged from the 2026-09-25 edition).
    box(c, 50, 588, 245, 64,
        "sfdc24.com authenticated owner",
        "Authenticated owner session with voice/text input, dialogue cards, design choices and live website/app prototype.",
        BLUE_SOFT, BLUE, "LIVE", 8.6, 6.2)
    box(c, 310, 588, 145, 64,
        "WhatsApp",
        "Inbound observed; outbound API accepted. Delivery remains unverified.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.3, 6.05)
    box(c, 470, 588, 175, 64,
        "Zoom + Ubuntu presenter",
        "OAuth/RTMS/presenter source exists; Ubuntu VM is stopped. No live meeting acceptance.",
        PURPLE_SOFT, PURPLE, "HELD", 8.1, 5.95)
    box(c, 660, 588, 175, 64,
        "Salesforce operator observation",
        "PlaygroundOrg showed 22 Leads; org binding and website facts/effects stay gated.",
        ORANGE_SOFT, ORANGE, "PARTIAL", 8.1, 5.95)

    # Browser media and controller layer.
    box(c, 50, 475, 165, 73,
        "OpenAI Realtime",
        CLAIM("p1.realtime"),
        GREEN_SOFT, GREEN, "PARTIAL", 8.25, 6.0)
    box(c, 230, 475, 160, 73,
        "Browser audio arbiter",
        REVIEW("r.site221"),
        BLUE_SOFT, BLUE, "LIVE", 8.15, 6.0)
    box(c, 410, 475, 278, 73,
        f"Studio Controller - production {CONTRACT['production_controller_label']}",
        "FastAPI session authority: auth, topic route, deadlines, revision fencing, typed events and command dedupe. " + CLAIM("p1.controller"),
        BLUE_SOFT, BLUE, "LIVE", 8.65, 6.15)
    box(c, 708, 475, 127, 73,
        "OpenAI TTS",
        "Current Architect and Muse speech returns as bounded browser audio blobs.",
        ORANGE_SOFT, ORANGE, "PARTIAL", 8.0, 5.8)

    # Parallel work and single commit layer.
    box(c, 50, 352, 202, 76,
        "TALK / RECAP route",
        "Topic routing can choose Claude, OpenAI or Gemini for bounded spoken text. Real Gemini owner-path proof is pending.",
        PURPLE_SOFT, PURPLE, "PARTIAL", 8.3, 5.9)
    box(c, 267, 352, 178, 76,
        "Claude builder",
        "Authoritative builder produces the only committable typed artifact proposal.",
        BLUE_SOFT, BLUE, "PARTIAL", 8.3, 6.05)
    box(c, 460, 352, 176, 76,
        "Analyst + Muse",
        "Bounded analysis and creative directions support the build; they do not commit artifacts.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.3, 5.95)
    box(c, 661, 352, 174, 76,
        "Ordered artifact + event ledger",
        "The controller alone validates and commits; this ledger records versions, events, snapshots and receipts.",
        GRAY_SOFT, INK, "CURRENT", 8.2, 5.95)

    # Durable plane.
    box(c, 50, 228, 220, 78,
        "Blackboard + Apps Script",
        "Handoff, claims, review, audit and exact row read-back. " + CLAIM("p1.incident"),
        ORANGE_SOFT, ORANGE, "CURRENT", 8.45, 6.05)
    box(c, 285, 228, 150, 78,
        "Pipedream + outbox",
        "Existing channel bridge. Deployed signing/dedupe and destination delivery remain unverified.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.25, 5.9)
    box(c, 450, 228, 160, 78,
        "GCS CAS session state",
        "Durable state, optimistic concurrency, replayable events and snapshot repair outside containers.",
        BLUE_SOFT, BLUE, "CURRENT", 8.25, 5.9)
    box(c, 625, 228, 210, 78,
        "GitHub + public site",
        CLAIM("p1.repo") + " " + CLAIM("p1.served"),
        PURPLE_SOFT, PURPLE, "LIVE", 8.15, 5.85)

    # Evidence band.
    box(c, 50, 76, 210, 144,
        "Owner live run 26 Sep 01:00:53-01:10:11Z",
        CLAIM("p1.run"),
        GREEN_SOFT, GREEN, "PARTIAL", 8.5, 6.0)
    box(c, 272, 76, 303, 144,
        "Exact-head verdicts on open work",
        " ".join(REVIEW(k) for k in ("r.pr272", "r.pr272b", "r.r5d", "r.site223", "r.site224",
                                     "r.site222a", "r.site222b")),
        REVIEW_SOFT, ORANGE, "NO-GO", 8.5, 5.95)
    box(c, 590, 76, 245, 144,
        "Held, and dark source",
        " ".join([REVIEW("r.pr260"), REVIEW("r.pr260b")]
                 + [CLAIM(k) for k in ("p1.r261", "p1.dark", "p1.unchanged")]),
        RED_SOFT, RED, "HELD", 8.5, 5.9)

    # Connector label chips are last so no box can cover or reframe them.
    connector_label(c, 236, 570, "session UI + voice", BLUE)
    connector_label(c, 135, 463, "duplex WebRTC", GREEN)
    connector_label(c, 535, 463, "bounded TTS audio blob", ORANGE)
    connector_label(c, 437, 448, "typed build", BLUE)
    connector_label(c, 580, 444, "analysis / options", GREEN)
    connector_label(c, 700, 449, "single fenced commit", BLUE)
    connector_label(c, 656, 330, "state + replay", BLUE)
    connector_label(c, 766, 339, "governed publish", PURPLE)
    connector_label(c, 290, 340, "handoff + audit", ORANGE)
    connector_label(c, 460, 320, "source only", PURPLE)
    connector_label(c, 700, 560, "disabled canary", ORANGE)
    connector_label(c, 600, 570, "ordered SSE + snapshots", BLUE)

    # Current stack panel (row text unchanged from the 2026-09-25 edition
    # except the OpenAI TTS row, which is a listed claim).
    top = tech_panel_header(
        c, rx, ry, rw, rh,
        "Current technologies and why",
        CLAIM("p1.legend"),
    )
    rows = [
        ("GitHub Pages + typed browser UI", "Fast versioned delivery of dialogue, prototype and release surfaces.", "LIVE", PURPLE),
        ("Cloud Run + Python/FastAPI", "Managed controller runtime, TLS, bounded API surface and rollback revisions.", "LIVE", BLUE),
        ("OpenAI Realtime WebRTC", "Lowest-hop browser conversation path; full microphone/audio acceptance remains open.", "PARTIAL", GREEN),
        ("OpenAI TTS", CLAIM("p1.tts_row"), "PARTIAL", ORANGE),
        ("Claude builder", "Single authoritative artifact proposal keeps revisions deterministic and reversible.", "PARTIAL", BLUE),
        ("Gemini topic route", "Fast alternative TALK/RECAP provider behind the same controller policy.", "PARTIAL", GREEN),
        ("Gemini advisor", "Merged source is quarantined; hardening, exact-head review and a real dark probe are required.", "DARK", RED),
        ("GCS CAS + event ledger", "Persistent session state, revision fencing, replay and reconnect repair.", "CURRENT", BLUE),
        ("Blackboard + Apps Script", "Durable multi-agent command, evidence, handoff and audit plane.", "CURRENT", ORANGE),
        ("Pipedream + WhatsApp API", "Existing mobile bridge; delivery/read receipts require separate proof.", "PARTIAL", GREEN),
        ("Salesforce APIs", "Business/customer-success engine; public facts and effects remain gated.", "HELD", ORANGE),
        ("Zoom RTMS + Ubuntu presenter", "Correct split between media observer and real meeting participant; not accepted live.", "HELD", PURPLE),
    ]
    for row in rows:
        top = tech_row(c, rx + 15, top, rw - 30, *row, row_h=42)

    footer(
        c, 1,
        f"Tabloid digital brief | Source: ADR-20260925 and its 2026-09-26 addendum, where each claim is cited | Facts refreshed {CONTRACT['facts_refreshed_label']}",
        "Transport proof is not human-heard end-to-end acceptance.",
    )
    c.showPage()


def draw_future(c: canvas.Canvas) -> None:
    header(
        c,
        "SFDC24 + Blackboard - future governed minibus architecture",
        f"Evidence-bound through {CONTRACT['facts_refreshed_label']}: each component's status now, the ADR pass condition that promotes it, and its gate",
        "FUTURE STATE",
    )
    principle_strip(c, [
        ("MOTHERBOARD COMMAND", "Registry, leases, policies, budgets, evidence, pause and kill stay central", BLUE_SOFT, BLUE),
        ("SEAMLESS PRESENT MOMENT", "Audio, dialogue cards and live prototype revisions share one turn timeline", GREEN_SOFT, GREEN),
        ("MULTI-AGENT, SINGLE COMMIT", "Claude builds; Gemini advises; controller validates and commits", PURPLE_SOFT, PURPLE),
        ("SFDC24 FIRST", "Converspan production waits for audio, isolation, durability and rollback gates", ORANGE_SOFT, ORANGE),
    ])
    (_, _, _, _), (rx, ry, rw, rh) = panel_shell(c)

    section_label(c, 43, 655, "Blackboard control and audit plane", 218)
    section_label(c, 43, 575, "SFDC24 service minibus", 150)
    section_label(c, 43, 300, "Sibling client minibuses and commercial plane", 270)

    # Outer SFDC24 minibus boundary.
    c.setFillColor(colors.HexColor("#FAFCFF"))
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.1)
    c.roundRect(45, 318, 802, 253, 11, stroke=1, fill=1)

    # Blackboard governs SFDC24 and each client minibus as siblings. The client
    # trunk stays outside the SFDC24 boundary so leases never appear to transit it.
    arrow(c, 445, 594, 445, 571, BLUE, 1.5)
    ortho_arrow(c, [(807, 571), (807, 585), (680, 585), (680, 594)],
                GREEN, 1.0, True)
    # Leases go down to each sibling minibus; bounded returns come back up to
    # Blackboard on their own trunk, also outside the SFDC24 boundary.
    ortho_arrow(c, [(840, 622), (856, 622), (856, 294), (115, 294), (115, 276)],
                BLUE, 1.0, True)
    for x in (285, 460):
        ortho_arrow(c, [(x, 294), (x, 276)], BLUE, 0.9, True)
    for x in (165, 345, 515):
        ortho_arrow(c, [(x, 276), (x, 287), (866, 287), (866, 606), (840, 606)],
                    GREEN, 0.9, True)

    # Channel row <-> coordinator row. Realtime is duplex; TTS returns only
    # through the sole audio arbiter; web commands and ordered SSE meet the
    # controller.
    arrow(c, 115, 503, 115, 476, GREEN, 1.25)
    arrow(c, 130, 476, 130, 503, GREEN, 1.25)
    arrow(c, 257, 503, 257, 476, ORANGE, 1.15)
    arrow(c, 340, 476, 300, 503, ORANGE, 1.0)
    arrow(c, 380, 503, 380, 476, BLUE, 1.25)
    arrow(c, 395, 476, 395, 503, BLUE, 1.25)
    arrow(c, 540, 503, 520, 476, GREEN, 1.0)
    arrow(c, 300, 445, 315, 445, BLUE, 1.2)
    arrow(c, 587, 445, 602, 445, BLUE, 1.2)

    # Coordinator row <-> work row: bounded work goes out, advice returns to
    # the controller, and only the committer writes one fenced revision.
    arrow(c, 334, 414, 334, 387, PURPLE, 1.1)
    arrow(c, 321, 387, 321, 414, GREEN, 1.0)
    arrow(c, 340, 356, 355, 356, BLUE, 1.25)
    arrow(c, 495, 387, 620, 414, BLUE, 1.25)

    # Durable effects return receipts through the capability gateways.
    ortho_arrow(c, [(836, 350), (842, 350), (842, 488), (726, 488), (726, 503)],
                PURPLE, 1.0)
    ortho_arrow(c, [(836, 370), (846, 370), (846, 494), (545, 494), (545, 503)],
                GREEN, 1.0)

    # Governed commercial facts only; no model receives CRM authority.
    arrow(c, 770, 318, 770, 276, ORANGE, 1.1)
    c.saveState()
    c.setStrokeColor(ORANGE)
    c.setLineWidth(1.0)
    for x in (285, 460):
        c.line(x, 210, x, 204)
    c.restoreState()
    ortho_arrow(c, [(115, 210), (115, 204), (770, 204), (770, 210)],
                ORANGE, 1.0)

    # Blackboard motherboard.
    box(c, 50, 594, 790, 58,
        "Blackboard motherboard - control, policy, audit and lifecycle authority",
        "Registry | signed capability/config leases | routing | budgets | release receipts | health/usage evidence | pause, quarantine, credential epoch, kill, retirement and reconciliation. " + COMPONENT_BOX("c.mother")[1] + " " + CLAIM("p2.return"),
        BLUE_SOFT, BLUE, None, 9.1, 6.35)
    # Three body lines fill this wide box, so its status sits on the title line.
    pill(c, 50 + 790 - 62, 594 + 58 - 25, COMPONENTS["c.mother"]["status"])

    def comp_box(key, x, y, w, h, fill, accent, title_size=8.0):
        title, body, status = COMPONENT_BOX(key)
        box(c, x, y, w, h, title, body, fill, accent, status, title_size)

    # Channel row inside SFDC24.
    comp_box("c.realtime", 60, 503, 130, 62, GREEN_SOFT, GREEN)
    comp_box("c.tts", 202, 503, 110, 62, ORANGE_SOFT, ORANGE)
    comp_box("c.web", 324, 503, 150, 62, BLUE_SOFT, BLUE)
    comp_box("c.wa", 486, 503, 118, 62, GREEN_SOFT, GREEN)
    comp_box("c.zoom", 616, 503, 220, 62, PURPLE_SOFT, PURPLE, 8.1)

    # Coordinator row.
    comp_box("c.arbiter", 60, 414, 240, 62, GREEN_SOFT, GREEN, 8.3)
    comp_box("c.controller", 315, 414, 272, 62, BLUE_SOFT, BLUE, 8.4)
    comp_box("c.ledger", 602, 414, 234, 62, GRAY_SOFT, INK, 8.2)

    # Work/effect row.
    comp_box("c.work", 60, 325, 280, 62, PURPLE_SOFT, PURPLE, 8.3)
    comp_box("c.commit", 355, 325, 150, 62, BLUE_SOFT, BLUE, 8.1)
    comp_box("c.outbox", 520, 325, 316, 62, ORANGE_SOFT, ORANGE, 8.2)

    # Sibling minibuses and the commercial plane.
    comp_box("c.converspan", 50, 210, 150, 66, PURPLE_SOFT, PURPLE)
    comp_box("c.nav", 210, 210, 175, 66, GREEN_SOFT, GREEN)
    comp_box("c.clients", 395, 210, 150, 66, BLUE_SOFT, BLUE)
    comp_box("c.charter", 555, 210, 135, 66, GRAY_SOFT, INK)
    comp_box("c.sf", 700, 210, 140, 66, ORANGE_SOFT, ORANGE)

    # Proof and sequence band, in page 1's style.
    proven = (CLAIM("p2.proven_main") + " " + REVIEW_SHORT("r.site216") + "; "
              + REVIEW_SHORT("r.site221") + ". " + CLAIM("p2.proven_rest"))
    box(c, 50, 76, 200, 122, "Proven toward the future", proven,
        GREEN_SOFT, GREEN, "PARTIAL", 8.5, 5.95)
    box(c, 262, 76, 250, 122, "Next promotions, in order", CLAIM("p2.order"),
        BLUE_SOFT, BLUE, "TARGET", 8.5, 5.95)
    box(c, 524, 76, 316, 122, "Converspan launch gate", CLAIM("p2.gate"),
        RED_SOFT, RED, "HELD", 8.5, 5.9)

    connector_label(c, 445, 583, "SFDC24 lease + config", BLUE)
    connector_label(c, 746, 585, "health / usage / evidence - no content", GREEN)
    connector_label(c, 620, 294, "independent leases", BLUE)
    connector_label(c, 400, 287, "bounded returns, no content", GREEN)
    connector_label(c, 123, 489, "duplex WebRTC", GREEN)
    connector_label(c, 257, 489, "TTS blob", ORANGE)
    connector_label(c, 322, 489, "TTS request", ORANGE)
    connector_label(c, 440, 489, "commands / SSE", BLUE)
    connector_label(c, 270, 400, "bounded work", PURPLE)
    connector_label(c, 392, 400, "advice returns", GREEN)
    connector_label(c, 560, 400, "one fenced revision", BLUE)
    connector_label(c, 790, 482, "speak / share / teardown", PURPLE)
    connector_label(c, 650, 494, "send + receipt", GREEN)
    connector_label(c, 770, 306, "SFDC24 authorized facts", ORANGE)
    connector_label(c, 455, 204, "client facts + entitlements", ORANGE)

    # Promotion ledger: every component, the ADR pass condition that promotes
    # it, and its gate.
    top = tech_panel_header(
        c, rx, ry, rw, rh,
        "Promotion ledger",
        "Status now, the ADR pass condition that promotes each component, and its gate (ADR acceptance matrix, G1-G9). TARGET is selected design, not a deployment claim.",
        60,
    )
    ledger_keys = ("c.mother", "c.realtime", "c.tts", "c.web", "c.wa", "c.zoom",
                   "c.arbiter", "c.controller", "c.ledger", "c.work", "c.commit",
                   "c.outbox", "c.charter", "c.converspan", "c.nav", "c.clients", "c.sf")
    accents = {"c.mother": BLUE, "c.realtime": GREEN, "c.tts": ORANGE, "c.web": BLUE,
               "c.wa": GREEN, "c.zoom": PURPLE, "c.arbiter": GREEN, "c.controller": BLUE,
               "c.ledger": INK, "c.work": PURPLE, "c.commit": BLUE, "c.outbox": ORANGE,
               "c.charter": INK, "c.converspan": PURPLE, "c.nav": GREEN,
               "c.clients": BLUE, "c.sf": ORANGE}
    rows = []
    for key in ledger_keys:
        title, test, status = COMPONENT_ROW(key)
        rows.append((title, test, status, accents[key]))
    for row in rows:
        top = tech_row(c, rx + 15, top, rw - 30, *row, row_h=32, body_offset=12.5)

    footer(
        c, 2,
        "Tabloid digital brief | Source: ADR-20260925 and its 2026-09-26 addendum, where each claim is cited",
        "TARGET is selected architecture, not a deployment or acceptance claim.",
    )
    c.showPage()


def build() -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    RENDERED_TEXT.clear()
    for used in USED.values():
        used.clear()
    # invariant=1 fixes ReportLab timestamps and document IDs so the committed
    # artifact has one reproducible digest across clean-checkout rebuilds.
    c = canvas.Canvas(
        str(OUT), pagesize=(PAGE_W, PAGE_H), pageCompression=1, invariant=1
    )
    c.setTitle("SFDC24 and Blackboard current and future architecture")
    c.setAuthor("Claude Code (2026-09-26 edition); 2026-09-25 edition by Codex")
    c.setSubject("Two-page evidence-bound architecture for realtime audio, live prototyping, Salesforce, WhatsApp, Zoom and governed client minibuses; ADR contract sha256:" + CONTRACT_SHA256)
    c.setKeywords("SFDC24, Blackboard, Converspan, minibus, OpenAI Realtime, Claude, Gemini, Salesforce, WhatsApp, Zoom, ADR-contract-" + CONTRACT_SHA256 + ", ADR-records-" + RECORDS_SHA256)
    draw_current(c)
    draw_future(c)
    rendered = "\n".join(RENDERED_TEXT)
    missing = [phrase for phrase in CONTRACT["required_pdf_phrases"] if phrase not in rendered]
    if missing:
        raise RuntimeError("PDF semantic drift; required rendered facts missing: " + repr(missing))
    check_all_rendered()
    c.save()
    return OUT


if __name__ == "__main__":
    print(build())
