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
    """The build plan and quote for a session with a charter."""
    from fpdf import FPDF
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
    from datetime import timedelta
    until = issued + timedelta(days=VALID_DAYS)
    artifact = state.get("artifact") or {}
    table = price_table or {}
    currency = table.get("currency", "")
    line_prices = (table.get("lines") or {}).get(topic or "other") or {}
    support_prices = table.get("support") or {}

    class Quote(FPDF):
        def header(self):
            # the watermark, under the content, on every page
            self.set_font("Helvetica", "B", 66)
            self.set_text_color(*MARK)
            wide = self.get_string_width(WATERMARK)
            with self.rotation(35, self.w / 2, self.h / 2):
                self.text(self.w / 2 - wide / 2, self.h / 2 + 8, WATERMARK)
            if self.page_no() > 1:
                self.set_xy(self.l_margin, 10)
                self.set_font("Helvetica", "B", 8.5)
                self.set_text_color(*NAVY)
                self.cell(40, 5, WATERMARK)
                self.set_font("Helvetica", "", 8.5)
                self.set_text_color(*MUTE)
                self.cell(self.w - self.l_margin - self.r_margin - 40, 5,
                          "Build plan and quote  " + number, align="R")
                self.set_draw_color(*RULE)
                self.set_line_width(0.2)
                self.line(self.l_margin, 16.5, self.w - self.r_margin, 16.5)
                self.set_y(22)
            self.set_text_color(*INK)

        def footer(self):
            self.set_y(-14)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.set_y(-12)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*MUTE)
            half = (self.w - self.l_margin - self.r_margin) / 2
            self.cell(half, 5, "Quote " + number)
            self.cell(half, 5, "Page %d of {nb}" % self.page_no(), align="R")

    pdf = Quote(format="A4")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_title("SFDC24 build plan and quote " + number)
    pdf.set_author("SFDC24")
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def section(text, gap=6, need=22):
        # a heading never sits alone at the foot of a page: it keeps room for its first rows
        if pdf.get_y() + gap + need > pdf.page_break_trigger:
            pdf.add_page()
        pdf.ln(gap)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(*NAVY)
        pdf.cell(width, 7, clean(text, 80), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.3)
        pdf.line(pdf.l_margin, pdf.get_y() + 0.6, pdf.l_margin + width, pdf.get_y() + 0.6)
        pdf.ln(3.2)
        pdf.set_text_color(*INK)

    def pair(label, value, label_w=34, size=10.5):
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*MUTE)
        y = pdf.get_y()
        pdf.cell(label_w, 5.6, clean(label, 40))
        pdf.set_font("Helvetica", "", size)
        pdf.set_text_color(*INK)
        pdf.set_xy(pdf.l_margin + label_w, y)
        pdf.multi_cell(width - label_w, 5.6, clean(value, 400), new_x="LMARGIN", new_y="NEXT")

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

    # --- header ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*NAVY)
    pdf.set_char_spacing(0.6)
    pdf.cell(width, 7, WATERMARK, new_x="LMARGIN", new_y="NEXT")
    pdf.set_char_spacing(0)
    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 24)
    pdf.cell(width, 11, "Build plan and quote", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pair("Quote", number)
    pair("Date", issued.strftime("%B %d, %Y"))
    pair("Validity", "Valid for %d days, until %s" % (VALID_DAYS, until.strftime("%B %d, %Y")))
    if prepared_for:
        pair("Prepared for", prepared_for)

    # --- the project and its scope of work ---
    section("Project")
    pair("Goal", _goal(state))
    pair("Session type", session_type(topic))

    section("Scope of work")
    frame = charter_frame(topic)
    labels = {d[0]: d[1] for d in frame}
    dims = {}
    for d in charter["dimensions"]:
        if isinstance(d, dict) and d.get("id") in labels:
            level = d.get("level") if type(d.get("level")) is int and d.get("level") in LEVEL_WORDS else 0
            dims[d["id"]] = {"level": level, "captured": clean(d.get("captured"), 140)}
    scope_w = (44, width - 44 - 38, 38)
    for did, label, covers in frame:
        entry = dims.get(did, {"level": 0, "captured": ""})
        if entry["level"] >= 2 and entry["captured"]:
            body = entry["captured"]
        elif entry["captured"]:
            body = "Noted so far: " + entry["captured"]
        else:
            body = covers[:1].upper() + covers[1:]
        grid((clean(label, 40), body, LEVEL_WORDS[entry["level"]]), scope_w,
             (("B", 10), ("", 10), ("", 8.5)), aligns=("L", "L", "R"),
             colors=(INK, INK if entry["level"] >= 1 else MUTE, MUTE), pad=1.8, line_h=4.8)
    if charter.get("next"):
        pdf.set_font("Helvetica", "I", 9.5)
        pdf.set_text_color(*MUTE)
        pdf.multi_cell(width, 5, "Next to settle: " + clean(charter.get("next"), 160), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)

    # --- line items ---
    section("Line items", need=34)
    widths = (48, width - 48 - 44, 44)

    def table_head():
        grid(("Item", "Description", "Price"), widths, (("B", 9), ("B", 9), ("B", 9)),
             aligns=("L", "L", "R"), colors=(MUTE, MUTE, MUTE), fill=FILL, rule=NAVY, rule_w=0.35)

    def row(cells, *, bold=False, rule=RULE, rule_w=0.2):
        grid(cells, widths, (("B", 10), ("B" if bold else "", 10), ("B" if bold else "", 10)),
             aligns=("L", "L", "R"), rule=rule, rule_w=rule_w, repeat=table_head)

    table_head()
    lines = quote_lines(topic)
    total, priced = 0.0, True
    for line in lines:
        amount = line_prices.get(line[0])
        if amount is None:
            priced = False
        else:
            total += amount
        row((line[1], _line_description(line, dims, artifact),
             money(amount, currency) if amount is not None else PRICED_AFTER_REVIEW))
    total = round(total, 2)
    if priced and lines:
        row(("Total", "", money(total, currency)), bold=True, rule=NAVY, rule_w=0.35)

    # --- payment terms ---
    section("Payment terms", need=24)
    half = round(total / 2, 2)
    for i, term in enumerate(PLAN_TERMS[:2]):
        amount = (money(half if i == 0 else round(total - half, 2), currency) if priced and lines else "")
        grid((term, amount), (width - 44, 44), (("", 10.5), ("", 10.5)), aligns=("L", "R"),
             rule=None, pad=1.2, line_h=5.4)

    # --- support plan ---
    section("Support plan options", need=36)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(width, 5.4, PLAN_TERMS[2], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)
    for key, name, what, per in SUPPORT_OPTIONS:
        amount = support_prices.get(key)
        row((name, what, (money(amount, currency) + " " + per) if amount is not None else PRICED_AFTER_REVIEW))

    # --- next step ---
    section("Next step", need=14)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*NAVY)
    pdf.multi_cell(width, 6, NEXT_STEP, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)

    # --- appendix: session notes ---
    pdf.add_page()
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
    if design_png is not None:
        section("Design snapshot", gap=4)
        try:
            pdf.image(io.BytesIO(design_png), w=min(width, 140))
        except Exception as exc:  # a PNG that passed the header checks but will not decode
            raise DesignImageError("design_png is not a readable PNG") from exc
    else:
        outline = _outline(artifact)[:30]
        if outline:
            section("The canvas", gap=4)
            pdf.set_font("Courier", "", 8.5)
            for line in outline:
                pdf.multi_cell(width, 4.4, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def mask_email(email: str) -> str:
    """j***@example.com: enough to recognise, never the whole address."""
    local, _, domain = str(email).partition("@")
    return (local[:1] or "*") + "***@" + domain


__all__ = ["DesignImageError", "build_summary_pdf", "build_quote_pdf", "clean", "decode_design_png", "mask_email",
           "quote_number", "PNG_MAX_BYTES", "PNG_MAX_SIDE", "PLAN_TERMS", "PRICED_AFTER_REVIEW", "TO_CONFIRM",
           "NEXT_STEP", "WATERMARK"]
