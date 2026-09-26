"""The end-of-session PDF: what was said, decided and built, from the session record.

Everything here is read from the controller's own session state. The only
visitor-supplied input is an optional PNG of the final canvas, which is
checked (signature, dimensions, size) before a PDF library ever parses it.
Text uses the PDF core fonts, which cover Latin-1 only, so every string is
reduced to printable Latin-1 first; fpdf2 escapes what it writes, so visitor
text can never become PDF operators.
"""
from __future__ import annotations

import base64
import binascii
import io
import re
import struct
from datetime import datetime, timezone

PNG_PREFIX = "data:image/png;base64,"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_MAX_BYTES = 1_500_000
PNG_MAX_SIDE = 4096

_PUNCTUATION = {
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    "…": "...", "•": "-", " ": " ", "→": "->", "←": "<-",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class DesignImageError(ValueError):
    """The design_png field is not an acceptable PNG."""


def decode_design_png(value) -> bytes | None:
    """The PNG bytes of a data URL, or None when absent. Raises DesignImageError."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.startswith(PNG_PREFIX):
        raise DesignImageError("design_png must be a data:image/png;base64 URL")
    encoded = value[len(PNG_PREFIX):]
    if len(encoded) > (PNG_MAX_BYTES * 4) // 3 + 4:
        raise DesignImageError("design_png exceeds %d bytes" % PNG_MAX_BYTES)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DesignImageError("design_png is not valid base64") from exc
    if len(raw) > PNG_MAX_BYTES:
        raise DesignImageError("design_png exceeds %d bytes" % PNG_MAX_BYTES)
    if len(raw) < 33 or not raw.startswith(PNG_SIGNATURE) or raw[12:16] != b"IHDR":
        raise DesignImageError("design_png is not a PNG")
    width, height = struct.unpack(">II", raw[16:24])
    if not (0 < width <= PNG_MAX_SIDE and 0 < height <= PNG_MAX_SIDE):
        raise DesignImageError("design_png must be at most %dx%d pixels" % (PNG_MAX_SIDE, PNG_MAX_SIDE))
    return raw


def clean(text, limit: int = 2000) -> str:
    """Printable Latin-1 text for the core fonts, never longer than `limit`."""
    value = str(text or "")
    for bad, good in _PUNCTUATION.items():
        value = value.replace(bad, good)
    value = _CONTROL.sub(" ", value.replace("\r\n", "\n").replace("\r", "\n"))
    value = value.encode("latin-1", "replace").decode("latin-1")
    return value[:limit]


def _outline(node: dict, depth: int = 0, out: list | None = None) -> list:
    out = [] if out is None else out
    if not isinstance(node, dict) or len(out) >= 80:
        return out
    label = clean(node.get("label"), 80)
    detail = clean(node.get("detail"), 120)
    line = "%s%s: %s" % ("    " * depth, clean(node.get("kind", "part"), 20), label)
    if detail:
        line += " (%s)" % detail
    out.append(line)
    for child in node.get("children") or []:
        _outline(child, depth + 1, out)
    return out


def _decided(state: dict) -> list:
    rows = []
    for q in state.get("questions") or []:
        if q.get("status") != "answered":
            continue
        chosen = next((o.get("label", "") for o in q.get("options") or []
                       if o.get("option_id") == q.get("selected_option")), "") or q.get("freeform_answer", "")
        rows.append("%s - %s" % (clean(q.get("prompt"), 200), clean(chosen, 200)))
    return rows


def build_summary_pdf(state: dict, *, design_png: bytes | None = None, price_table: dict | None = None,
                      prepared_for: str | None = None) -> bytes:
    """One PDF of the working session - the build plan and quote when the session
    has a charter. Raises DesignImageError for an unreadable PNG."""
    if _charter(state) is not None:
        return build_quote_pdf(state, design_png=design_png, price_table=price_table, prepared_for=prepared_for)
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_title("SFDC24 working session")
    pdf.set_author("SFDC24")
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def heading(text):
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(11, 31, 58)
        pdf.multi_cell(width, 7, clean(text, 120), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Helvetica", "", 10.5)

    def para(text, size=10.5):
        pdf.set_font("Helvetica", "", size)
        pdf.multi_cell(width, 5.5, clean(text), new_x="LMARGIN", new_y="NEXT")

    def bullets(lines):
        for line in lines:
            pdf.multi_cell(width, 5.5, "- " + clean(line), new_x="LMARGIN", new_y="NEXT")

    artifact = state.get("artifact") or {}
    created = int(state.get("created_at") or 0)
    day = datetime.fromtimestamp(created, timezone.utc).strftime("%B %d, %Y") if created else ""

    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(11, 31, 58)
    pdf.multi_cell(width, 10, "Your SFDC24 working session", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(90, 90, 90)
    subtitle = clean(artifact.get("label"), 200)
    pdf.multi_cell(width, 6, " - ".join(p for p in (subtitle, day) if p), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(30, 30, 30)

    recap = (state.get("recap") or {}).get("text")
    if recap:
        heading("Recap")
        para(recap)

    said = [t.get("text", "") for t in state.get("transcript") or [] if t.get("role", "visitor") == "visitor"]
    if said:
        heading("What you asked for")
        bullets([clean(s, 600) for s in said[-40:]])     # a whole spoken line: the utterance cap

    confirmed = [(e.get("payload") or {}).get("text", "") for e in state.get("events") or []
                 if e.get("type") == "confirm"]
    confirmed = [c for c in confirmed if c]
    if confirmed:
        heading("What the architect built")
        bullets([clean(c, 300) for c in confirmed[-30:]])

    decided = _decided(state)
    if decided:
        heading("Decisions")
        bullets(decided)

    heading("The final design")
    if design_png is not None:
        try:
            pdf.image(io.BytesIO(design_png), w=width)
        except Exception as exc:  # a PNG that passed the header checks but will not decode
            raise DesignImageError("design_png is not a readable PNG") from exc
        pdf.ln(2)
    outline = _outline(artifact)
    if outline:
        pdf.set_font("Courier", "", 9)
        for line in outline:
            pdf.multi_cell(width, 4.6, line, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10.5)

    model = state.get("model") or {}
    objects = model.get("objects") or []
    if objects:
        heading("Data model%s" % ((" - " + clean(model.get("domain"), 80)) if model.get("domain") else ""))
        names = {}
        for o in objects:
            names[o.get("id")] = o.get("name", "")
            fields = ", ".join("%s (%s)" % (f.get("name", ""), f.get("type", "")) for f in o.get("fields") or [])
            purpose = o.get("purpose") or ""
            para("%s%s%s" % (clean(o.get("name"), 80), (": " + clean(purpose, 200)) if purpose else "",
                             ("\n    Fields: " + clean(fields, 600)) if fields else ""))
        links = ["%s %s %s" % (clean(names.get(r.get("from"), ""), 80), clean(r.get("label"), 80),
                               clean(names.get(r.get("to"), ""), 80)) for r in model.get("relationships") or []]
        if links:
            pdf.set_font("Helvetica", "B", 10.5)
            pdf.multi_cell(width, 5.5, "How they connect", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10.5)
            bullets(links)
        findings = model.get("findings") or []
        if findings:
            pdf.set_font("Helvetica", "B", 10.5)
            pdf.multi_cell(width, 5.5, "Findings", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10.5)
            bullets([clean(f, 300) for f in findings])

    rating = state.get("rating") or {}
    if rating.get("score"):
        heading("How happy you were")
        para("%d out of 5%s" % (int(rating["score"]),
                                (" - " + clean(rating.get("comment"), 300)) if rating.get("comment") else ""))

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(width, 5, "Built live with you on sfdc24.com.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# --- the build plan and quote (a session with a charter) -----------------------
# Owner, 2026-09-25, on the emailed plan from his run: "it needs a lot more work
# to make it look like a quote rather than summary of discussion". A quote: the
# header (quote number, date, validity, who it is for), the project and its
# scope of work (the charter), priced line items, the payment terms, the support
# plan options and the next step; the discussion moves to an appendix.
PLAN_TERMS = (
    "50% to start build and test.",
    "50% on delivery and handover.",
    "Support plan, chosen at handover: subscription or on demand.",
)
PRICED_AFTER_REVIEW = "Priced after review"
TO_CONFIRM = "To confirm at kickoff"
NEXT_STEP = "Reply to this email to accept, or book a kickoff at sfdc24.com."
# A draft has nothing to accept yet (Codex R7: an unpriced quote is not acceptable).
DRAFT_NEXT_STEP = "Reply to this email with questions, or book a kickoff at sfdc24.com."
WATERMARK = "sfdc24.com"
VALID_DAYS = 30
SUPPORT_OPTIONS = (
    ("subscription", "Subscription", "Monthly support: updates, fixes and questions", "/ month"),
    ("on_demand", "On demand", "Hourly support, when you need it", "/ hour"),
)
LEVEL_WORDS = {0: TO_CONFIRM, 1: TO_CONFIRM, 2: "Clear", 3: "Confirmed"}

NAVY = (11, 31, 58)
INK = (30, 30, 30)
MUTE = (110, 116, 128)
RULE = (214, 220, 229)
FILL = (244, 246, 250)
MARK = (236, 240, 246)
# the 2026-09-26 redesign: an accent, soft fills and status colours, each with
# text that keeps at least 4.5:1 contrast on its fill
ACCENT = (0, 110, 138)
ACCENT_SOFT = (214, 236, 242)
ON_NAVY = (196, 210, 230)
PANEL = (242, 245, 250)
BAND = (247, 249, 252)
TOTAL_FILL = (234, 240, 248)
NEXT_FILL = (232, 244, 247)
SHADOW = (225, 230, 238)
GO_FILL, GO_INK = (220, 244, 228), (22, 101, 52)
CLEAR_FILL, CLEAR_INK = (224, 234, 250), (29, 64, 140)
DRAFT_FILL, DRAFT_INK = (255, 243, 205), (122, 82, 0)
DOT_1, DOT_2, DOT_3 = (236, 106, 94), (245, 191, 79), (98, 197, 84)
STATUS_READY = "READY TO ACCEPT"
STATUS_DRAFT = "DRAFT - PRICES TO CONFIRM"
FIGURE_CAPTION = "Your prototype, as built live in the session."
WIREFRAME_CAPTION = "The structure of your prototype, from the canvas."
TAX_NOTE = "Applicable taxes are added on the invoice."
MILESTONE_UNPRICED = ("Half of the agreed price", "The other half")
ACCEPT_LINE = "To accept this quote, sign below or reply to the email it came with."
DRAFT_LINE = ("This is a draft build plan for review. It becomes a quote you can accept once every "
              "line has its price.")
QUOTE_MAX_PAGES = 6
APPENDIX_ROOM = 70
FIGURE_MAX_SIDE = 1600


def _charter(state: dict) -> dict | None:
    charter = state.get("charter")
    if not isinstance(charter, dict) or not isinstance(charter.get("dimensions"), list) or not charter["dimensions"]:
        return None
    return charter


def quote_number(state: dict) -> str:
    """Q-YYYYMMDD-XXXXXX: the session's day and six hex digits of a hash of it.
    Short, stable for a session, and it never shows the session id."""
    import hashlib
    created = int(state.get("created_at") or 0)
    day = datetime.fromtimestamp(created, timezone.utc).strftime("%Y%m%d") if created else "00000000"
    seed = "sfdc24-quote:%s:%d" % (state.get("session_id") or "", created)
    return "Q-%s-%s" % (day, hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6].upper())


def _goal(state: dict) -> str:
    said = [str(t.get("text") or "").strip() for t in state.get("transcript") or []
            if isinstance(t, dict) and t.get("role", "visitor") == "visitor" and str(t.get("text") or "").strip()]
    first = next((s for s in said if len(s.split()) >= 3), said[0] if said else "")
    return clean(first, 240) if first else TO_CONFIRM


def _line_description(line, dims: dict, artifact: dict) -> str:
    _, _, default, source = line
    if source == "canvas":
        parts = [clean(c.get("label"), 30) for c in (artifact.get("children") or [])
                 if isinstance(c, dict) and clean(c.get("label"), 30).strip()][:6]
        return ("Covers: " + ", ".join(parts)) if parts else default
    if source and source in dims and dims[source].get("level", 0) >= 2 and dims[source].get("captured"):
        return clean(dims[source]["captured"], 140)
    return default


def build_quote_pdf(state: dict, *, design_png: bytes | None = None, price_table: dict | None = None,
                    prepared_for: str | None = None) -> bytes:
    """The build plan and quote for a session with a charter.

    Owner, 2026-09-26: the emailed PDF was "still all text"; he asked for "a nice
    looking quote PDF that has images, design elements and a professional
    format". Page one carries the brand band, the quote's status, the facts of
    the quote, the prototype itself and what the visitor gets; the scope, the
    priced lines, the two payment milestones, the support plans, acceptance and
    the next step follow; the conversation is the appendix. Every string still
    goes through clean(); every shape is drawn here, nothing is fetched; the
    only raster is the visitor's own checked PNG, scaled down before embedding.
    """
    from fpdf import FPDF
    from datetime import timedelta
    try:
        from workers.topics import charter_frame, quote_lines, session_type, TOPICS
    except ImportError:  # pragma: no cover - the app always has the workers package
        raise
    from .pricing import money

    charter = _charter(state)
    topic = charter.get("topic") if charter.get("topic") in TOPICS else ""
    number = quote_number(state)
    created = int(state.get("created_at") or 0)
    issued = datetime.fromtimestamp(created, timezone.utc) if created else datetime.now(timezone.utc)
    until = issued + timedelta(days=VALID_DAYS)
    artifact = state.get("artifact") or {}
    table = price_table or {}
    currency = table.get("currency", "")
    line_prices = (table.get("lines") or {}).get(topic or "other") or {}
    support_prices = table.get("support") or {}
    lines = quote_lines(topic)
    priced = bool(lines) and all(line_prices.get(line[0]) is not None for line in lines)
    total = round(sum(line_prices.get(line[0]) or 0 for line in lines), 2) if priced else 0.0
    picture = _scaled_png(design_png) if design_png is not None else None

    class Quote(FPDF):
        def header(self):
            # the watermark, under the content, on every page
            self.set_font("Helvetica", "B", 66)
            self.set_text_color(*MARK)
            wide = self.get_string_width(WATERMARK)
            with self.rotation(35, self.w / 2, self.h / 2):
                self.text(self.w / 2 - wide / 2, self.h / 2 + 8, WATERMARK)
            if self.page_no() > 1:
                self.set_fill_color(*NAVY)
                self.rect(0, 0, self.w, 4, "F")
                self.set_xy(self.l_margin, 9)
                self.set_font("Helvetica", "B", 8.5)
                self.set_text_color(*NAVY)
                self.cell(40, 5, WATERMARK)
                self.set_font("Helvetica", "", 8.5)
                self.set_text_color(*MUTE)
                self.cell(self.w - self.l_margin - self.r_margin - 40, 5,
                          "Build plan and quote  " + number, align="R")
                self.set_draw_color(*RULE)
                self.set_line_width(0.2)
                self.line(self.l_margin, 15.5, self.w - self.r_margin, 15.5)
                self.set_y(21)
            self.set_text_color(*INK)

        def footer(self):
            self.set_y(-14)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.set_y(-12)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*MUTE)
            third = (self.w - self.l_margin - self.r_margin) / 3
            self.cell(third, 5, "Quote " + number)
            self.cell(third, 5, WATERMARK, align="C")
            self.cell(third, 5, "Page %d of {nb}" % self.page_no(), align="R")

    pdf = Quote(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_title("SFDC24 build plan and quote " + number)
    pdf.set_author("SFDC24")
    pdf.set_creation_date(issued)          # the same session renders the same bytes
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def section(text, gap=6, need=22):
        # a heading never sits alone at the foot of a page: it keeps room for its first rows
        if pdf.get_y() + gap + need > pdf.page_break_trigger:
            pdf.add_page()
        pdf.ln(gap)
        y = pdf.get_y()
        pdf.set_fill_color(*ACCENT)
        pdf.rect(pdf.l_margin, y + 1.2, 1.4, 5, "F")
        pdf.set_xy(pdf.l_margin + 3.6, y)
        pdf.set_font("Helvetica", "B", 12.5)
        pdf.set_text_color(*NAVY)
        pdf.cell(width - 3.6, 7.4, clean(text, 80), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.4)
        pdf.set_text_color(*INK)

    def pair(label, value, label_w=34, size=10.5, x=None, w=None):
        x = pdf.l_margin if x is None else x
        w = width if w is None else w
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*MUTE)
        y = pdf.get_y()
        pdf.set_x(x)
        pdf.cell(label_w, 5.8, clean(label, 40))
        pdf.set_font("Helvetica", "", size)
        pdf.set_text_color(*INK)
        pdf.set_xy(x + label_w, y)
        pdf.multi_cell(w - label_w, 5.8, clean(value, 400), new_x="LMARGIN", new_y="NEXT")

    def grid(cells, widths, styles, *, aligns=None, colors=None, fill=None, rule=RULE, rule_w=0.2,
             pad=2.2, line_h=5.0, repeat=None):
        """One row of a ruled table; wraps each cell and keeps the row on one page."""
        aligns = aligns or ["L"] * len(cells)
        colors = colors or [INK] * len(cells)
        rows_needed = []
        for (style, size), text, w in zip(styles, cells, widths):
            pdf.set_font("Helvetica", style, size)
            rows_needed.append(max(1, len(pdf.multi_cell(w - 2 * pad, line_h, text, dry_run=True, output="LINES"))))
        h = max(rows_needed) * line_h + 2 * pad
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()
            if repeat:
                repeat()
        x0, y0 = pdf.l_margin, pdf.get_y()
        if fill:
            pdf.set_fill_color(*fill)
            pdf.rect(x0, y0, sum(widths), h, "F")
        x = x0
        for (style, size), text, w, align, color in zip(styles, cells, widths, aligns, colors):
            pdf.set_font("Helvetica", style, size)
            pdf.set_text_color(*color)
            pdf.set_xy(x + pad, y0 + pad)
            pdf.multi_cell(w - 2 * pad, line_h, text, align=align, new_x="LMARGIN", new_y="NEXT")
            x += w
        if rule:
            pdf.set_draw_color(*rule)
            pdf.set_line_width(rule_w)
            pdf.line(x0, y0 + h, x0 + sum(widths), y0 + h)
        pdf.set_xy(x0, y0 + h)
        pdf.set_text_color(*INK)
        return y0, h

    def pill(x, y, text, fill, ink, size=7.5, align_right=False):
        pdf.set_font("Helvetica", "B", size)
        w = pdf.get_string_width(text) + 5
        if align_right:
            x -= w
        pdf.set_fill_color(*fill)
        pdf.rect(x, y, w, size * 0.62, "F", round_corners=True, corner_radius=size * 0.31)
        pdf.set_text_color(*ink)
        pdf.set_xy(x, y)
        pdf.cell(w, size * 0.62, text, align="C")
        pdf.set_text_color(*INK)
        return w

    # --- the brand band: the SFDC24 mark and wordmark, drawn, not fetched ---
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, 38, "F")
    pdf.set_fill_color(*ACCENT)
    pdf.rect(0, 38, pdf.w, 1.6, "F")
    _brand_mark(pdf, pdf.l_margin, 11, 15)
    pdf.set_xy(pdf.l_margin + 19, 11.2)
    pdf.set_font("Helvetica", "B", 19)
    pdf.set_text_color(255, 255, 255)
    pdf.set_char_spacing(0.8)
    pdf.cell(60, 8, "SFDC24")
    pdf.set_char_spacing(0)
    pdf.set_xy(pdf.l_margin + 19, 19.6)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*ON_NAVY)
    pdf.cell(60, 5, "Built live with you, on " + WATERMARK)
    right = pdf.w - pdf.r_margin
    pdf.set_xy(right - 70, 11.4)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*ON_NAVY)
    pdf.cell(70, 5, "QUOTE", align="R")
    pdf.set_xy(right - 70, 16.6)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(70, 6, number, align="R")
    pdf.set_xy(right - 70, 23.4)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*ON_NAVY)
    pdf.cell(70, 5, issued.strftime("%B %d, %Y"), align="R")

    # --- title and status ---
    pdf.set_xy(pdf.l_margin, 47)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(*NAVY)
    pdf.cell(width - 62, 11, "Build plan and quote")
    if priced:
        pill(right, 49.6, STATUS_READY, GO_FILL, GO_INK, size=8, align_right=True)
    else:
        pill(right, 49.6, STATUS_DRAFT, DRAFT_FILL, DRAFT_INK, size=8, align_right=True)
    pdf.set_xy(pdf.l_margin, 59)

    # --- the facts of the quote, in one panel ---
    facts = [("Quote", number), ("Date", issued.strftime("%B %d, %Y")),
             ("Validity", "Valid for %d days, until %s" % (VALID_DAYS, until.strftime("%B %d, %Y")))]
    if prepared_for:
        facts.append(("Prepared for", prepared_for))
    facts.append(("Session type", session_type(topic)))
    panel_y = pdf.get_y()
    panel_h = 4 + 5.8 * len(facts) + 3
    pdf.set_fill_color(*PANEL)
    pdf.rect(pdf.l_margin, panel_y, width, panel_h, "F", round_corners=True, corner_radius=2.5)
    pdf.set_xy(pdf.l_margin, panel_y + 3.5)
    for label, value in facts:
        pair(label, value, label_w=32, x=pdf.l_margin + 5, w=width - 10)
    pdf.set_y(panel_y + panel_h + 5)

    # --- the prototype and what you get ---
    fig_w, side_x = 104, pdf.l_margin + 110
    top = pdf.get_y()
    fig_h = _figure(pdf, pdf.l_margin, top, fig_w, 70, picture, artifact)
    pdf.set_xy(pdf.l_margin, top + fig_h + 1.5)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*MUTE)
    pdf.cell(fig_w, 4.5, FIGURE_CAPTION if picture else WIREFRAME_CAPTION)
    pdf.set_xy(side_x, top)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.set_text_color(*NAVY)
    pdf.cell(width - 110, 6, "What you get", new_x="LMARGIN", new_y="NEXT")
    y = top + 8
    for line in lines:
        _icon(pdf, line[0], side_x + 4.5, y + 4.2, 4.2)
        pdf.set_xy(side_x + 11, y + 0.6)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*INK)
        pdf.cell(width - 121, 4.6, _fit(pdf, clean(line[1], 40), width - 121))
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*MUTE)
        wrapped = pdf.multi_cell(width - 121, 3.8, clean(line[2], 90), dry_run=True, output="LINES")[:2]
        if len(wrapped) == 2:
            rest = clean(line[2], 90)[len(" ".join(wrapped[:1])):].strip()
            wrapped[1] = _fit(pdf, rest, width - 121)
        for k, text in enumerate(wrapped):
            pdf.set_xy(side_x + 11, y + 5.0 + k * 3.8)
            pdf.cell(width - 121, 3.8, text)
        y += 7.4 + 3.8 * max(1, len(wrapped))
    pdf.set_text_color(*INK)
    pdf.set_y(max(top + fig_h + 7, y + 2))

    # --- the project and its scope of work ---
    section("Project", need=18)
    pair("Goal", _goal(state))

    section("Scope of work", need=30)
    frame = charter_frame(topic)
    labels = {d[0]: d[1] for d in frame}
    dims = {}
    for d in charter["dimensions"]:
        if isinstance(d, dict) and d.get("id") in labels:
            level = d.get("level") if type(d.get("level")) is int and d.get("level") in LEVEL_WORDS else 0
            dims[d["id"]] = {"level": level, "captured": clean(d.get("captured"), 140)}
    scope_w = (44, width - 44 - 30, 30)
    for i, (did, label, covers) in enumerate(frame):
        entry = dims.get(did, {"level": 0, "captured": ""})
        if entry["level"] >= 2 and entry["captured"]:
            body = entry["captured"]
        elif entry["captured"]:
            body = "Noted so far: " + entry["captured"]
        else:
            body = covers[:1].upper() + covers[1:]
        y0, h = grid((clean(label, 40), body, ""), scope_w, (("B", 10), ("", 10), ("", 8)),
                     aligns=("L", "L", "R"), colors=(INK, INK if entry["level"] >= 1 else MUTE, MUTE),
                     fill=BAND if i % 2 == 0 else None, rule=None, pad=2.0, line_h=4.8)
        word = LEVEL_WORDS[entry["level"]]
        fill, ink = ((GO_FILL, GO_INK) if entry["level"] >= 3 else (CLEAR_FILL, CLEAR_INK) if entry["level"] == 2
                     else (DRAFT_FILL, DRAFT_INK))
        pill(pdf.l_margin + width - 2, y0 + 2.0, word, fill, ink, size=7, align_right=True)
        pdf.set_xy(pdf.l_margin, y0 + h)
    if charter.get("next"):
        pdf.ln(1.5)
        pdf.set_font("Helvetica", "I", 9.5)
        pdf.set_text_color(*MUTE)
        pdf.multi_cell(width, 5, "Next to settle: " + clean(charter.get("next"), 160), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

    # --- line items ---
    section("Line items", need=40)
    widths = (10, 44, width - 10 - 44 - 40, 40)

    def table_head():
        grid(("#", "Item", "Description", "Price"), widths, (("B", 8.5),) * 4, aligns=("L", "L", "L", "R"),
             colors=((255, 255, 255),) * 4, fill=NAVY, rule=None, pad=2.2)

    table_head()
    for i, line in enumerate(lines):
        amount = line_prices.get(line[0])
        grid(("%d" % (i + 1), line[1], _line_description(line, dims, artifact),
              money(amount, currency) if amount is not None else PRICED_AFTER_REVIEW), widths,
             (("", 9), ("B", 10), ("", 9.5), ("B" if amount is not None else "", 10 if amount is not None else 9)),
             aligns=("L", "L", "L", "R"), colors=(MUTE, INK, INK, INK if amount is not None else MUTE),
             fill=BAND if i % 2 == 1 else None, repeat=table_head)
    if priced:
        y0, h = grid(("", "Total", "", money(total, currency)), widths, (("", 10), ("B", 11), ("", 10), ("B", 11.5)),
                     aligns=("L", "L", "L", "R"), colors=(INK, NAVY, INK, NAVY), fill=TOTAL_FILL, rule=NAVY,
                     rule_w=0.5, pad=2.6)
        pdf.set_draw_color(*NAVY)
        pdf.set_line_width(0.5)
        pdf.line(pdf.l_margin, y0, pdf.l_margin + width, y0)
        pdf.ln(1.2)
        pdf.set_font("Helvetica", "I", 8.5)
        pdf.set_text_color(*MUTE)
        pdf.cell(width, 4.5, TAX_NOTE, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

    # --- payment milestones: two steps on one line ---
    section("Payment milestones", need=30)
    half = round(total / 2, 2)
    step_w = width / 2
    y = pdf.get_y() + 1
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin + 9.5, y + 4.5, pdf.l_margin + step_w + 0.5, y + 4.5)
    for i, term in enumerate(PLAN_TERMS[:2]):
        x = pdf.l_margin + i * step_w
        pdf.set_fill_color(*NAVY)
        pdf.circle(x + 5, y + 4.5, 4.2, "F")
        pdf.set_xy(x + 0.8, y + 2.1)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(8.4, 5, str(i + 1), align="C")
        pdf.set_xy(x, y + 11)
        pdf.set_font("Helvetica", "B", 10.5)
        pdf.set_text_color(*INK)
        pdf.cell(step_w - 4, 5.4, term)
        pdf.set_xy(x, y + 17)
        if priced:
            pdf.set_font("Helvetica", "B", 12)
            pdf.set_text_color(*NAVY)
            pdf.cell(step_w - 4, 6, money(half if i == 0 else round(total - half, 2), currency))
        else:
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*MUTE)
            pdf.cell(step_w - 4, 6, MILESTONE_UNPRICED[i])
    pdf.set_text_color(*INK)
    pdf.set_xy(pdf.l_margin, y + 25)

    # --- support plan options: two cards ---
    section("Support plan options", need=44)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(width, 5.4, PLAN_TERMS[2], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    gap, card_h = 6, 26
    card_w = (width - gap) / 2
    y = pdf.get_y()
    for i, (key, name, what, per) in enumerate(SUPPORT_OPTIONS):
        x = pdf.l_margin + i * (card_w + gap)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.3)
        pdf.set_fill_color(255, 255, 255)
        pdf.rect(x, y, card_w, card_h, "DF", round_corners=True, corner_radius=2.5)
        pdf.set_fill_color(*ACCENT)
        pdf.rect(x, y, card_w, 1.6, "F")
        pdf.set_xy(x + 5, y + 4.2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*NAVY)
        pdf.cell(card_w - 10, 5.6, name)
        pdf.set_xy(x + 5, y + 10.2)
        pdf.set_font("Helvetica", "", 8.8)
        pdf.set_text_color(*MUTE)
        pdf.cell(card_w - 10, 4.6, what)
        amount = support_prices.get(key)
        pdf.set_xy(x + 5, y + 17)
        if amount is not None:
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(*INK)
            pdf.cell(card_w - 10, 6, money(amount, currency) + " " + per)
        else:
            pdf.set_font("Helvetica", "", 9.5)
            pdf.set_text_color(*MUTE)
            pdf.cell(card_w - 10, 6, PRICED_AFTER_REVIEW)
    pdf.set_text_color(*INK)
    pdf.set_xy(pdf.l_margin, y + card_h + 2)

    # --- acceptance: only a fully priced quote can be accepted ---
    if priced:
        section("Acceptance", need=30)
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*MUTE)
        pdf.multi_cell(width, 5, ACCEPT_LINE, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(7)
        y = pdf.get_y()
        col = (width - 12) / 3
        pdf.set_draw_color(*INK)
        pdf.set_line_width(0.3)
        for i, label in enumerate(("Name", "Signature", "Date")):
            x = pdf.l_margin + i * (col + 6)
            pdf.line(x, y + 6, x + col, y + 6)
            pdf.set_xy(x, y + 7)
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(col, 4, label)
        pdf.set_text_color(*INK)
        pdf.set_xy(pdf.l_margin, y + 13)
    else:
        section("Draft for review", need=20)
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(width, 5, DRAFT_LINE, new_x="LMARGIN", new_y="NEXT")

    # --- next step, in a callout ---
    section("Next step", need=18)
    y = pdf.get_y()
    pdf.set_fill_color(*NEXT_FILL)
    pdf.rect(pdf.l_margin, y, width, 12, "F", round_corners=True, corner_radius=2)
    pdf.set_fill_color(*ACCENT)
    pdf.rect(pdf.l_margin, y, 1.6, 12, "F")
    pdf.set_xy(pdf.l_margin + 6, y + 3.2)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*NAVY)
    pdf.cell(width - 10, 6, NEXT_STEP if priced else DRAFT_NEXT_STEP)
    pdf.set_text_color(*INK)
    pdf.set_xy(pdf.l_margin, y + 14)

    # --- appendix: session notes (a new page only when this one is nearly full) ---
    if pdf.get_y() + APPENDIX_ROOM > pdf.page_break_trigger:
        pdf.add_page()
    else:
        pdf.ln(8)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.4)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
        pdf.ln(6)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*NAVY)
    pdf.cell(width, 9, "Session notes", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*MUTE)
    pdf.multi_cell(width, 5, "A short record of the conversation behind this plan.", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)

    def notes(title, items):
        if not items:
            return
        section(title, gap=4)
        pdf.set_font("Helvetica", "", 9.5)
        for item in items:
            pdf.multi_cell(width, 5, "- " + item, new_x="LMARGIN", new_y="NEXT")

    recap = (state.get("recap") or {}).get("text")
    if recap:
        section("Recap", gap=4)
        pdf.set_font("Helvetica", "", 9.5)
        pdf.multi_cell(width, 5, clean(recap, 1200), new_x="LMARGIN", new_y="NEXT")
    said = [t.get("text", "") for t in state.get("transcript") or [] if t.get("role", "visitor") == "visitor"]
    notes("What you asked for", [clean(s, 300) for s in said[-8:]])
    built = [(e.get("payload") or {}).get("text", "") for e in state.get("events") or [] if e.get("type") == "confirm"]
    notes("What the architect built", [clean(c, 200) for c in built if c][-8:])
    notes("Decisions", _decided(state)[-8:])
    outline = _outline(artifact)[:30]
    if outline:
        section("The canvas", gap=4)
        pdf.set_font("Courier", "", 8.5)
        for line in outline:
            pdf.multi_cell(width, 4.4, line, new_x="LMARGIN", new_y="NEXT")
    if pdf.pages_count > QUOTE_MAX_PAGES:     # bounded: every list above is capped; this is the backstop
        raise ValueError("the quote ran to %d pages" % pdf.pages_count)
    return bytes(pdf.output())


def _fit(pdf, text: str, w: float) -> str:
    """The text, shortened with an ellipsis until it fits `w` in the current font."""
    if pdf.get_string_width(text) <= w:
        return text
    while text and pdf.get_string_width(text + "...") > w:
        text = text[:-1]
    return text.rstrip() + "..."


def _scaled_png(raw: bytes) -> bytes:
    """The visitor's checked PNG, at most FIGURE_MAX_SIDE pixels on its long side:
    it bounds the PDF's size and the work of embedding it. Raises DesignImageError."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            img = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img
            if max(img.size) > FIGURE_MAX_SIDE:
                img.thumbnail((FIGURE_MAX_SIDE, FIGURE_MAX_SIDE))
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=False)
            return out.getvalue()
    except DesignImageError:
        raise
    except Exception as exc:  # a PNG that passed the header checks but will not decode
        raise DesignImageError("design_png is not a readable PNG") from exc


