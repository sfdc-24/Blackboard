"""A client's existing page, opened on the canvas to be changed by voice.

``fetch_page`` fetches ONLY the exact https URL registered for the project
(app/clients.py), follows at most three redirects and only within the same
registrable site, stops at 8 seconds and 1.5 MB, and accepts only HTML.

``page_to_tree`` turns that HTML into the builder's artifact tree - text only.
Scripts, styles, frames, SVG and every URL (links, image sources, script
sources) are dropped; what is kept is visible words, as plain-text labels, in
the node kinds the builder edits and the homepage canvas renders: screen,
section, nav, heading, text, button, image-placeholder, list, form, field.
The tree is capped at MAX_NODES nodes and each label at LABEL_MAX characters.
"""
from __future__ import annotations

import re
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .clients import project_url_ok

FETCH_SECONDS = 8.0
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
MAX_NODES = 60
LABEL_MAX = 200
USER_AGENT = "SFDC24-Studio/1.0 (+https://www.sfdc24.com)"


class PageFetchError(RuntimeError):
    """The project page could not be loaded (never carries the page or its URL)."""


def registrable(host: str) -> str:
    """The site a host belongs to: its last two labels (www.steelworkson.ca -> steelworkson.ca)."""
    labels = (host or "").lower().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else ""


def same_site(url: str, site: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(site) and (host == site or host.endswith("." + site))


def fetch_page(url: str, *, client=None, timeout: float = FETCH_SECONDS, max_bytes: int = MAX_BYTES,
               max_redirects: int = MAX_REDIRECTS, clock=time.monotonic) -> str:
    """The registered page's HTML, or PageFetchError. ``client`` is an httpx.Client
    (tests pass a MockTransport one); redirects are followed here, never by httpx."""
    if not project_url_ok(url):
        raise PageFetchError("not a registered https page")
    import httpx
    site = registrable(urlsplit(url).hostname or "")
    deadline = clock() + timeout
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False,
                                    headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        current = url
        for hop in range(max_redirects + 1):
            try:
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = urljoin(current, response.headers.get("location") or "")
                        if hop >= max_redirects or not project_url_ok(target) or not same_site(target, site):
                            raise PageFetchError("redirect refused")
                        current = target
                        continue
                    if response.status_code != 200:
                        raise PageFetchError("status %d" % response.status_code)
                    if "html" not in (response.headers.get("content-type") or "").lower():
                        raise PageFetchError("not html")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise PageFetchError("page too large")
                        if clock() > deadline:
                            raise PageFetchError("page too slow")
                    return bytes(body).decode(response.encoding or "utf-8", errors="replace")
            except httpx.HTTPError as exc:
                raise PageFetchError(type(exc).__name__) from None
        raise PageFetchError("too many redirects")
    finally:
        if own:
            client.close()


_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "canvas",
         "video", "audio", "head", "title", "math"}
_SECTIONS = {"header", "footer", "section", "article", "aside"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_TEXT_BLOCKS = {"p", "blockquote", "figcaption", "dt", "dd", "td", "th", "address", "div", "label"}
_VOID = {"img", "input", "br", "hr", "meta", "link", "source", "area", "base", "col", "embed", "param",
         "track", "wbr"}
_UNSAFE = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")


def clean_text(value: str, cap: int = LABEL_MAX) -> str:
    """Visible words as one plain line: no control, format or separator code
    points, no angle brackets, single spaces, at most ``cap`` characters."""
    kept = []
    for ch in value or "":
        if ch in "<>":
            continue
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch) not in _UNSAFE:
            kept.append(ch)
    text = re.sub(r" +", " ", "".join(kept)).strip()
    if len(text) > cap:
        text = text[:cap].rsplit(" ", 1)[0].rstrip(" ,.;:-") or text[:cap]
    return text


