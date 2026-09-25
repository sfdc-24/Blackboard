"""Build the two-page SFDC24 / Blackboard architecture brief.

The PDF is deliberately evidence-aware: LIVE, PARTIAL, HELD, and TARGET are
not interchangeable. It reads and validates the machine-readable semantic
contract in the accepted 2026-09-25 ADR, then binds that contract's digest into
the PDF. The result is small enough to use as the Drive architecture directive.

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
OUT = ROOT / "docs" / "SFDC24-BLACKBOARD-ARCHITECTURE-20260925.pdf"
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


def load_contract() -> tuple[dict, str]:
    """Read the closed semantic contract from the ADR and verify its anchors."""
    adr_text = ADR.read_text(encoding="utf-8")
    if adr_text.count(CONTRACT_START) != 1 or adr_text.count(CONTRACT_END) != 1:
        raise RuntimeError("ADR must contain exactly one architecture PDF contract")
    raw = adr_text.split(CONTRACT_START, 1)[1].split(CONTRACT_END, 1)[0].strip()
    if not raw.startswith("```json") or not raw.endswith("```"):
        raise RuntimeError("architecture PDF contract must be a fenced JSON object")
    contract = json.loads(raw[len("```json"): -len("```")].strip())
    if not isinstance(contract, dict) or set(contract) != CONTRACT_KEYS:
        raise RuntimeError("architecture PDF contract keys do not match the closed schema")
    if contract["schema_version"] != 1:
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
    missing = [phrase for phrase in contract["required_adr_phrases"]
               if adr_text.count(phrase) < 2]
    if missing:
        # Each anchor occurs once in the contract and must also occur in ADR prose.
        raise RuntimeError("ADR semantic drift; missing prose anchors: " + repr(missing))
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

STATUS_FILL = {
    "LIVE": GREEN_SOFT,
    "CURRENT": GREEN_SOFT,
    "PARTIAL": ORANGE_SOFT,
    "HELD": RED_SOFT,
    "TARGET": BLUE_SOFT,
    "OPTIONAL": PURPLE_SOFT,
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
                      title: str, body: str) -> float:
    paragraph(c, title, x + 15, y + h - 17, w - 30, 11.5, 13, INK, True)
    paragraph(c, body, x + 15, y + h - 38, w - 30, 7.0, 8.1, MUTED)
    return y + h - 70


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
        f"Evidence-bound snapshot through {CONTRACT['facts_refreshed_label']}: what is live, what was rehearsed, and what is still held",
        "CURRENT STATE",
    )
    principle_strip(c, [
        ("PRODUCTION CONTAINED", f"R5 serves {CONTRACT['production_traffic_percent']}%; advisor tag removed; next template is advisor-off", GREEN_SOFT, GREEN),
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
    arrow(c, 290, 270, 270, 270, GREEN, 1.0, True)
    ortho_arrow(c, [(557, 588), (557, 320), (220, 320), (220, 306)],
                PURPLE, 1.0, True)
    ortho_arrow(c, [(748, 588), (748, 560), (650, 560), (650, 548)],
                ORANGE, 1.0, True)

    # Surface layer.
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
        "Direct browser WebRTC host for microphone, transcript, approved host speech and interruption.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.25, 6.0)
    box(c, 230, 475, 160, 73,
        "Browser audio arbiter",
        "Owns one playback queue, captions, mute/listening state and audible-turn ordering.",
        BLUE_SOFT, BLUE, "CURRENT", 8.15, 6.0)
    box(c, 410, 475, 278, 73,
        f"Studio Controller - production {CONTRACT['production_controller_label']}",
        "FastAPI session authority: auth, topic route, questions, deadlines, revision fencing, typed events, command dedupe and provider coordination.",
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
        BLUE_SOFT, BLUE, "CURRENT", 8.3, 6.05)
    box(c, 460, 352, 176, 76,
        "Analyst + Muse",
        "Bounded analysis and creative directions support the build; they do not commit artifacts.",
        GREEN_SOFT, GREEN, "CURRENT", 8.3, 5.95)
    box(c, 661, 352, 174, 76,
        "Ordered artifact + event ledger",
        "The controller alone validates and commits; this ledger records versions, events, snapshots and receipts.",
        GRAY_SOFT, INK, "CURRENT", 8.2, 5.95)

    # Durable plane.
    box(c, 50, 228, 220, 78,
        "Blackboard + Apps Script",
        "Motherboard handoff, claims, review, audit, release evidence and exact row read-back. Not in the media hot path.",
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
        f"Site main {CONTRACT['site_commit_label']} exactly matches served homepage bytes; CI and rollback remain versioned.",
        PURPLE_SOFT, PURPLE, "LIVE", 8.15, 5.85)

    # Evidence band.
    box(c, 50, 91, 245, 111,
        "Authenticated R5 rehearsal - proven",
        "Session 200; /voice 200; eight TTS blobs reached the browser; /commands, /analyze and /inspire completed concurrently; landing page, demo form and Clean Signal direction became visible; session ended cleanly.",
        GREEN_SOFT, GREEN, "PARTIAL", 8.5, 6.0)
    box(c, 310, 91, 245, 111,
        "Gemini advisor containment - proven",
        "Unaccepted R6 was returned to zero traffic; adv tag removed; former URL 404; R5 restored to 100%; zero-traffic service template normalized to advisor=false. No /advise call was observed in R6 logs.",
        BLUE_SOFT, BLUE, "CURRENT", 8.5, 5.95)
    box(c, 570, 91, 265, 111,
        "Release held pending acceptance",
        "Microphone transcription, physical-speaker/human-heard audio, real Gemini TALK, five turns, two barge-ins, ten uninterrupted minutes, replay, Salesforce website facts/effects, delivered WhatsApp receipt, and live Zoom listen/speak/share.",
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
        "LIVE serving; CURRENT present; PARTIAL bounded proof; HELD disabled/frozen; TARGET future. Foundry excluded; Meta optional/off.",
    )
    rows = [
        ("GitHub Pages + typed browser UI", "Fast versioned delivery of dialogue, prototype and release surfaces.", "LIVE", PURPLE),
        ("Cloud Run + Python/FastAPI", "Managed controller runtime, TLS, bounded API surface and rollback revisions.", "LIVE", BLUE),
        ("OpenAI Realtime WebRTC", "Lowest-hop browser conversation path; full microphone/audio acceptance remains open.", "PARTIAL", GREEN),
        ("OpenAI TTS", "Female-capable natural speech for non-host lines through the browser queue.", "PARTIAL", ORANGE),
        ("Claude builder", "Single authoritative artifact proposal keeps revisions deterministic and reversible.", "CURRENT", BLUE),
        ("Gemini topic route", "Fast alternative TALK/RECAP provider behind the same controller policy.", "PARTIAL", GREEN),
        ("Gemini advisor", "Merged source is quarantined; hardening, exact-head review and a real dark probe are required.", "HELD", RED),
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
        f"Tabloid digital brief | Source: ADR-20260925 on PR265 | Facts refreshed {CONTRACT['facts_refreshed_label']}",
        "Transport proof is not human-heard end-to-end acceptance.",
    )
    c.showPage()


def draw_future(c: canvas.Canvas) -> None:
    header(
        c,
        "SFDC24 + Blackboard - future governed minibus architecture",
        "Blackboard stays the motherboard; SFDC24, Converspan and client minibuses inherit bounded capabilities and remain revocable",
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
    section_label(c, 43, 542, "SFDC24 service minibus", 150)
    section_label(c, 43, 201, "Sibling client minibuses and commercial plane", 270)

    # Outer minibus boundary.
    c.setFillColor(colors.HexColor("#FAFCFF"))
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.1)
    c.roundRect(45, 225, 802, 310, 11, stroke=1, fill=1)

    # Blackboard governs SFDC24 and each client minibus as siblings. The client
    # trunk stays outside the SFDC24 boundary so leases never appear to transit it.
    arrow(c, 445, 580, 445, 535, BLUE, 1.5)
    ortho_arrow(c, [(807, 535), (807, 560), (680, 560), (680, 580)],
                GREEN, 1.0, True)
    ortho_arrow(c, [(840, 610), (856, 610), (856, 197)],
                BLUE, 1.0, True)
    for x in (145, 360, 568):
        ortho_arrow(c, [(856, 197), (x, 197), (x, 191)],
                    BLUE, 0.9, True)
        ortho_arrow(c, [(x + 5, 191), (x + 5, 194), (852, 194), (856, 197)],
                    GREEN, 0.75, True)
    ortho_arrow(c, [(856, 197), (870, 197), (870, 625), (840, 625)],
                GREEN, 0.9, True)

    # Browser media and channel ingress. Realtime is duplex; TTS always returns
    # through the sole audio arbiter.
    arrow(c, 135, 452, 160, 413, BLUE, 1.1)
    arrow(c, 255, 413, 275, 452, GREEN, 1.25)
    arrow(c, 275, 452, 255, 413, GREEN, 1.25)
    arrow(c, 430, 413, 405, 452, ORANGE, 1.1)
    arrow(c, 375, 452, 255, 413, ORANGE, 1.1)
    arrow(c, 200, 375, 330, 375, BLUE, 1.25)
    arrow(c, 135, 452, 400, 413, BLUE, 0.9)
    arrow(c, 530, 452, 500, 413, GREEN, 1.0)
    arrow(c, 710, 452, 555, 413, PURPLE, 1.0)

    # Multi-agent work returns through the coordinator or the single writer.
    arrow(c, 420, 337, 195, 304, PURPLE, 1.05)
    arrow(c, 280, 304, 505, 337, GREEN, 1.0)
    ortho_arrow(c, [(300, 304), (300, 323), (420, 323), (420, 304)],
                BLUE, 1.35)
    ortho_arrow(c, [(500, 270), (600, 270), (600, 320), (720, 320), (720, 337)],
                BLUE, 1.25)
    arrow(c, 550, 337, 670, 304, ORANGE, 1.15)
    ortho_arrow(c, [(827, 375), (840, 375), (840, 526), (135, 526), (135, 514)],
                BLUE, 1.0)

    # Durable external effects return receipts through capability gateways.
    ortho_arrow(c, [(590, 304), (590, 440), (560, 440), (560, 452)],
                GREEN, 1.0)
    ortho_arrow(c, [(760, 304), (835, 304), (835, 430), (710, 430), (710, 452)],
                PURPLE, 1.0)

    # SFDC24 and client minibuses exchange only governed commercial facts with
    # Salesforce; no model receives direct CRM authority.
    ortho_arrow(c, [(760, 240), (840, 240), (840, 220), (755, 220), (755, 191)],
                ORANGE, 1.1)
    ortho_arrow(c, [(780, 191), (780, 215), (830, 215), (830, 247), (790, 247), (790, 240)],
                GREEN, 1.0)
    for x in (145, 360, 568):
        arrow(c, x, 91, x, 82, ORANGE, 0.9)
        arrow(c, x, 77, x, 91, GREEN, 0.9)
    ortho_arrow(c, [(145, 82), (755, 82), (755, 91)], ORANGE, 1.0)
    ortho_arrow(c, [(775, 91), (775, 77), (145, 77)], GREEN, 1.0)

    # Blackboard motherboard.
    box(c, 50, 580, 790, 70,
        "Blackboard motherboard - control, policy, audit and lifecycle authority",
        "Registry | signed capability/config leases | routing | budgets | release receipts | health/usage evidence | pause, quarantine, credential epoch, kill, retirement and reconciliation. Customer content stays in each tenant.",
        BLUE_SOFT, BLUE, "TARGET", 9.1, 6.35)

    # Channel and explicit media-provider row inside SFDC24.
    box(c, 65, 452, 135, 62,
        "Web / mobile",
        "Scoped sign-in, dialogue, captions and live prototype.",
        BLUE_SOFT, BLUE, "TARGET", 8.1)
    box(c, 210, 452, 130, 62,
        "OpenAI Realtime",
        "Direct duplex WebRTC: mic, host speech and barge-in.",
        GREEN_SOFT, GREEN, "TARGET", 8.0)
    box(c, 350, 452, 110, 62,
        "OpenAI TTS",
        "Approved secondary-voice blobs only.",
        ORANGE_SOFT, ORANGE, "TARGET", 8.0)
    box(c, 470, 452, 120, 62,
        "WhatsApp",
        "Signed/deduped async intake, status and approvals.",
        GREEN_SOFT, GREEN, "TARGET", 8.0)
    box(c, 600, 452, 227, 62,
        "Zoom RTMS + Ubuntu presenter",
        "Consented events; on-demand speak, present, share and teardown with receipts.",
        PURPLE_SOFT, PURPLE, "TARGET", 8.1)

    # Coordinator row.
    box(c, 65, 337, 225, 76,
        "One browser audio arbiter",
        "One owner controls playback, TTS queue, interrupt, cancel, captions and consent state.",
        GREEN_SOFT, GREEN, "TARGET", 8.4, 5.95)
    box(c, 330, 337, 255, 76,
        "Studio Controller turn and task arbiter",
        "Trusted identity/reference, immutable snapshot, deadlines, cancellation, fallbacks, revision fencing and event order.",
        BLUE_SOFT, BLUE, "TARGET", 8.55, 6.0)
    box(c, 615, 337, 212, 76,
        "Session, event + CAS ledger",
        "Durable commands, fenced revisions, replay receipts, unknown-outcome holds and snapshot repair.",
        GRAY_SOFT, INK, "TARGET", 8.3, 5.9)

    # Work/effect row.
    box(c, 65, 240, 260, 64,
        "Concurrent bounded work",
        "Claude build goes to the writer; Gemini/creative advice returns to the controller.",
        PURPLE_SOFT, PURPLE, "TARGET", 8.35, 5.85)
    box(c, 340, 240, 160, 64,
        "Single artifact committer",
        "Validates one proposal; writes one fenced revision.",
        BLUE_SOFT, BLUE, "TARGET", 8.15, 5.8)
    box(c, 515, 240, 312, 64,
        "Durable outbox + capability gateways",
        "Authorized, idempotent, read-back and reconciled WhatsApp, Zoom, Git, payment and Salesforce effects.",
        ORANGE_SOFT, ORANGE, "TARGET", 8.25, 5.75)

    # Client seeds and commercial engine.
    box(c, 50, 91, 190, 100,
        "Converspan minibus",
        "Web/logo/app design on the governed core. Production stays frozen until SFDC24 foundation gates pass.",
        PURPLE_SOFT, PURPLE, CONTRACT["converspan_production"], 8.45, 6.0)
    box(c, 255, 91, 210, 100,
        "Nav / steelworkson.ca minibus",
        "Nav signs in directly for sites/redesigns. Separate tenant identity, state, keys, limits and audit.",
        GREEN_SOFT, GREEN, "TARGET", 8.45, 5.95)
    box(c, 480, 91, 175, 100,
        "Additional client minibuses",
        "Same seed; reconfigurable, throttled, paused, upgraded or retired by Blackboard.",
        BLUE_SOFT, BLUE, "TARGET", 8.35, 5.9)
    box(c, 670, 91, 170, 100,
        "Salesforce commercial engine",
        "Campaign, lead, opportunity, tokenized payment reference, entitlement, case, success and renewal.",
        ORANGE_SOFT, ORANGE, "TARGET", 8.3, 5.85)

    connector_label(c, 445, 558, "SFDC24 lease + config", BLUE)
    connector_label(c, 746, 560, "health / usage / evidence - no content", GREEN)
    connector_label(c, 260, 433, "duplex WebRTC", GREEN)
    connector_label(c, 390, 433, "approved TTS request + blob", ORANGE)
    connector_label(c, 510, 526, "ordered SSE + snapshots + receipts", BLUE)
    connector_label(c, 340, 326, "Claude proposal to writer", BLUE)
    connector_label(c, 500, 315, "advice returns to controller", GREEN)
    connector_label(c, 670, 326, "one fenced revision", BLUE)
    connector_label(c, 545, 440, "send + receipt", GREEN)
    connector_label(c, 735, 430, "speak / share / teardown", PURPLE)
    connector_label(c, 765, 220, "SFDC24 events / authorized facts", ORANGE)
    connector_label(c, 455, 82, "client events / authorized facts + entitlements", ORANGE)
    connector_label(c, 690, 197, "independent leases; evidence returns", BLUE)

    # Future stack panel.
    top = tech_panel_header(
        c, rx, ry, rw, rh,
        "Selected future stack and why",
        "TARGET means selected future architecture, not deployed or accepted. Choices favor speed, safe autonomy and reversibility.",
    )
    rows = [
        ("Email OTP + scoped session tokens", "Target passwordless entry; OIDC-compatible boundary when external identity is selected.", "TARGET", GREEN),
        ("WebRTC + OpenAI Realtime", "Direct low-hop host audio, captions and barge-in; short-lived server-minted access.", "TARGET", GREEN),
        ("OpenAI TTS + one audio queue", "Welcoming secondary voices without competing playback authorities.", "TARGET", ORANGE),
        ("Cloud Run + FastAPI controller", "Managed TLS/scaling with deterministic session and commit authority.", "TARGET", BLUE),
        ("Typed JSON + DOM/SVG", "Fast, crisp and accessible live prototypes; no model HTML, script or eval.", "TARGET", PURPLE),
        ("Claude builder + Gemini analyst", "Parallel specialized value while only one validated proposal can commit.", "TARGET", PURPLE),
        ("GCS CAS + durable outbox", "Durable replay, revision control, effects, restart recovery and reconciliation.", "TARGET", BLUE),
        ("Blackboard signed leases", "Central capability ceiling, budgets, rewiring, revocation and sub-60s connected kill target.", "TARGET", BLUE),
        ("WhatsApp + Pipedream", "Low-friction mobile status/intake outside the continuous-audio path.", "TARGET", GREEN),
        ("Zoom RTMS + Ubuntu presenter", "Backend observer plus on-demand participant for speech, presentation and screen share.", "TARGET", PURPLE),
        ("Salesforce as CRM/success", "One attribution and customer-success spine across domains and client minibuses.", "TARGET", ORANGE),
        ("Payment-provider boundary (TBD)", "Provider owns checkout/card data; Salesforce stores tokenized commercial references only.", "TARGET", ORANGE),
    ]
    for row in rows:
        top = tech_row(c, rx + 15, top, rw - 30, *row, row_h=38)

    c.setFillColor(RED_SOFT)
    c.roundRect(rx + 15, ry + 10, rw - 30, 65, 8, stroke=0, fill=1)
    paragraph(c, "Non-waivable launch gate", rx + 27, ry + 62, rw - 54,
              7.4, 8.4, RED, True)
    paragraph(c,
              "No Converspan production onboarding until SFDC24 proves continuous audio, artifact replay, tenant isolation, durable effects, source-to-runtime identity and rollback.",
              rx + 27, ry + 45, rw - 54, 7.0, 8.0, INK)

    footer(
        c, 2,
        "Tabloid digital brief | Source: ADR-20260925 on PR265 | Review inputs: Claude, Gemini, Codex and Grok",
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
    c.setAuthor("Codex with Claude, Gemini and Grok review input")
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