def _figure(pdf, x: float, y: float, w: float, h_max: float, picture: bytes | None, artifact: dict) -> float:
    """The prototype in a frame: the visitor's snapshot, fitted, or a wireframe of
    the canvas's top-level parts. Returns the frame's height."""
    if picture:
        pw, ph = struct.unpack(">II", picture[16:24])
        inner_w, inner_h = w - 6, h_max - 6
        scale = min(inner_w / pw, inner_h / ph)
        iw, ih = pw * scale, ph * scale
        h = ih + 6
        pdf.set_fill_color(*SHADOW)
        pdf.rect(x + 1.2, y + 1.2, w, h, "F", round_corners=True, corner_radius=2)
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, h, "DF", round_corners=True, corner_radius=2)
        try:
            pdf.image(io.BytesIO(picture), x=x + (w - iw) / 2, y=y + 3, w=iw, h=ih,
                      alt_text="The prototype built live in the session")
        except Exception as exc:
            raise DesignImageError("design_png is not a readable PNG") from exc
        return h
    parts = [clean(c.get("label"), 30) for c in (artifact.get("children") or [])
             if isinstance(c, dict) and clean(c.get("label"), 30).strip()][:6]
    h = 56
    pdf.set_fill_color(*PANEL)
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.3)
    pdf.rect(x, y, w, h, "DF", round_corners=True, corner_radius=2)
    pdf.set_fill_color(*NAVY)
    pdf.rect(x, y, w, 6, "F", round_corners=True, corner_radius=2)
    for i, dot in enumerate((DOT_1, DOT_2, DOT_3)):
        pdf.set_fill_color(*dot)
        pdf.circle(x + 4 + i * 3.6, y + 3, 0.9, "F")
    pdf.set_xy(x + 14, y + 0.8)
    pdf.set_font("Helvetica", "B", 7.5)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(w - 18, 4.4, clean(artifact.get("label"), 40) or "Your prototype")
    rows = parts or ["Your first build appears here"]
    row_h = min(9.0, (h - 10) / len(rows) - 1.4)
    for i, name in enumerate(rows):
        ry = y + 8.5 + i * (row_h + 1.4)
        pdf.set_fill_color(255, 255, 255)
        pdf.rect(x + 4, ry, w - 8, row_h, "F", round_corners=True, corner_radius=1.2)
        pdf.set_fill_color(*ACCENT_SOFT)
        pdf.rect(x + 6, ry + row_h / 2 - 1.2, 2.4, 2.4, "F")
        pdf.set_xy(x + 10, ry + row_h / 2 - 2.4)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*INK)
        pdf.cell(w - 16, 4.8, name)
    pdf.set_text_color(*INK)
    return h