class _Builder(HTMLParser):
    def __init__(self, title: str):
        super().__init__(convert_charrefs=True)
        self.count = 1                       # the screen
        self.full = False
        self.screen = {"id": "screen", "kind": "screen", "label": clean_text(title, 80) or "Project",
                       "children": []}
        self.section = None
        self.nav = None
        self.list = None
        self.form = None
        self.skip, self.skip_tag = 0, ""
        self.stack = []                      # open tags
        self.buffers = []                    # [tag, attrs, words] of open text-collecting elements
        self.last_text = None

    # -- nodes ---------------------------------------------------------------------
    def _new(self, kind: str, label: str, detail: str = "") -> dict | None:
        if self.count >= MAX_NODES:
            self.full = True
            return None
        self.count += 1
        node = {"id": "pg-%d" % self.count, "kind": kind, "label": label}
        if detail:
            node["detail"] = detail
        return node

    def _section(self, label: str = "Page") -> dict | None:
        if self.section is None:
            self.section = self._new("section", label)
            if self.section is None:
                return None
            self.section["children"] = []
            self.screen["children"].append(self.section)
        return self.section

    def _add(self, kind: str, label: str, detail: str = "") -> None:
        label = clean_text(label)
        if not label or (kind == "text" and len(label) < 2):
            return
        if kind in ("text", "heading") and self.last_text == (kind, label):
            return                           # the same words twice in a row
        if self.nav is not None:
            if kind in ("text", "button"):
                node = self._new("text", label)
                if node:
                    self.nav["children"].append(node)
            return
        if self.form is not None and kind in ("button", "field", "text"):
            node = self._new(kind, label, detail)
            if node:
                self.form["children"].append(node)
            return
        if self.list is not None and kind == "text":
            node = self._new("text", label)
            if node:
                self.list["children"].append(node)
            return
        parent = self._section()
        if parent is None:
            return
        node = self._new(kind, label, detail)
        if node:
            parent["children"].append(node)
            self.last_text = (kind, label)

    def _container(self, kind: str, label: str) -> dict | None:
        if self.nav is not None:
            return None
        parent = self._section()
        if parent is None:
            return None
        node = self._new(kind, clean_text(label) or kind.capitalize())
        if node is None:
            return None
        node["children"] = []
        parent["children"].append(node)
        return node

    # -- parser events ---------------------------------------------------------------
    def _enter_skip(self, tag):
        self.skip_tag, self.skip = tag, 1

    def handle_starttag(self, tag, attrs):
        if self.full:
            return
        if self.skip:
            if tag == self.skip_tag:
                self.skip += 1
            return
        a = {k: (v or "") for k, v in attrs}
        if tag == "select":
            label = a.get("aria-label") or a.get("name") or "Choice"
            self._add("field", label)
            self._enter_skip(tag)            # its options are not page copy
            return
        if tag == "textarea":
            label = a.get("aria-label") or a.get("placeholder") or a.get("name") or "Message"
            self._add("field", label, clean_text(a.get("placeholder", ""), 80))
            self._enter_skip(tag)            # its default text is not page copy
            return
        if tag in _SKIP:
            if tag not in _VOID:
                self._enter_skip(tag)
            return
        if tag not in _VOID:
            self.stack.append(tag)
        if tag == "nav" and self.nav is None:
            node = self._new("nav", clean_text(a.get("aria-label", "")) or "Navigation")
            if node is not None:
                node["children"] = []
                self.screen["children"].append(node)
                self.nav = node
            return
        if tag in _SECTIONS and self.nav is None:
            self.section = None
            self._section(clean_text(a.get("aria-label", "")) or tag.capitalize())
            return
        if tag in ("ul", "ol") and self.list is None and self.form is None:
            self.list = self._container("list", a.get("aria-label", "") or "List")
            return
        if tag == "form" and self.form is None:
            self.form = self._container("form", a.get("aria-label", "") or "Form")
            return
        if tag == "img":
            width, height = a.get("width", ""), a.get("height", "")
            if width in ("0", "1") or height in ("0", "1"):
                return                       # a tracking pixel
            self._flush_open()
            self._add("image-placeholder", a.get("alt", "") or "Image")
            return
        if tag == "input":
            kind = a.get("type", "text").lower()
            if kind in ("submit", "button"):
                self._add("button", a.get("value", "") or "Submit")
            elif kind in ("text", "email", "tel", "search", "url", "number", "date", ""):
                label = a.get("aria-label") or a.get("placeholder") or a.get("name") or "Field"
                self._add("field", label, clean_text(a.get("placeholder", ""), 80))
            return
        if tag == "a" and self.nav is not None:
            self.buffers.append([tag, "text", []])   # a menu link's words, never its address
            return
        buttonish = tag == "button" or (tag == "a" and (a.get("role") == "button"
                                                        or re.search(r"\b(btn|button|cta)\b", a.get("class", ""))))
        if buttonish or tag in _HEADINGS or tag == "li" or tag in _TEXT_BLOCKS:
            if tag in _TEXT_BLOCKS or tag == "li":
                self._flush_open()           # a block inside a block starts a new line
            kind = "button" if buttonish else "heading" if tag in _HEADINGS else "text"
            self.buffers.append([tag, kind, []])
        if tag == "br" and self.buffers:
            self.buffers[-1][2].append(" ")

    def _flush_open(self):
        for entry in self.buffers:
            if entry[2] and entry[1] == "text":
                self._add("text", "".join(entry[2]))
                entry[2] = []

    def handle_endtag(self, tag):
        if self.full:
            return
        if self.skip:
            if tag == self.skip_tag:
                self.skip -= 1
            return
        if tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self._close(open_tag)
            if open_tag == tag:
                break

    def _close(self, tag):
        if self.buffers and self.buffers[-1][0] == tag:
            _, kind, words = self.buffers.pop()
            text = "".join(words)
            if text.strip():
                self._add(kind, text)
        if tag == "nav" and self.nav is not None:
            self.nav = None
        elif tag in ("ul", "ol") and self.list is not None:
            self.list = None
        elif tag == "form":
            self.form = None
        elif tag in _SECTIONS:
            self.section = None

    def handle_data(self, data):
        if self.full or self.skip:
            return
        if self.buffers:
            self.buffers[-1][2].append(data)
        elif data.strip():
            self._add("text", data)

    def finish(self) -> dict:
        while self.stack and not self.full:
            self._close(self.stack.pop())
        # Empty containers say nothing; drop them.
        def prune(node):
            kids = [prune(c) for c in node.get("children") or []]
            kids = [c for c in kids if c is not None]
            if "children" in node:
                node["children"] = kids
                if not kids and node["kind"] in ("section", "list", "nav", "form"):
                    return None
            return node
        prune(self.screen)
        return self.screen


def page_to_tree(html: str, title: str) -> dict:
    """The builder's artifact tree for a page: plain text in known kinds only."""
    builder = _Builder(title)
    try:
        builder.feed(html or "")
        builder.close()
    except Exception:                        # a broken page still yields what was read
        pass
    return builder.finish()


def tree_nodes(tree: dict) -> int:
    return 1 + sum(tree_nodes(c) for c in tree.get("children") or [])


__all__ = ["PageFetchError", "fetch_page", "page_to_tree", "clean_text", "registrable", "same_site",
           "tree_nodes", "MAX_NODES", "LABEL_MAX"]
