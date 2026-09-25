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


def build_summary_pdf(state: dict, *, design_png: bytes | None = None) -> bytes:
    """One PDF of the working session. Raises DesignImageError for an unreadable PNG."""
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
        bullets([clean(s, 300) for s in said[-40:]])

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


def mask_email(email: str) -> str:
    """j***@example.com: enough to recognise, never the whole address."""
    local, _, domain = str(email).partition("@")
    return (local[:1] or "*") + "***@" + domain


__all__ = ["DesignImageError", "build_summary_pdf", "clean", "decode_design_png", "mask_email",
           "PNG_MAX_BYTES", "PNG_MAX_SIDE"]
