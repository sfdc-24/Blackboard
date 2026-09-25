"""A client's existing page, opened on the canvas to be changed by voice.

FETCH (Gate 1, SSRF). ``fetch_page`` fetches ONLY the exact https URL the
server-owned registry holds for the project (app/clients.py); a request never
supplies a URL. The host name is resolved here, EVERY answer must be a public
unicast address, and the connection goes to that validated address with the
real host name kept for TLS (SNI and certificate check) and the Host header,
so nothing can re-resolve the name between the check and the connect (DNS
rebinding). Redirects are refused outright - the registered URL is the page.
Connect, first-byte, idle and total time are bounded; so are compressed and
decompressed size; only text/html is accepted. No proxy from the environment
is used. Errors never carry the URL, the address or any of the page.

TREE. ``page_to_tree`` turns that HTML into the builder's artifact tree - text
only. Scripts, styles, frames, objects, embeds, SVG, MathML, templates and
every attribute except a few read-only labels (alt, aria-label, placeholder,
the value of a submit button) are dropped; no URL from the page survives, not
even as visible text. What is kept is plain-text labels in the node kinds the
builder edits and the homepage canvas renders as text: screen, section, nav,
heading, text, button, image-placeholder (never fetched), list, form, field.
Input characters, nodes, depth, label length, images and parse time are capped.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
import unicodedata
import zlib
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from .clients import project_url_ok

# httpx logs each request URL at INFO and httpcore the TLS server name at DEBUG.
# A project fetch must not put the client's host or path in any log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CONNECT_SECONDS = 3.0
IDLE_SECONDS = 4.0            # longest wait for the next bytes (httpx read timeout)
FIRST_BYTE_SECONDS = 5.0      # from the request to the response headers
TOTAL_SECONDS = 8.0
MAX_COMPRESSED_BYTES = 1_500_000
MAX_DECOMPRESSED_BYTES = 3_000_000
CONTENT_TYPES = ("text/html",)
CHARSETS = {"utf-8": "utf-8", "utf8": "utf-8", "iso-8859-1": "latin-1", "latin-1": "latin-1",
            "windows-1252": "cp1252", "cp1252": "cp1252", "us-ascii": "ascii", "ascii": "ascii"}

MAX_HTML_CHARS = 2_000_000
FEED_CHUNK = 16_384           # the parser is fed this much at a time ...
MAX_PENDING = 65_536          # ... and may never hold more than this unparsed (one giant tag)
MAX_EVENTS = 50_000           # tags and text runs handled, in all
MAX_CALLBACK_CHARS = 4_096    # the most of one text run or attribute ever looked at
MAX_OUTSTANDING_FETCHES = 4   # page loads (and their threads) alive at once, timed out or not
MAX_NODES = 60
MAX_DEPTH = 4                 # screen > section > list/form > item
MAX_IMAGES = 12
LABEL_MAX = 200
PARSE_SECONDS = 2.0
MAX_OPEN_TAGS = 256
USER_AGENT = "SFDC24-Studio/1.0 (+https://www.sfdc24.com)"


class PageFetchError(RuntimeError):
    """The project page could not be loaded (never carries the page, its URL or address)."""


class FetchBusy(PageFetchError):
    """Every page-load slot is taken: nothing was started."""


# A page load keeps its slot until its thread has actually finished - a caller
# that stopped waiting at the deadline does not free it. So however many
# resolvers or connections hang, at most MAX_OUTSTANDING_FETCHES threads exist.
_SLOTS = threading.BoundedSemaphore(MAX_OUTSTANDING_FETCHES)


# -- which addresses may be reached ---------------------------------------------------
_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.31.196.0/24", "192.52.193.0/24", "192.88.99.0/24",
    "192.168.0.0/16", "192.175.48.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64",
    "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20", "5f00::/16", "fc00::/7", "fe80::/10",
    "fec0::/10", "ff00::/8",
)]


def address_ok(ip) -> bool:
    """A public unicast address: never loopback, private (RFC1918, ULA),
    carrier-grade NAT, link-local, multicast, documentation, benchmarking,
    reserved, unspecified, or an IPv6 form that carries an IPv4 address."""
    if ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo or getattr(ip, "scope_id", None)):
        return False
    if any(ip in net for net in _BLOCKED if net.version == ip.version):
        return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_private or ip.is_loopback or ip.is_link_local
                                       or ip.is_reserved or ip.is_unspecified)


def system_resolver(host: str) -> list:
    return [info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def resolve_pinned(host: str, resolver) -> "ipaddress.IPv4Address | ipaddress.IPv6Address":
    """Resolve once; every answer must pass ``address_ok``; the first is used."""
    try:
        answers = list(resolver(host))
    except (OSError, UnicodeError, ValueError):
        raise PageFetchError("the name did not resolve") from None
    if not answers:
        raise PageFetchError("the name did not resolve")
    chosen = None
    for answer in answers:
        try:
            ip = ipaddress.ip_address(answer)
        except ValueError:
            raise PageFetchError("an address was refused") from None
        if not address_ok(ip):
            raise PageFetchError("an address was refused")
        chosen = chosen or ip
    return chosen


def fetch_page(url: str, *, resolver=None, transport=None, clock=time.monotonic) -> str:
    """The registered page's HTML, or PageFetchError - within TOTAL_SECONDS of
    wall-clock time, whatever the resolver, the connection or the body do: the
    whole fetch runs in a worker the caller stops waiting for at the deadline.
    (That worker is itself bounded by the connect and idle timeouts and the
    per-chunk deadline, and its result is discarded.)"""
    if not project_url_ok(url):
        raise PageFetchError("not a registered https page")
    slots = _SLOTS
    if not slots.acquire(blocking=False):
        raise FetchBusy("every page-load slot is taken")
    outcome: dict = {}

    def work():
        try:
            outcome["html"] = _fetch(url, resolver, transport, clock)
        except PageFetchError as exc:
            outcome["error"] = exc
        except BaseException as exc:         # never the message: it can carry the URL
            outcome["error"] = PageFetchError(type(exc).__name__)
        finally:
            slots.release()                  # only once the work has really stopped

    worker = threading.Thread(target=work, name="studio-project-fetch", daemon=True)
    try:
        worker.start()
    except BaseException:
        slots.release()
        raise
    worker.join(TOTAL_SECONDS)
    if worker.is_alive():
        raise PageFetchError("the page was too slow")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["html"]


def _fetch(url: str, resolver, transport, clock) -> str:
    import httpx
    parts = urlsplit(url)
    host = parts.hostname
    ip = resolve_pinned(host, resolver or system_resolver)
    pinned = urlunsplit(("https", "[%s]" % ip if ip.version == 6 else str(ip), parts.path or "/", "", ""))
    timeout = httpx.Timeout(connect=CONNECT_SECONDS, read=IDLE_SECONDS, write=CONNECT_SECONDS,
                            pool=CONNECT_SECONDS)
    client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)
    started = clock()
    try:
        request = client.build_request(
            "GET", pinned,
            headers={"Host": host, "User-Agent": USER_AGENT, "Accept": "text/html",
                     "Accept-Encoding": "gzip, identity"},
            # TLS is to the registered name, not the address: SNI and the
            # certificate check both use it.
            extensions={"sni_hostname": host})
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            if clock() - started > FIRST_BYTE_SECONDS:
                raise PageFetchError("the page was too slow to answer")
            if 300 <= response.status_code < 400:
                raise PageFetchError("a redirect was refused")
            if response.status_code != 200:
                raise PageFetchError("the page answered %d" % response.status_code)
            media, _, params = (response.headers.get("content-type") or "").partition(";")
            if media.strip().lower() not in CONTENT_TYPES:
                raise PageFetchError("not an html page")
            coding = (response.headers.get("content-encoding") or "").strip().lower()
            if coding not in ("", "identity", "gzip"):
                raise PageFetchError("an unsupported encoding")
            inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if coding == "gzip" else None
            raw_total, body = 0, bytearray()
            for chunk in response.iter_raw():
                raw_total += len(chunk)
                if raw_total > MAX_COMPRESSED_BYTES:
                    raise PageFetchError("the page is too large")
                room = MAX_DECOMPRESSED_BYTES - len(body)
                piece = inflate.decompress(chunk, room + 1) if inflate else chunk
                if len(piece) > room or (inflate and inflate.unconsumed_tail):
                    raise PageFetchError("the page is too large")
                body.extend(piece)
                if clock() - started > TOTAL_SECONDS:
                    raise PageFetchError("the page was too slow")
            if inflate:
                rest = inflate.flush()
                if len(body) + len(rest) > MAX_DECOMPRESSED_BYTES:
                    raise PageFetchError("the page is too large")
                body.extend(rest)
            charset = ""
            for param in params.split(";"):
                key, _, value = param.partition("=")
                if key.strip().lower() == "charset":
                    charset = value.strip().strip('"').lower()
            return bytes(body).decode(CHARSETS.get(charset, "utf-8"), errors="replace")
        finally:
            response.close()
    except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL, zlib.error, UnicodeError, LookupError) as exc:
        raise PageFetchError(type(exc).__name__) from None
    finally:
        client.close()


# -- the page as a tree ---------------------------------------------------------------
_SKIP = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed", "canvas",
         "video", "audio", "picture", "head", "title", "xml", "frameset", "frame", "applet", "portal"}
_SECTIONS = {"header", "footer", "section", "article", "aside", "main"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_TEXT_BLOCKS = {"p", "blockquote", "figcaption", "dt", "dd", "td", "th", "address", "div", "label"}
_VOID = {"img", "input", "br", "hr", "meta", "link", "source", "area", "base", "col", "embed", "param",
         "track", "wbr", "keygen"}
_UNSAFE = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")
# No address from the page reaches a label, even as visible text: links,
# bare domains, IP addresses and email addresses become a neutral placeholder.
LINK = "[link]"
EMAIL = "[email]"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftps?|javascript|vbscript|data|file|blob|wss?|mailto|tel|sms):\S+"
                     r"|(?:^|(?<=\s))//\S+|\bwww\.\S+")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?:/\S*)?")
_IPV6_RE = re.compile(r"(?i)(?<![\w:])\[?([0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7})(?:%\w+)?\]?(?::\d{1,5})?(?![\w:])")
_TLD = (r"(?:com|net|org|info|biz|io|co|ai|app|dev|site|online|shop|store|tech|xyz|me|tv|gov|edu|mil|int"
        r"|cloud|page|link|live|pro|blog|news|agency|design|studio|solutions|services|[a-z]{2})")
_DOMAIN_RE = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+" + _TLD + r"\b(?::\d{1,5})?(?:/\S*)?")


def _ipv6_link(match) -> str:
    text = match.group(1)
    if "::" not in text and not re.search(r"(?i)[a-f]", text):
        return match.group(0)                # a time or a score, not an address
    try:
        ipaddress.IPv6Address(text)
        return LINK
    except ValueError:
        return match.group(0)


def redact(text: str) -> str:
    """Replace every link-, domain-, IP- and email-shaped token with a placeholder."""
    text = _EMAIL_RE.sub(EMAIL, text)
    text = _URL_RE.sub(LINK, text)
    text = _IPV4_RE.sub(LINK, text)
    text = _IPV6_RE.sub(_ipv6_link, text)
    return _DOMAIN_RE.sub(LINK, text)


def clean_text(value: str, cap: int = LABEL_MAX, *, redacted: bool = True) -> str:
    """Visible words as one plain line: no addresses (``redacted``), no
    control, format or separator code points, no angle brackets, single
    spaces, at most ``cap``. Only the first MAX_CALLBACK_CHARS are looked at."""
    value = (value or "")[:MAX_CALLBACK_CHARS]
    kept = []
    for ch in value or "":
        if ch in "<>":
            continue
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch) not in _UNSAFE:
            kept.append(ch)
    text = "".join(kept)
    if redacted:
        text = redact(text)
    text = re.sub(r" +", " ", text).strip()
    if len(text) > cap:
        text = text[:cap].rsplit(" ", 1)[0].rstrip(" ,.;:-") or text[:cap]
    return text


class _ParseStop(Exception):
    """The parse budget is spent: stop now, keep what was built."""


class _Builder(HTMLParser):
    def __init__(self, title: str, clock=time.monotonic):
        super().__init__(convert_charrefs=True)
        self.clock = clock
        self.deadline = clock() + PARSE_SECONDS
        self.events = 0
        self.count = 1                       # the screen
        self.images = 0
        self.full = False
        # The project's name from the registry is not page text: kept as written.
        self.screen = {"id": "screen", "kind": "screen", "label": clean_text(title, 80, redacted=False) or "Project",
                       "children": []}
        self.section = None
        self.nav = None
        self.list = None
        self.form = None
        self.skip, self.skip_tag = 0, ""
        self.stack = []                      # open tags
        self.buffers = []                    # [tag, kind, words] of open text-collecting elements
        self.last_text = None

    def _over_time(self) -> bool:
        """Every callback starts here: past the time or event budget, the whole
        parse stops at once (the exception unwinds out of HTMLParser.feed)."""
        self.events += 1
        if self.events > MAX_EVENTS or self.clock() > self.deadline:
            self.full = True
            raise _ParseStop()
        return self.full

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
        if self._over_time():
            return
        if self.skip:
            if tag == self.skip_tag:
                self.skip += 1
            return
        # Only these attributes are ever read, and only as label text.
        a = {k: (v or "")[:400] for k, v in attrs[:64]
             if k in ("alt", "aria-label", "placeholder", "value", "name", "type", "class", "role",
                      "width", "height")}
        if tag == "select":
            self._add("field", a.get("aria-label") or a.get("name") or "Choice")
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
            if len(self.stack) >= MAX_OPEN_TAGS:
                return                       # nesting this deep adds no structure
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
            if width in ("0", "1") or height in ("0", "1") or self.images >= MAX_IMAGES:
                return                       # a tracking pixel, or enough pictures
            self.images += 1
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
        if self._over_time():
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
        if self._over_time() or self.skip:
            return
        data = data[:MAX_CALLBACK_CHARS]     # never scan more of one text run than this
        if self.buffers:
            words = self.buffers[-1][2]
            if sum(len(w) for w in words) < LABEL_MAX * 4:
                words.append(data[:LABEL_MAX * 4])
        elif data.strip():
            self._add("text", data)

    def finish(self) -> dict:
        while self.stack and not self.full:
            self._close(self.stack.pop())

        # Empty containers say nothing; drop them. Nothing below MAX_DEPTH survives.
        def prune(node, depth):
            kids = [prune(c, depth + 1) for c in node.get("children") or []] if depth < MAX_DEPTH else []
            kids = [c for c in kids if c is not None]
            if "children" in node:
                node["children"] = kids
                if not kids and node["kind"] in ("section", "list", "nav", "form"):
                    return None
            return node
        prune(self.screen, 1)
        return self.screen


def page_to_tree(html: str, title: str, *, clock=time.monotonic) -> dict:
    """The builder's artifact tree for a page: plain text in known kinds only."""
    builder = _Builder(title, clock=clock)
    text = (html or "")[:MAX_HTML_CHARS]
    try:
        # Fed in chunks, with a hard stop between them on time, on the node cap,
        # and on the unparsed remainder: a tag or a script that never closes is
        # held by the parser, and re-scanning it chunk after chunk is the cost
        # this bounds.
        for start in range(0, len(text), FEED_CHUNK):
            builder.feed(text[start:start + FEED_CHUNK])
            if builder.full or clock() > builder.deadline or len(builder.rawdata) > MAX_PENDING:
                raise _ParseStop()
        builder.close()
    except Exception:                        # a stopped or broken page still yields what was read
        pass
    return builder.finish()


def tree_nodes(tree: dict) -> int:
    return 1 + sum(tree_nodes(c) for c in tree.get("children") or [])


def tree_depth(tree: dict) -> int:
    return 1 + max((tree_depth(c) for c in tree.get("children") or []), default=0)


__all__ = ["PageFetchError", "address_ok", "fetch_page", "page_to_tree", "clean_text", "resolve_pinned",
           "system_resolver", "tree_nodes", "tree_depth", "MAX_NODES", "MAX_DEPTH", "MAX_IMAGES", "LABEL_MAX"]
