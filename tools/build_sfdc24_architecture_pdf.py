"""Build the two-page SFDC24 / Blackboard architecture brief.

The PDF is deliberately evidence-aware: LIVE, CURRENT, PARTIAL, REVIEW, DARK,
HELD, and TARGET are not interchangeable. It reads and validates the
machine-readable semantic contract in the accepted 2026-09-25 ADR (snapshot
refreshed by its 2026-09-26 addendum), then binds that contract's digest into
the PDF. The result is small enough to use as the Drive architecture directive.
The 2026-09-25 edition stays committed as a historical artifact; this script
builds the current edition only.

Clean-checkout dependency install:
    python -m pip install -r tools/requirements-architecture-pdf.txt
"""
from __future__ import annotations

import hashlib
import json
import math
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
             row_h: float = 41) -> float:
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.45)
    c.line(x, top - row_h, x + w, top - row_h)
    pill(c, x, top - 16, status)
    paragraph(c, title, x + 58, top - 3, w - 58, 7.5, 8.2, INK, True)
    paragraph(c, why, x + 58, top - 16, w - 58, 7.0, 7.9, MUTED)
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
        f"Evidence-bound snapshot through {CONTRACT['facts_refreshed_label']}: what is live, what the owner ran, what is in review, and what is still held",
        "CURRENT STATE",
    )
    principle_strip(c, [
        ("PRODUCTION CONTAINED", f"R5b serves {CONTRACT['production_traffic_percent']}%; rollback r5-0895605-g kept; r5c at 0%", GREEN_SOFT, GREEN),
        ("ONE AUDIO ARBITER", "Realtime host plus queued TTS; each line hears only its own response", BLUE_SOFT, BLUE),
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
    arrow(c, 290, 270, 270, 270, GREEN, 1.0, True)
    ortho_arrow(c, [(557, 588), (557, 320), (220, 320), (220, 306)],
                PURPLE, 1.0, True)
    ortho_arrow(c, [(748, 588), (748, 560), (650, 560), (650, 548)],
                ORANGE, 1.0, True)

    # Surface layer.
    box(c, 50, 588, 245, 64,
        "sfdc24.com authenticated owner",
        "Guided meeting, host nudge and nudge lifecycle (#209, #216, #221) on site main 88b2419; voice, text and live prototype.",
        BLUE_SOFT, BLUE, "LIVE", 8.6, 6.2)
    box(c, 310, 588, 145, 64,
        "WhatsApp",
        "Outbound accepted (HTTP 200); delivery and read remain unverified.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.3, 6.05)
    box(c, 470, 588, 175, 64,
        "Zoom + Ubuntu presenter",
        "OAuth/RTMS/presenter source exists; Ubuntu VM is stopped. No live meeting acceptance.",
        PURPLE_SOFT, PURPLE, "HELD", 8.1, 5.95)
    box(c, 660, 588, 175, 64,
        "Salesforce operator observation",
        "Writes paused (BLK-059). PlaygroundOrg showed 22 Leads; facts and effects stay gated.",
        ORANGE_SOFT, ORANGE, "PARTIAL", 8.1, 5.95)

    # Browser media and controller layer.
    box(c, 50, 475, 165, 73,
        "OpenAI Realtime",
        "Direct browser WebRTC host. Metadata echo on created and done probed live on gpt-realtime-2.1.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.25, 6.0)
    box(c, 230, 475, 160, 73,
        "Browser audio arbiter",
        "One playback queue; a line hears only its own response; 20 s talk deadline, aborted on end (#221).",
        BLUE_SOFT, BLUE, "LIVE", 8.15, 6.0)
    box(c, 410, 475, 278, 73,
        f"Studio Controller - production {CONTRACT['production_controller_label']}",
        "FastAPI session authority: auth, topic route, questions, deadlines, revision fencing, typed events and command dedupe. Cloud Run: 60 s request limit, concurrency 8, 1 CPU / 512Mi, builder claude-sonnet-5.",
        BLUE_SOFT, BLUE, "LIVE", 8.65, 6.15)
    box(c, 708, 475, 127, 73,
        "OpenAI TTS",
        "Architect and Muse speech returns as bounded browser audio blobs.",
        ORANGE_SOFT, ORANGE, "PARTIAL", 8.0, 5.8)

    # Parallel work and single commit layer.
    box(c, 50, 352, 202, 76,
        "TALK / RECAP route",
        "Topic routing can choose Claude, OpenAI or Gemini for bounded spoken text. Gemini owner-path proof pending; #270 prompts DARK.",
        PURPLE_SOFT, PURPLE, "PARTIAL", 8.3, 5.9)
    box(c, 267, 352, 178, 76,
        "Claude builder",
        "Only committable proposal. 26 Sep: one turn hit 504 at 60 s, then a 90 s lease 409'd five builds.",
        BLUE_SOFT, BLUE, "PARTIAL", 8.3, 6.05)
    box(c, 460, 352, 176, 76,
        "Analyst + Muse",
        "Bounded analysis and creative options; no commit. /analyze also hit 504 at 60 s.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.3, 5.95)
    box(c, 661, 352, 174, 76,
        "Ordered artifact + event ledger",
        "The controller alone validates and commits; this ledger records versions, events, snapshots and receipts.",
        GRAY_SOFT, INK, "CURRENT", 8.2, 5.95)

    # Durable plane.
    box(c, 50, 228, 220, 78,
        "Blackboard + Apps Script",
        "Handoff, claims, review, audit and exact row read-back; not in the media path. Review lane outage 25 Sep 22:38-23:30Z: Codex upstream 401; an app restart restored it.",
        ORANGE_SOFT, ORANGE, "CURRENT", 8.45, 6.05)
    box(c, 290, 228, 165, 78,
        "Pipedream + outbox",
        "Existing channel bridge. Deployed signing/dedupe and destination delivery remain unverified.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.25, 5.9)
    box(c, 475, 228, 175, 78,
        "GCS CAS session state",
        "Durable state, optimistic concurrency, replayable events and snapshot repair outside containers.",
        BLUE_SOFT, BLUE, "CURRENT", 8.25, 5.9)
    box(c, 670, 228, 165, 78,
        "GitHub + public site",
        f"Site main {CONTRACT['site_commit_label']} is served: two cache-busted samples each of voice, canvas and index matched main.",
        PURPLE_SOFT, PURPLE, "LIVE", 8.15, 5.85)

    # Evidence band.
    box(c, 50, 91, 245, 111,
        "Owner live run 26 Sep 01:00-01:10Z - proven",
        "Session, voice, TTS, concurrent analyze/inspire/commands, recap, rating and the summary-PDF handoff (HTTP 200; delivery unverified) worked; the owner: better than the last round. Defects: builder 504 at 60 s, then 409 x5 (about 3 min of architect silence); /analyze 504 at 60 s; canvas sparse after the first build.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.5, 6.0)
    box(c, 310, 91, 245, 111,
        "Fixes in review - not deployed",
        "Blackboard #272: 40 s builder client, no retries, max_tokens 4000, a timeout ends as a completed command, analyst 40 s, build now when the analyst asks (Cursor GO 8a9349e; Codex pending). r5d = 0895605 + #272 only; tests 62/24/84 pass. Site #222 step text; #223 builder hears the question. The owner moves traffic.",
        REVIEW_SOFT, ORANGE, "REVIEW", 8.5, 5.95)
    box(c, 570, 91, 265, 111,
        "Held pending gates",
        "#260 client workspaces: Gate 1 round 12 NO-GO at ecee267 (redaction before strip/cap, fetch lease deadline, cancel at send, advisor lease atomicity); #261 stacked; workspaces off, registry unseeded. DARK in main: #269 charter + quote PDF (no prices), Gemini advisor, #270 prompts. Zoom, Salesforce writes, WhatsApp delivery, Converspan.",
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

    # Current stack panel.
    top = tech_panel_header(
        c, rx, ry, rw, rh,
        "Current technologies and why",
        "LIVE serving; CURRENT present; PARTIAL bounded proof; REVIEW open PR; DARK merged, off; HELD gated; TARGET future. Foundry excluded; Meta optional/off.",
    )
    rows = [
        ("GitHub Pages + typed browser UI", "Fast versioned delivery of dialogue, prototype and release surfaces.", "LIVE", PURPLE),
        ("Cloud Run + Python/FastAPI", "Managed runtime and rollback revisions; its 60 s request limit is the budget every lane must fit.", "LIVE", BLUE),
        ("OpenAI Realtime WebRTC", "Lowest-hop browser conversation path; human-heard audio acceptance remains open.", "PARTIAL", GREEN),
        ("OpenAI TTS", "Female-capable natural speech for non-host lines through the browser queue.", "PARTIAL", ORANGE),
        ("Claude builder", "Single authoritative proposal; the 26 Sep run exposed an unbounded client (#272 in review).", "PARTIAL", BLUE),
        ("Gemini topic route", "Fast alternative TALK/RECAP provider behind the same controller policy.", "PARTIAL", GREEN),
        ("Gemini advisor", "Merged and off; hardening, exact-head review and a real dark probe are required.", "DARK", RED),
        ("GCS CAS + event ledger", "Persistent session state, revision fencing, replay and reconnect repair.", "CURRENT", BLUE),
        ("Blackboard + Apps Script", "Durable multi-agent command, evidence, handoff and audit plane.", "CURRENT", ORANGE),
        ("Pipedream + WhatsApp API", "Existing mobile bridge; delivery/read receipts require separate proof.", "PARTIAL", GREEN),
        ("Salesforce APIs", "Business/customer-success engine; writes paused (BLK-059), facts and effects gated.", "HELD", ORANGE),
        ("Zoom RTMS + Ubuntu presenter", "Correct split between media observer and real meeting participant; not accepted live.", "HELD", PURPLE),
    ]
    for row in rows:
        top = tech_row(c, rx + 15, top, rw - 30, *row, row_h=42)

    footer(
        c, 1,
        f"Tabloid digital brief | Source: ADR-20260925 + 26 Sep addendum | Facts refreshed {CONTRACT['facts_refreshed_label']}",
        "Transport proof is not human-heard end-to-end acceptance.",
    )
    c.showPage()


def draw_future(c: canvas.Canvas) -> None:
    header(
        c,
        "SFDC24 + Blackboard - future governed minibus architecture",
        f"Evidence-bound through {CONTRACT['facts_refreshed_label']}: how far each component has come, what promotes it, and who gates it",
        "FUTURE STATE",
    )
    principle_strip(c, [
        ("MOTHERBOARD COMMAND", "Leases, budgets, pause and kill stay central; the board and review gates run today", BLUE_SOFT, BLUE),
        ("SEAMLESS PRESENT MOMENT", "One turn timeline; response ownership and the talk deadline shipped in #221", GREEN_SOFT, GREEN),
        ("MULTI-AGENT, SINGLE COMMIT", "Claude builds; analyst and creative advise; the controller alone commits", PURPLE_SOFT, PURPLE),
        ("SFDC24 FIRST", "Converspan waits for the measured launch gate; Nav waits for #260 and #261", ORANGE_SOFT, ORANGE),
    ])
    (_, _, _, _), (rx, ry, rw, rh) = panel_shell(c)

    section_label(c, 43, 655, "Blackboard control and audit plane", 218)
    section_label(c, 43, 575, "SFDC24 service minibus", 150)
    section_label(c, 43, 297, "Sibling client minibuses and commercial plane", 270)

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
    ortho_arrow(c, [(840, 622), (856, 622), (856, 292), (145, 292), (145, 288)],
                BLUE, 1.0, True)
    for x in (360, 568):
        ortho_arrow(c, [(x, 292), (x, 288)], BLUE, 0.9, True)

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
    arrow(c, 760, 325, 760, 288, ORANGE, 1.1)
    c.saveState()
    c.setStrokeColor(ORANGE)
    c.setLineWidth(1.0)
    for x in (360, 568):
        c.line(x, 205, x, 199)
    c.restoreState()
    ortho_arrow(c, [(145, 205), (145, 199), (755, 199), (755, 205)],
                ORANGE, 1.0)

    # Blackboard motherboard.
    box(c, 50, 594, 790, 58,
        "Blackboard motherboard - control, policy, audit and lifecycle authority",
        "Registry | signed capability/config leases | routing | budgets | release receipts | health/usage evidence | pause, quarantine, kill and retirement. Now: board, claims, exact-head review receipts and row read-back run; registry, leases and kill switch are TARGET.",
        BLUE_SOFT, BLUE, None, 9.1, 6.35)
    # Two body lines fill this wide box, so its status sits on the title line.
    pill(c, 50 + 790 - 62, 594 + 58 - 25, "PARTIAL")

    # Channel row inside SFDC24.
    box(c, 60, 503, 130, 62,
        "OpenAI Realtime",
        "Now: live host over WebRTC; human-heard acceptance open.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.0)
    box(c, 202, 503, 110, 62,
        "OpenAI TTS",
        "Now: Architect and Muse blobs, one queue.",
        ORANGE_SOFT, ORANGE, "PARTIAL", 8.0)
    box(c, 324, 503, 150, 62,
        "Web / mobile + email OTP",
        "Now: invited-operator code sign-in; public visitors off.",
        BLUE_SOFT, BLUE, "PARTIAL", 8.0)
    box(c, 486, 503, 118, 62,
        "WhatsApp",
        "Now: outbound accepted; delivery unverified.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.0)
    box(c, 616, 503, 220, 62,
        "Zoom RTMS + Ubuntu presenter",
        "Now: source only; presenter VM stopped; no live meeting acceptance.",
        PURPLE_SOFT, PURPLE, "HELD", 8.1)

    # Coordinator row.
    box(c, 60, 414, 240, 62,
        "One browser audio arbiter",
        "Now: one queue; response-id ownership and a 20 s talk deadline (#221).",
        GREEN_SOFT, GREEN, "LIVE", 8.3)
    box(c, 315, 414, 272, 62,
        "Studio Controller turn and task arbiter",
        "Now: r5b serves 100%; lanes must fit Cloud Run's 60 s. r5d bounds the builder (#272).",
        BLUE_SOFT, BLUE, "PARTIAL", 8.4)
    box(c, 602, 414, 234, 62,
        "Session, event + CAS ledger",
        "Now: GCS CAS, fenced revisions, replay. Terminal outcome recovery is in #260.",
        GRAY_SOFT, INK, "PARTIAL", 8.2)

    # Work/effect row.
    box(c, 60, 325, 280, 62,
        "Concurrent bounded work",
        "Now: Claude builder, analyst and creative run in parallel; Gemini advisor DARK.",
        PURPLE_SOFT, PURPLE, "PARTIAL", 8.3)
    box(c, 355, 325, 150, 62,
        "Single artifact committer",
        "Now: validates one proposal; one fenced revision.",
        BLUE_SOFT, BLUE, "LIVE", 8.1)
    box(c, 520, 325, 316, 62,
        "Durable outbox + capability gateways",
        "Authorized, idempotent, read-back WhatsApp, Zoom, Git, payment and Salesforce effects. Now: not built.",
        ORANGE_SOFT, ORANGE, "TARGET", 8.2)

    # Client seeds and commercial engine.
    box(c, 50, 205, 190, 83,
        "Converspan minibus",
        "Web/logo/app design on the governed core. Production stays frozen until the measured launch gate passes.",
        PURPLE_SOFT, PURPLE, CONTRACT["converspan_production"], 8.45, 6.0)
    box(c, 255, 205, 210, 83,
        "Nav / steelworkson.ca minibus",
        "Now: #260 Gate 1 round 12 NO-GO (ecee267); #261 stacked; r5c operator slot at 0%; workspaces off.",
        GREEN_SOFT, GREEN, "HELD", 8.45, 5.95)
    box(c, 480, 205, 175, 83,
        "Additional client minibuses",
        "Same seed; reconfigurable, throttled, paused, upgraded or retired by Blackboard.",
        BLUE_SOFT, BLUE, "TARGET", 8.35, 5.9)
    box(c, 670, 205, 170, 83,
        "Salesforce commercial engine",
        "Now: charter + quote PDF DARK (#269, no prices); writes paused (BLK-059).",
        ORANGE_SOFT, ORANGE, "DARK", 8.3, 5.85)

    # Proof and sequence band, in page 1's style.
    box(c, 50, 80, 255, 110,
        "Proven toward the future",
        "Direct realtime path: guided meeting, host nudge and nudge lifecycle (#209, #216, #221) served on site main 88b2419. One audio arbiter: each realtime line binds by its metadata and ignores other responses; Realtime echoes that metadata (probed live 26 Sep). Single committer: builder, analyst and creative lanes ran concurrently in the owner's 26 Sep run. Exact-head gates held two bad heads and passed the repairs.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.5, 5.95)
    box(c, 318, 80, 262, 110,
        "Next promotions, in order",
        "1 r5d = 0895605 + #272 only (Codex GO; the owner moves traffic). 2 Owner rehearsal closes G1: 10 min, 5 turns, 2 barge-ins, 3 revisions. 3 #260 Gate 1, then #261 durable publication. 4 #269 repairs, then CX1 charter only (PDF and prices off). 5 CX2 priced quotes after the owner's price list. 6 Nav pilot. 7 Converspan onboarding after its gate. Traffic is always a separate GO.",
        BLUE_SOFT, BLUE, "TARGET", 8.5, 5.95)
    box(c, 593, 80, 247, 110,
        "Converspan launch gate",
        "Measured on production SFDC24 first: a 10 min owner session with 5 turns, 2 barge-ins and 3 revisions replayed from the ledger; zero 504s and zero 409 lease lockouts across 24 h of real sessions; #260 and #261 exact-head GO with cross-tenant, revocation and crash-replay negatives; served bytes and image digest match the reviewed commit; a rollback drill read back; a 24/48 h canary soak.",
        RED_SOFT, RED, "HELD", 8.5, 5.9)

    connector_label(c, 445, 583, "SFDC24 lease + config", BLUE)
    connector_label(c, 746, 585, "health / usage / evidence - no content", GREEN)
    connector_label(c, 470, 292, "independent leases; evidence returns", BLUE)
    connector_label(c, 123, 489, "duplex WebRTC", GREEN)
    connector_label(c, 257, 489, "TTS blob", ORANGE)
    connector_label(c, 322, 489, "TTS request", ORANGE)
    connector_label(c, 440, 489, "commands / SSE", BLUE)
    connector_label(c, 270, 400, "bounded work", PURPLE)
    connector_label(c, 392, 400, "advice returns", GREEN)
    connector_label(c, 560, 400, "one fenced revision", BLUE)
    connector_label(c, 790, 482, "speak / share / teardown", PURPLE)
    connector_label(c, 650, 494, "send + receipt", GREEN)
    connector_label(c, 760, 306, "SFDC24 authorized facts", ORANGE)
    connector_label(c, 455, 199, "client facts + entitlements", ORANGE)

    # Promotion ledger: every component, what promotes it, and who gates it.
    top = tech_panel_header(
        c, rx, ry, rw, rh,
        "Promotion ledger",
        "Status now, the test that promotes it, and who gates it. TARGET is selected design, not a deployment claim.",
        60,
    )
    rows = [
        ("Blackboard motherboard", "Promote when a signed expiring lease pauses and kills a zero-traffic minibus within 60 s. Gate: Codex + owner.", "PARTIAL", BLUE),
        ("OpenAI Realtime", "Promote on G1: human-heard 10 min, 5 turns, 2 barge-ins. Gate: owner rehearsal + Codex receipt.", "PARTIAL", GREEN),
        ("OpenAI TTS", "Promote on G1 with no overlapping voices and no unexplained silence. Gate: owner rehearsal.", "PARTIAL", ORANGE),
        ("Web / mobile + email OTP", "Promote when public visitors sign in by code under a measured cap. Gate: owner switch.", "PARTIAL", BLUE),
        ("WhatsApp", "Promote when one message yields one delivered status with a destination receipt. Gate: G8.", "PARTIAL", GREEN),
        ("Zoom RTMS + Ubuntu presenter", "Promote when a consented meeting hears, speaks, shares and tears down. Gate: owner entitlement + G8.", "HELD", PURPLE),
        ("One browser audio arbiter", "Promote on G1 barge-in evidence; response ownership shipped in #221. Gate: owner rehearsal.", "LIVE", GREEN),
        ("Studio Controller turn and task arbiter", "Promote r5d on zero 504s and zero 409 lockouts in real sessions. Gate: Codex GO on #272; owner traffic.", "PARTIAL", BLUE),
        ("Session, event + CAS ledger", "Promote when a restart yields one outcome and one receipt. Gate: Codex Gate 1 on #260.", "PARTIAL", INK),
        ("Concurrent bounded work", "Promote when the Gemini advisor dark-probes with no artifact authority. Gate: Codex exact-head.", "PARTIAL", PURPLE),
        ("Single artifact committer", "Promote when the final artifact rebuilds from the ledger after a restart. Gate: G1 replay receipt.", "LIVE", BLUE),
        ("Durable outbox + capability gateways", "Promote when a sandbox effect dispatches once, reads back and survives restart. Gate: G7 + Codex.", "TARGET", ORANGE),
        ("Converspan minibus", "Promote only when the launch gate passes in full. Gate: non-waivable.", CONTRACT["converspan_production"], PURPLE),
        ("Nav / steelworkson.ca minibus", "Promote after #260/#261 GO and zero-traffic cross-tenant negatives, then a pilot. Gate: Codex + owner.", "HELD", GREEN),
        ("Additional client minibuses", "Promote after the Converspan canary soaks 24/48 h with a rollback drill. Gate: owner.", "TARGET", BLUE),
        ("Salesforce commercial engine", "Promote read-only org-bound facts (G6), then sandbox writes (G7); priced quotes after CX2. Gate: owner.", "DARK", ORANGE),
    ]
    for row in rows:
        top = tech_row(c, rx + 15, top, rw - 30, *row, row_h=34)

    footer(
        c, 2,
        "Tabloid digital brief | Source: ADR-20260925 + 26 Sep addendum | Review inputs: Claude, Cursor, Gemini, Codex and Grok",
        "TARGET is selected architecture, not a deployment or acceptance claim.",
    )
    c.showPage()


def build() -> Path:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    RENDERED_TEXT.clear()
    # invariant=1 fixes ReportLab timestamps and document IDs so the committed
    # artifact has one reproducible digest across clean-checkout rebuilds.
    c = canvas.Canvas(
        str(OUT), pagesize=(PAGE_W, PAGE_H), pageCompression=1, invariant=1
    )
    c.setTitle("SFDC24 and Blackboard current and future architecture")
    c.setAuthor("Codex and Claude Code with Cursor, Gemini and Grok review input")
    c.setSubject("Two-page evidence-bound architecture for realtime audio, live prototyping, Salesforce, WhatsApp, Zoom and governed client minibuses; ADR contract sha256:" + CONTRACT_SHA256)
    c.setKeywords("SFDC24, Blackboard, Converspan, minibus, OpenAI Realtime, Claude, Gemini, Salesforce, WhatsApp, Zoom, ADR-contract-" + CONTRACT_SHA256)
    draw_current(c)
    draw_future(c)
    rendered = "\n".join(RENDERED_TEXT)
    missing = [phrase for phrase in CONTRACT["required_pdf_phrases"] if phrase not in rendered]
    if missing:
        raise RuntimeError("PDF semantic drift; required rendered facts missing: " + repr(missing))
    c.save()
    return OUT


if __name__ == "__main__":
    print(build())