def _brand_mark(pdf, x: float, y: float, s: float) -> None:
    """The SFDC24 mark: a rounded tile with three rising bars, in vector shapes."""
    pdf.set_fill_color(*ACCENT)
    pdf.rect(x, y, s, s, "F", round_corners=True, corner_radius=s * 0.22)
    pdf.set_fill_color(255, 255, 255)
    bar = s * 0.16
    for i, frac in enumerate((0.35, 0.55, 0.75)):
        bh = s * frac
        pdf.rect(x + s * 0.2 + i * (bar + s * 0.08), y + s * 0.84 - bh, bar, bh, "F")


def _icon(pdf, kind: str, cx: float, cy: float, r: float) -> None:
    """A small deliverable glyph in an accent disc, drawn from lines and shapes."""
    pdf.set_fill_color(*ACCENT_SOFT)
    pdf.circle(cx, cy, r, "F")
    pdf.set_draw_color(*NAVY)
    pdf.set_fill_color(*NAVY)
    pdf.set_line_width(0.45)
    u = r * 0.5
    if kind == "discovery":                                   # a magnifier
        pdf.circle(cx - u * 0.25, cy - u * 0.25, u * 0.62, "D")
        pdf.line(cx + u * 0.2, cy + u * 0.2, cx + u * 0.9, cy + u * 0.9)
    elif kind == "design":                                    # three swatches
        for i, dx in enumerate((-0.7, 0.0, 0.7)):
            pdf.set_fill_color(*(ACCENT, NAVY, DOT_1)[i])
            pdf.circle(cx + dx * u, cy + (0.25 if i == 1 else -0.2) * u, u * 0.42, "F")
    elif kind in ("build", "configuration", "model"):         # stacked blocks
        pdf.rect(cx - u * 0.9, cy + u * 0.1, u * 0.8, u * 0.7, "F")
        pdf.rect(cx + u * 0.1, cy + u * 0.1, u * 0.8, u * 0.7, "F")
        pdf.rect(cx - u * 0.4, cy - u * 0.8, u * 0.8, u * 0.7, "F")
    elif kind in ("test", "testing"):                         # a check mark
        pdf.set_line_width(0.7)
        pdf.polyline([(cx - u * 0.8, cy), (cx - u * 0.2, cy + u * 0.6), (cx + u * 0.9, cy - u * 0.6)])
    elif kind == "handover":                                  # a key
        pdf.circle(cx - u * 0.45, cy, u * 0.42, "D")
        pdf.line(cx - u * 0.03, cy, cx + u * 0.95, cy)
        pdf.line(cx + u * 0.55, cy, cx + u * 0.55, cy + u * 0.35)
        pdf.line(cx + u * 0.85, cy, cx + u * 0.85, cy + u * 0.35)
    elif kind == "automation":                                # a bolt
        pdf.polygon([(cx + u * 0.15, cy - u), (cx - u * 0.6, cy + u * 0.1), (cx - u * 0.05, cy + u * 0.1),
                     (cx - u * 0.2, cy + u), (cx + u * 0.6, cy - u * 0.15), (cx + u * 0.05, cy - u * 0.15)],
                    style="F")
    elif kind in ("data", "quality"):                         # a cylinder
        pdf.ellipse(cx - u * 0.7, cy - u * 0.9, u * 1.4, u * 0.5, "D")
        pdf.line(cx - u * 0.7, cy - u * 0.65, cx - u * 0.7, cy + u * 0.65)
        pdf.line(cx + u * 0.7, cy - u * 0.65, cx + u * 0.7, cy + u * 0.65)
        pdf.ellipse(cx - u * 0.7, cy + u * 0.4, u * 1.4, u * 0.5, "D")
    elif kind == "reports":                                   # a bar chart
        for i, frac in enumerate((0.6, 1.0, 1.4)):
            pdf.rect(cx - u * 0.85 + i * u * 0.6, cy + u * 0.8 - u * frac, u * 0.42, u * frac, "F")
    else:                                                     # anything else: a dot
        pdf.circle(cx, cy, u * 0.45, "F")


def mask_email(email: str) -> str:
    """j***@example.com: enough to recognise, never the whole address."""
    local, _, domain = str(email).partition("@")
    return (local[:1] or "*") + "***@" + domain


__all__ = ["DesignImageError", "build_summary_pdf", "build_quote_pdf", "clean", "decode_design_png", "mask_email",
           "quote_number", "PNG_MAX_BYTES", "PNG_MAX_SIDE", "PLAN_TERMS", "PRICED_AFTER_REVIEW", "TO_CONFIRM",
           "NEXT_STEP", "DRAFT_NEXT_STEP", "WATERMARK", "STATUS_READY", "STATUS_DRAFT", "QUOTE_MAX_PAGES",
           "FIGURE_MAX_SIDE"]
