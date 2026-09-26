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

import errno
import ipaddress
import json
import logging
import re
import select
import socket
import subprocess
import sys
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
RESOLVE_SECONDS = 3.0         # the name lookup, in a child process killed at this deadline
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
MAX_RUN_CHARS = 2_000         # one unbroken visible run held across tags; longer is dropped whole
USER_AGENT = "SFDC24-Studio/1.0 (+https://www.sfdc24.com)"


class PageFetchError(RuntimeError):
    """The project page could not be loaded (never carries the page, its URL or address)."""


class FetchBusy(PageFetchError):
    """Every page-load slot is taken: nothing was started."""


class FetchTimeout(PageFetchError):
    """The caller stopped waiting; the load's thread may still be running.
    ``done`` is set once that thread has really stopped, so a caller can keep
    the load's durable leases alive until then (Codex Gate 1 on 59ed871).
    ``cancel`` stops it: set, the load makes no further step - the name lookup
    child is killed, no connection is opened, no more bytes are read - so a
    caller that can no longer hold its leases stops the work before they can
    lapse (Codex Gate 1 on 80dfc8f)."""

    def __init__(self, message: str, done: "threading.Event | None" = None,
                 cancel: "threading.Event | None" = None):
        super().__init__(message)
        self.done = done
        self.cancel = cancel


class FetchCancelled(PageFetchError):
    """The load was stopped on purpose (its leases could not be kept)."""


class WorkDeadline:
    """When a load's work must have stopped: the end of its last CONFIRMED
    durable lease, less a margin longer than any one blocking step (Codex Gate
    1 on ecee267). The keeper moves it only after a renewal is written; the
    work reads it itself before every step - the lookup, the request, every
    chunk - on the same clock the leases use. So a keeper that is paused, or
    starved, cannot keep the work alive past its lease: the work stops on its
    own, before any other instance can take the place."""

    def __init__(self, clock, until: float):
        self.clock = clock
        self._until = float(until)
        self._lock = threading.Lock()

    def extend(self, until: float) -> None:
        with self._lock:
            self._until = max(self._until, float(until))

    def passed(self) -> bool:
        with self._lock:
            until = self._until
        return float(self.clock()) >= until


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


# The system lookup can block without bound and a thread cannot be stopped,
# so it runs in a child process that is killed at RESOLVE_SECONDS or the
# moment the load is cancelled: no lookup outlives its load's leases (Codex
# Gate 1 on 80dfc8f). The host is argv, never a shell; -I -S keep the child
# free of the environment and site packages; it prints one JSON list.
_RESOLVE_CHILD = ("import json, socket, sys\n"
                  "print(json.dumps([i[4][0] for i in socket.getaddrinfo(sys.argv[1], 443, "
                  "type=socket.SOCK_STREAM)]))")
_RESOLVE_OUTPUT = 65536


def system_resolver(host: str, cancel: "threading.Event | None" = None, seconds: float | None = None,
                    deadline: "WorkDeadline | None" = None) -> list:
    limit = RESOLVE_SECONDS if seconds is None else float(seconds)
    # A load already stopped launches no lookup child at all (Codex Gate 1 B3
    # on cfdebac); one stopped after the launch is killed within a poll.
    _stopped(cancel, deadline)
    child = subprocess.Popen([sys.executable, "-I", "-S", "-c", _RESOLVE_CHILD, str(host)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    limit_at = time.monotonic() + limit
    try:
        while child.poll() is None:
            _stopped(cancel, deadline)
            if time.monotonic() >= limit_at:
                raise OSError("the name did not resolve in time")
            time.sleep(0.02)
        out = child.stdout.read(_RESOLVE_OUTPUT + 1)
        if child.returncode != 0 or len(out) > _RESOLVE_OUTPUT:
            raise OSError("the name did not resolve")
        answers = json.loads(out.decode("utf-8"))
        if not isinstance(answers, list) or not all(isinstance(a, str) for a in answers):
            raise ValueError("not a list of addresses")
        return answers
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()


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


def fetch_page(url: str, *, resolver=None, transport=None, clock=time.monotonic,
               deadline: "WorkDeadline | None" = None) -> str:
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
    finished, cancel = threading.Event(), threading.Event()

    def work():
        try:
            outcome["html"] = _fetch(url, resolver, transport, clock, cancel, deadline)
        except PageFetchError as exc:
            outcome["error"] = exc
        except BaseException as exc:         # never the message: it can carry the URL
            outcome["error"] = PageFetchError(type(exc).__name__)
        finally:
            slots.release()                  # only once the work has really stopped
            finished.set()

    worker = threading.Thread(target=work, name="studio-project-fetch", daemon=True)
    try:
        worker.start()
    except BaseException:
        slots.release()
        raise
    worker.join(TOTAL_SECONDS)
    if worker.is_alive():
        raise FetchTimeout("the page was too slow", finished, cancel)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["html"]


def _stopped(cancel, deadline=None) -> None:
    if cancel is not None and cancel.is_set():
        raise FetchCancelled("the load was stopped")
    if deadline is not None and deadline.passed():
        raise FetchCancelled("the load's lease ran out")


POLL_SECONDS = 0.02            # how soon a stopped load's connect, read or live socket ends
_CONNECTING = {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
               getattr(errno, "WSAEWOULDBLOCK", 10035), getattr(errno, "WSAEINPROGRESS", 10036),
               getattr(errno, "WSAEALREADY", 10037), getattr(errno, "WSAEINVAL", 10022)}


class _Interrupter:
    """Closes a load's live sockets the moment it is stopped (Codex Gate 1 B3
    on cfdebac): an operation that began just before the stop - a connect, a
    TLS handshake, a write or a read - fails at once instead of running on."""

    def __init__(self, halted):
        self.halted = halted                 # () -> bool: cancelled, or the deadline passed
        self.sockets: list = []
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.fired = False
        self.thread = threading.Thread(target=self._watch, name="studio-fetch-interrupt", daemon=True)
        self.thread.start()

    def register(self, sock) -> None:
        with self.lock:
            if self.fired:
                _close_socket(sock)
                return
            self.sockets.append(sock)

    def _watch(self) -> None:
        while not self.done.wait(POLL_SECONDS):
            if self.halted():
                with self.lock:
                    self.fired = True
                    for sock in self.sockets:
                        _close_socket(sock)
                return

    def stop(self) -> None:
        self.done.set()


def _close_socket(sock) -> None:
    import socket as _socket
    try:
        sock.shutdown(_socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _gated_backend(stop, interrupter):
    """An httpcore network backend whose every step checks ``stop`` first and
    whose sockets the interrupter can close mid-operation."""
    import socket as _socket
    import httpcore
    from httpcore._backends.sync import SyncStream

    class GatedStream(httpcore.NetworkStream):
        def __init__(self, inner):
            self.inner = inner
            sock = inner.get_extra_info("socket")
            if sock is not None:
                interrupter.register(sock)

        def read(self, max_bytes, timeout=None):
            # In short slices, checking between them: a stopped load's read
            # ends within one slice, and bytes that arrive after a stop are
            # never used.
            limit = None if timeout is None else time.monotonic() + float(timeout)
            while True:
                stop()
                left = POLL_SECONDS if limit is None else min(POLL_SECONDS, limit - time.monotonic())
                if left <= 0:
                    raise httpcore.ReadTimeout("no bytes in time")
                try:
                    data = self.inner.read(max_bytes, left)
                except httpcore.ReadTimeout:
                    continue
                except BaseException:
                    stop()                    # the interrupter closed it: report the stop, not the error
                    raise
                stop()
                return data

        def write(self, buffer, timeout=None):
            stop()
            try:
                self.inner.write(buffer, timeout)
            except BaseException:
                stop()
                raise

        def close(self):
            self.inner.close()

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            stop()
            try:
                wrapped = GatedStream(self.inner.start_tls(ssl_context, server_hostname, timeout))
            except BaseException:
                stop()
                raise
            stop()
            return wrapped

        def get_extra_info(self, info):
            return self.inner.get_extra_info(info)

    class GatedBackend(httpcore.NetworkBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            stop()
            family = _socket.AF_INET6 if ":" in str(host) else _socket.AF_INET
            sock = _socket.socket(family, _socket.SOCK_STREAM)
            interrupter.register(sock)        # from here the interrupter can close it
            try:
                if local_address is not None:
                    sock.bind((local_address, 0))
                # A non-blocking connect, polled: a stop ends it within one poll.
                sock.setblocking(False)
                limit = time.monotonic() + (CONNECT_SECONDS if timeout is None else float(timeout))
                stop()
                code = sock.connect_ex((host, port))
                while True:
                    if code == 0:
                        break
                    if code not in _CONNECTING:
                        raise httpcore.ConnectError("the connection was refused")
                    stop()
                    if time.monotonic() >= limit:
                        raise httpcore.ConnectTimeout("the connection took too long")
                    _, writable, failed = select.select([], [sock], [sock], POLL_SECONDS)
                    if writable or failed:
                        code = sock.getsockopt(_socket.SOL_SOCKET, _socket.SO_ERROR)
                        if code == 0:
                            break
                        raise httpcore.ConnectError("the connection failed")
                stop()
                sock.setblocking(True)
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except BaseException:
                _close_socket(sock)
                stop()                        # a connect cut off by the stop reports the stop
                raise
            return GatedStream(SyncStream(sock))

        def sleep(self, seconds):
            time.sleep(seconds)

    GatedBackend.stream_class = GatedStream
    return GatedBackend()


def _real_transport(stop, interrupter):
    """httpx's transport with our gated backend under its connection pool."""
    import httpcore
    import httpx

    transport = httpx.HTTPTransport(trust_env=False)
    pool = transport._pool
    transport._pool = httpcore.ConnectionPool(
        ssl_context=pool._ssl_context, max_connections=1, max_keepalive_connections=0,
        http1=True, http2=False, retries=0, network_backend=_gated_backend(stop, interrupter))
    return transport


def _gated(transport, stop):
    """The transport behind a gate checked inside it, immediately before a
    request is connected or written (Codex Gate 1 on ecee267): a cancel or a
    lapsed deadline that lands after the caller's last check and before the
    send still wins - nothing reaches the network."""
    import httpx

    class Gated(httpx.BaseTransport):
        def __init__(self, inner):
            self.inner = inner

        def handle_request(self, request):
            stop()
            return self.inner.handle_request(request)

        def close(self):
            self.inner.close()
    return Gated(transport if transport is not None else httpx.HTTPTransport(trust_env=False))


def _halted(cancel, deadline) -> bool:
    return bool((cancel is not None and cancel.is_set()) or (deadline is not None and deadline.passed()))


def _fetch(url: str, resolver, transport, clock, cancel=None, deadline=None) -> str:
    import httpx
    parts = urlsplit(url)
    host = parts.hostname
    _stopped(cancel, deadline)
    ip = resolve_pinned(host, resolver or (lambda name: system_resolver(name, cancel, deadline=deadline)))
    _stopped(cancel, deadline)               # every step checks: a stopped load goes no further
    pinned = urlunsplit(("https", "[%s]" % ip if ip.version == 6 else str(ip), parts.path or "/", "", ""))
    timeout = httpx.Timeout(connect=CONNECT_SECONDS, read=IDLE_SECONDS, write=CONNECT_SECONDS,
                            pool=CONNECT_SECONDS)
    stop = lambda: _stopped(cancel, deadline)  # noqa: E731
    interrupter = None
    if transport is None:
        # The real network: every socket step gated, live sockets closed on a stop.
        interrupter = _Interrupter(lambda: _halted(cancel, deadline))
        transport = _real_transport(stop, interrupter)
    client = httpx.Client(transport=_gated(transport, stop), timeout=timeout,
                          follow_redirects=False, trust_env=False)
    started = clock()
    try:
        request = client.build_request(
            "GET", pinned,
            headers={"Host": host, "User-Agent": USER_AGENT, "Accept": "text/html",
                     "Accept-Encoding": "gzip, identity"},
            # TLS is to the registered name, not the address: SNI and the
            # certificate check both use it.
            extensions={"sni_hostname": host})
        _stopped(cancel, deadline)
        response = client.send(request, stream=True, follow_redirects=False)
        try:
            _stopped(cancel, deadline)
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
                _stopped(cancel, deadline)
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
    except FetchCancelled:
        raise
    except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL, zlib.error, UnicodeError, LookupError) as exc:
        if _halted(cancel, deadline):
            raise FetchCancelled("the load was stopped") from None       # the interrupter closed it
        raise PageFetchError(type(exc).__name__) from None
    finally:
        client.close()
        if interrupter is not None:
            interrupter.stop()


# -- the page as a tree ---------------------------------------------------------------
_SKIP = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed", "canvas",
         "video", "audio", "picture", "head", "title", "xml", "frameset", "frame", "applet", "portal"}
_SECTIONS = {"header", "footer", "section", "article", "aside", "main"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_TEXT_BLOCKS = {"p", "blockquote", "figcaption", "dt", "dd", "td", "th", "address", "div", "label"}
_VOID = {"img", "input", "br", "hr", "meta", "link", "source", "area", "base", "col", "embed", "param",
         "track", "wbr", "keygen"}
_UNSAFE = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")
# Inline tags do not end a run of words: "alice<b>@example.com</b>" reads as
# one address, as a browser shows it (Codex Gate 1 on ecee267: a split run
# left part of an address behind).
_INLINE = {"b", "strong", "i", "em", "span", "small", "u", "s", "sub", "sup", "code", "kbd", "mark", "abbr",
           "cite", "q", "time", "font", "var", "samp", "dfn", "bdi", "bdo", "data", "wbr", "del", "ins", "tt"}
# No address from the page reaches a label, even as visible text: links,
# bare domains, IP addresses and email addresses become a neutral placeholder.
# Conservative on purpose (Codex Gate 1 NO-GO on a2d98fc; Cursor NO-GO on
# dfbcc11): ANY plausible host is an address. A host is found inside any run of
# word characters, hyphens and dots (ASCII or IDNA full-width), wherever the
# run starts - after "@", "-", ".", "_" or a full-width dot too - and whatever
# its label lengths: the run is an address when some dot has anything before
# it and a TLD-shaped label after it (2-63 letters of any script, or xn--...).
# The whole run goes, with its :port and /?# tail. Combining marks belong to
# the run and to the TLD (Codex, dfbcc11), so Indic hosts (उदाहरण.भारत) and
# decomposed (NFD) ones are taken whole. Any local@host in any script is an
# address too; IPv4 counts with full-width dots; every eight-group IPv6
# candidate is decided by parsing it. Words like "e.g.",
# "i.e." and "Inc." stay; a product name written like a host ("Node.js") is
# redacted too, which is the price of never leaking an address. Every scan is
# linear in the text.
# Every one of them reads the compatibility view (_compat_view, below).
LINK = "[link]"
EMAIL = "[email]"
_DOT = "[.\u3002\uff0e\uff61]"
_URL_RE = re.compile(r"(?i)\b(?:https?|ftps?|javascript|vbscript|data|file|blob|wss?|mailto|tel|sms):\S+"
                     r"|(?:^|(?<=\s))//\S+|\bwww\.\S+")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:" + _DOT + r"\d{1,3}){3}(?::\d{1,5})?(?:/\S*)?")
_IPV6_RE = re.compile(r"(?i)(?<![\w:])\[?([0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7})(?:%\w+)?\]?(?::\d{1,5})?(?![\w:])")
_DOT_CHARS = ".\u3002\uff0e\uff61"
_HOST_SPLIT_RE = re.compile(_DOT)
_HOST_TAIL_RE = re.compile(r"(?::\d{1,5})?(?:[/?#]\S*)?")


def _host_char(ch: str) -> bool:
    """What a host run holds: letters and digits of any script, combining
    marks, "-", "_" and the dots."""
    return ch.isalnum() or ch in "-_" or ch in _DOT_CHARS or unicodedata.category(ch)[0] == "M"


def _tld_like(label: str) -> bool:
    """2-63 letters of any script, or a punycode label - measured on the base
    letters only: combining marks anywhere (first included) and "-" or "_" at
    either edge never hide a TLD, and marks never push it past 63 (Cursor
    NO-GO on cc56fea)."""
    base = "".join(c for c in label if unicodedata.category(c)[0] != "M").strip("-_")
    if base[:4].lower() == "xn--":
        return 5 <= len(base) <= 63 and all(c.isascii() and (c.isalnum() or c == "-") for c in base)
    return 2 <= len(base) <= 63 and all(c.isalpha() for c in base)


def _hostlike(run: str) -> bool:
    """A dot with anything before it and a TLD-shaped label after it."""
    parts = _HOST_SPLIT_RE.split(run)
    return any(parts[i - 1] and _tld_like(parts[i]) for i in range(1, len(parts)))


def _host_spans(text: str):
    """One pass over the text: each maximal run of host characters is looked
    at once, so the scan is linear however the text is shaped."""
    i, n = 0, len(text)
    while i < n:
        if not _host_char(text[i]):
            i += 1
            continue
        j = i
        while j < n and _host_char(text[j]):
            j += 1
        host = text[i:j].rstrip(_DOT_CHARS)          # a sentence's full stop is not the host's
        if _hostlike(host):
            end = _HOST_TAIL_RE.match(text, i + len(host)).end()
            yield i, end, LINK
            i = end
        else:
            i = j


def _ipv6_link(match) -> str:
    text = match.group(1)
    if "::" not in text and not re.search(r"(?i)[a-f]", text) and text.count(":") != 7:
        return match.group(0)                # a time or a score, not an address; eight groups are always parsed
    try:
        ipaddress.IPv6Address(text)
        return LINK
    except ValueError:
        return match.group(0)


def _ipv6_found(view: str):
    for match in _IPV6_RE.finditer(view):
        if _ipv6_link(match) == LINK:
            yield match.start(), match.end(), LINK


def _regex_found(pattern, placeholder: str):
    return lambda view: ((m.start(), m.end(), placeholder) for m in pattern.finditer(view))


# Every detection reads a compatibility view of the text (Codex Gate 1 NO-GO on
# 5c2957d and 38bc713). Each code point becomes its NFKC mapping, the way UTS46
# maps a host name, so circled, full-width, squared, mathematical and other
# compatibility letters, digits, dots, colons and "@" read as what they stand
# for (secret.ⓒⓞⓜ is secret.com). One code point maps to at most 18, so the
# view is bounded and stays linear in the text, and only code points outside
# ASCII are normalized, one at a time - never the payload as a whole. An offset
# map leads every match back to the original, and the COMPLETE original
# characters it covers are replaced; everything else keeps its own characters.
def _compat_view(text: str) -> tuple[str, list | None]:
    """(the view, the original index of each view character), or (text, None)
    when nothing maps to anything else."""
    if text.isascii():
        return text, None
    chunks, origin, changed = [], [], False
    for i, ch in enumerate(text):
        mapped = unicodedata.normalize("NFKC", ch) if ord(ch) > 0x7F else ch
        if len(mapped) > 1 and all(c in _DOT_CHARS for c in mapped):
            # A repeated-dot form UTS46 disallows (two-dot leader, ellipsis,
            # their vertical forms) reads as one dot, conservatively: secret‥com
            # is taken for secret.com (Codex, 21:19Z on 132a544).
            mapped = "."
        if not mapped:
            mapped = ch
        changed = changed or mapped != ch
        chunks.append(mapped)
        origin.extend([i] * len(mapped))
    if not changed:
        return text, None
    return "".join(chunks), origin


def _replace_found(text: str, finder) -> str:
    """One detection pass on the compatibility view; each match replaces the
    complete original characters it covers."""
    view, origin = _compat_view(text)
    out, last = [], 0
    for start, end, placeholder in finder(view):
        if origin is not None:
            start, end = origin[start], origin[end - 1] + 1
        if start < last:                     # inside the last replacement's characters: widen it
            last = max(last, end)
            continue
        out.append(text[last:start])
        out.append(placeholder)
        last = end
    out.append(text[last:])
    return "".join(out)


# Mailboxes (Codex Gate 1 NO-GO on 59ed871). The registry accepts a mailbox
# as 1-64 code points that are neither whitespace nor "@", an "@", and 1-255
# more (app/clients.py) - counted in ORIGINAL code points, where one ligature
# is one code point, not the three letters of its compatibility view. So the
# scan reads the original text, in the registry's own grammar: a mailbox is the
# whole run of non-whitespace characters around an "@" (or its full-width and
# small forms), with something before it and something after it. The whole
# run goes, whatever its length - local part and domain together, bracketed
# literal or not - so no window can leave a registry-valid prefix or suffix
# behind: not U+FB03 x30 before the "@", not a literal of 51 squared units,
# not "secret" + a full-width "<" or ":". That is over-redaction by design.
# Only trailing sentence punctuation (.,;:!?) stays outside it - unless the
# part after the "@" is nothing but that punctuation ("zqclient@?!", "a@."),
# which the registry accepts as a mailbox too: then the whole run goes,
# punctuation and all (Codex Gate 1 on 80dfc8f). One pass, one
# character test per character (_email_class, counted by the linear-work
# test): linear by construction.
_AT_FORMS = frozenset("@\ufe6b\uff20")     # every code point whose NFKC holds "@" (checked by a test)
_TRAIL = frozenset(".,;:!?")
_GAP, _AT, _TRAILING, _BODY = 0, 1, 2, 3


def _email_class(ch: str) -> int:
    if ch.isspace():
        return _GAP
    if ch in _AT_FORMS:
        return _AT
    return _TRAILING if ch in _TRAIL else _BODY


def _email_spans(text: str):
    """(start, end, EMAIL) for each run of non-whitespace characters that
    holds an "@" with something before and after it, in original coordinates;
    trailing sentence punctuation is left outside the span, except when it is
    all that follows the "@" - then the whole run goes."""
    i, n = 0, len(text)
    while i < n:
        kind = _email_class(text[i])
        if kind == _GAP:
            i += 1
            continue
        start, at, core = i, -1, (i if kind != _TRAILING else -1)
        i += 1
        while i < n:
            kind = _email_class(text[i])
            if kind == _GAP:
                break
            if kind == _AT and at < 0:
                at = i
            if kind != _TRAILING:
                core = i
            i += 1
        if at > start and i > at + 1:                 # the registry's grammar: 1+ before, 1+ after
            yield start, (core + 1 if core > at else i), EMAIL


def _replace_raw(text: str, finder) -> str:
    """Spans found on the original text itself, each replaced whole."""
    out, last = [], 0
    for start, end, placeholder in finder(text):
        out.append(text[last:start])
        out.append(placeholder)
        last = end
    out.append(text[last:])
    return "".join(out)


def redact(text: str) -> str:
    """Replace every link-, domain-, IP- and email-shaped token with a
    placeholder: links, IPs and domains found on the compatibility view
    (above), mailboxes on the original text in the registry's grammar."""
    text = _replace_found(text, _regex_found(_URL_RE, LINK))      # first: a link's user@host goes with it
    text = _replace_raw(text, _email_spans)                        # the registry's grammar, original code points
    text = _replace_found(text, _regex_found(_IPV4_RE, LINK))
    text = _replace_found(text, _ipv6_found)
    return _replace_found(text, _host_spans)


def _cut(text: str, n: int) -> tuple[str, bool]:
    """At most n characters, and whether anything was cut. A token the cut
    goes through is dropped WHOLE (Codex Gate 1 on ecee267): a cap must never
    leave part of an address - a local part, an "@", a piece of a domain."""
    if len(text) <= n:
        return text, False
    head = text[:n]
    if not text[n].isspace():
        i = len(head)
        while i > 0 and not head[i - 1].isspace():
            i -= 1
        head = head[:i]
    return head, True


def _drop_tail(text: str) -> str:
    """Without its last token, unless it already ends between words: for text
    whose end was cut by something that could not see the rest."""
    i = len(text)
    while i > 0 and not text[i - 1].isspace():
        i -= 1
    return text[:i]


def clean_text(value: str, cap: int = LABEL_MAX, *, redacted: bool = True) -> str:
    """Visible words as one plain line: no addresses (``redacted``), no
    control, format or separator code points, no angle brackets, single
    spaces, at most ``cap``. Only the first MAX_CALLBACK_CHARS are looked at,
    and a token that limit cuts is dropped whole. Addresses are found on the
    ORIGINAL code points before anything is stripped (Codex Gate 1 on
    ecee267: "alice@<" lost its "<" and kept "alice@"), and again after, so
    stripping can neither hide an address nor assemble one."""
    value, _ = _cut(value or "", MAX_CALLBACK_CHARS)
    if redacted:
        value = redact(value)
    text = _strip_unsafe(value)
    if redacted:
        text = redact(text)
    text = re.sub(r" +", " ", text).strip()
    if len(text) > cap:
        text = text[:cap].rsplit(" ", 1)[0].rstrip(" ,.;:-") or text[:cap]
    return text


def _strip_unsafe(value: str) -> str:
    """No angle brackets, no control, format or separator code points, and
    every whitespace a plain space."""
    kept = []
    for ch in value or "":
        if ch in "<>":
            continue
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch) not in _UNSAFE:
            kept.append(ch)
    return "".join(kept)


def _spread(pieces: list, whole: str) -> list:
    """``whole`` is the pieces joined, with one span rewritten. Only that span
    moves: the characters before and after it stay in their own pieces, and
    its new text goes to the piece where the change begins. Joined, the
    result is exactly ``whole``."""
    joined = "".join(pieces)
    if whole == joined:
        return list(pieces)
    n = min(len(joined), len(whole))
    head = 0
    while head < n and joined[head] == whole[head]:
        head += 1
    tail = 0
    while tail < n - head and joined[-1 - tail] == whole[-1 - tail]:
        tail += 1
    end, middle = len(joined) - tail, whole[head:len(whole) - tail]
    owner = min(head, len(joined) - 1)
    out, at = [], 0
    for text in pieces:
        start, at = at, at + len(text)
        out.append(text[:max(0, head - start)] + (middle if start <= owner < at else "")
                   + text[max(0, end - start):])
    return out


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
        self.buffers = []                    # [tag, kind, words, cut] of open text-collecting elements
        self.last_text = None
        # One text run can reach handle_data in pieces (a feed chunk boundary, a
        # comment in the middle): the pieces are joined, and read as one run
        # at the next tag or at the end (Codex Gate 1 on ecee267).
        self.pending, self.pending_len = [], 0
        self.stopped = False                 # the parse ended early: the last run may be cut
        # ONE UNRESOLVED VISIBLE RUN (Codex Gate 1 B1 on cfdebac). A word that
        # meets a tag with no whitespace between may continue on the far side
        # of it - any tag: ordinary, skipped or custom - so zqalice<a>@x</a>yz
        # reads as one word. Its pieces are held here, with where each would
        # have gone, and nothing the page says reaches a node until the word
        # ends: then the whole word is redacted as one. While a word is held,
        # every other change to the tree waits in ``queue`` too, so the page
        # keeps its order. A parse that stops while a word is held drops it,
        # and so does a word longer than MAX_RUN_CHARS or one running into
        # text past MAX_CALLBACK_CHARS: its later pieces go too (``dropping``)
        # until whitespace or a line break ends it. Nothing held or dropped
        # was ever materialised.
        self.run = []                        # [(text, the entry it goes to)]
        self.run_len = 0
        self.queue = []                      # (function, args): tree changes waiting behind the held word
        self.placed = []                     # what each held piece became, while the queue drains
        self.run_done = []
        self.dropping = False
        self.unplaced = []                   # sections made, joining the screen with their first child

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

    def _section(self, label: str = "Page") -> dict:
        if self.section is None:
            self.section = {"kind": "section", "label": label, "children": []}
            self.unplaced.append(self.section)
        return self.section

    def _join(self, node) -> bool:
        """A section joins the screen once - at its first child, or where the
        page opens its own <section> - and only then is it counted and named,
        as it was made at that moment before B1. False: the node cap is full."""
        for i, pending in enumerate(self.unplaced):
            if pending is node:
                made = self._new("section", node["label"])
                if made is None:
                    return False
                del self.unplaced[i]
                kids = node["children"]
                node.clear()
                node.update(made)
                node["children"] = kids
                self.screen["children"].append(node)
                return True
        return True

    def _append(self, parent, node) -> None:
        if self._join(parent):
            parent["children"].append(node)

    def _slot(self, kind: str):
        """Where a node of ``kind`` goes, decided at the moment the page put it there."""
        if self.nav is not None:
            return ("nav", self.nav) if kind in ("text", "button") else None
        if self.form is not None and kind in ("button", "field", "text"):
            return ("form", self.form)
        if self.list is not None and kind == "text":
            return ("list", self.list)
        return ("section", self._section())

    def _later(self, fn, *args) -> None:
        """A change to the tree: now, or - behind a held word - in page order
        once the word is resolved."""
        if self.run:
            self.queue.append((fn, args))
        else:
            fn(*args)

    def _add(self, kind: str, label: str, detail: str = "") -> None:
        self._later(self._emit, kind, label, detail, self._slot(kind))

    def _emit(self, kind: str, label: str, detail: str, slot) -> None:
        label = clean_text(label)
        if not label or (kind == "text" and len(label) < 2):
            return
        if kind in ("text", "heading") and self.last_text == (kind, label):
            return                           # the same words twice in a row
        if slot is None:
            return
        where, parent = slot
        if not self._join(parent):
            return
        node = self._new("text" if where in ("nav", "list") else kind, label,
                         detail if where in ("form", "section") else "")
        if node:
            parent["children"].append(node)
            if where == "section":
                self.last_text = (kind, label)

    def _container(self, kind: str, label: str) -> dict | None:
        if self.nav is not None:
            return None
        parent = self._section()
        if not self.run and not self._join(parent):
            return None                      # (behind a held word, it joins in page order below)
        node = self._new(kind, clean_text(label) or kind.capitalize())
        if node is None:
            return None
        node["children"] = []
        self._later(self._append, parent, node)
        return node

    # -- parser events ---------------------------------------------------------------
    def _enter_skip(self, tag):
        self.skip_tag, self.skip = tag, 1

    def handle_starttag(self, tag, attrs):
        if self._over_time():
            return
        if tag not in _INLINE:
            self._flush_data()
        if self.skip:
            if tag == self.skip_tag:
                self.skip += 1
            return
        # Only these attributes are ever read, and only as label text; a token
        # the 400-character limit cuts is dropped whole.
        a = {k: _cut(v or "", 400)[0] for k, v in attrs[:64]
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
                self._later(self.screen["children"].append, node)
                self.nav = node
            return
        if tag in _SECTIONS and self.nav is None:
            self.section = None
            self._later(self._join, self._section(clean_text(a.get("aria-label", "")) or tag.capitalize()))
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
            self.buffers.append([tag, "text", [], False])   # a menu link's words, never its address
            return
        buttonish = tag == "button" or (tag == "a" and (a.get("role") == "button"
                                                        or re.search(r"\b(btn|button|cta)\b", a.get("class", ""))))
        if buttonish or tag in _HEADINGS or tag == "li" or tag in _TEXT_BLOCKS:
            if tag in _TEXT_BLOCKS or tag == "li":
                self._flush_open()           # a block inside a block starts a new line
            kind = "button" if buttonish else "heading" if tag in _HEADINGS else "text"
            self.buffers.append([tag, kind, [], False])
        if tag == "br":
            self._resolve()                  # a line break ends any word
            self.dropping = False
            if self.buffers:
                self.buffers[-1][2].append(" ")

    @staticmethod
    def _words(entry) -> str:
        text = "".join(entry[2])
        return _drop_tail(text) if entry[3] else text     # its last token may run on past the limit

    def _flush_open(self):
        entries = [entry for entry in self.buffers if entry[1] == "text"]
        if self.run:
            # Held pieces may still land in these elements: their words are
            # read in page order, once the word resolves.
            self.queue.append((self._flush_entries, (entries, self._slot("text"))))
            return
        for entry in entries:
            if entry[2]:
                self._add("text", self._words(entry))
                entry[2], entry[3] = [], False

    def _flush_entries(self, entries, slot) -> None:
        for entry in entries:
            if entry[2]:
                self._emit("text", self._words(entry), "", slot)
                entry[2], entry[3] = [], False

    def handle_endtag(self, tag):
        if self._over_time():
            return
        if tag not in _INLINE:
            self._flush_data()
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
            entry = self.buffers.pop()
            if self.run:
                # A held piece may still belong to this element: its words
                # are read when the word resolves, in page order.
                self.queue.append((self._emit_entry, (entry, self._slot(entry[1]))))
            else:
                text = self._words(entry)
                if text.strip():
                    self._add(entry[1], text)
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
        room = MAX_CALLBACK_CHARS + 1 - self.pending_len   # one more than is read: shows a cut
        if room > 0:
            self.pending.append(data[:room])
            self.pending_len += min(len(data), room)

    def _flush_data(self, cut_short: bool = False):
        """Read the joined run: never more of it than MAX_CALLBACK_CHARS, and a
        token a limit cuts goes whole."""
        if not self.pending:
            return
        data = "".join(self.pending)
        self.pending, self.pending_len = [], 0
        data, lost = _cut(data, MAX_CALLBACK_CHARS)
        if cut_short or lost:
            data = _drop_tail(data)          # a limit or a stop cut the last word: it goes whole
        # Text in no element collects in an entry of its own: one node, as the
        # page wrote it, placed once its last piece is.
        target = self.buffers[-1] if self.buffers else [None, "text", [], False]
        if not data:
            if lost:                         # one token past the limit: the word before it runs into it
                self._drop_run()
                self.dropping = True
            return
        lead = re.match(r"\S*", data).group(0)
        if lead and (self.run or self.dropping):
            if not self.dropping:
                self._hold(lead, target)     # the held word goes on past the tag
            data = data[len(lead):]          # (a dropped one goes on being dropped)
            if not data:
                self.dropping = self.dropping or lost
                self._loose(target)
                return                       # still unbroken
        self._resolve()                      # whitespace: the word before it has ended
        tail = "" if data[-1].isspace() else re.search(r"\S+$", data).group(0)
        body = data[:len(data) - len(tail)]
        if body:
            self._put(body, target)
        if tail:
            self._hold(tail, target)
        self.dropping = lost                 # a cut text's last word may go on past the next tag
        self._loose(target)

    def _loose(self, entry) -> None:
        if entry[0] is None:                 # text in no element: one node, once its last piece is placed
            self._later(self._emit_entry, entry, self._slot("text"))

    def _put(self, text: str, entry) -> None:
        """Words the page has finished, to the element they are in."""
        budget = LABEL_MAX * 4 - sum(len(w) for w in entry[2])
        if budget <= 0:
            entry[3] = entry[3] or bool(text)
            return
        piece, over = _cut(text, budget)
        entry[2].append(piece)
        entry[3] = entry[3] or over

    def _hold(self, text: str, entry) -> None:
        self.run.append((text, entry))
        self.queue.append((self._place, (len(self.run) - 1,)))
        self.run_len += len(text)
        if self.run_len > MAX_RUN_CHARS:
            self._drop_run()                 # an unbroken run this long says nothing worth keeping
            self.dropping = True             # nor does the rest of it

    def _place(self, index: int) -> None:
        if self.placed[index]:
            self._put(self.placed[index], self.run_done[index][1])

    def _emit_entry(self, entry, slot) -> None:
        text = self._words(entry)
        if text.strip():
            self._emit(entry[1], text, "", slot)

    def _drop_run(self) -> None:
        self._settle([""] * len(self.run))

    def _resolve(self) -> None:
        """The held word has ended: it is read as ONE word, the way clean_text
        reads a label - addresses on the original code points, then again once
        "<>" and control or format code points are gone - so a character
        stripped at a tag boundary cannot assemble an address that no piece
        shows whole (Cursor NO-GO on 3fef998). Each stage runs over the whole
        word, and moves only what it changed (_spread); the strip itself is
        per character, so it moves nothing."""
        if not self.run:
            return
        texts = [text for text, _ in self.run]
        if len(texts) > 1:
            texts = _spread(texts, redact("".join(texts)))
            texts = [_strip_unsafe(text) for text in texts]
            texts = _spread(texts, redact("".join(texts)))
        self._settle(texts)

    def _settle(self, texts) -> None:
        """The held word is resolved (or dropped): each piece becomes its text
        in ``texts``, and every change that waited behind it runs, in order."""
        self.run_done, self.placed = self.run, texts
        self.run, self.run_len = [], 0
        queue, self.queue = self.queue, []
        for fn, args in queue:
            fn(*args)
        self.run_done, self.placed = [], []

    def finish(self) -> dict:
        # A parse that stopped early may have stopped inside a word: the last
        # run, and every open element's last token, are dropped (Codex Gate 1
        # on ecee267) rather than kept as part of an address.
        self._flush_data(cut_short=self.stopped)
        if self.stopped:
            self._drop_run()                 # a word the stop may have cut is never materialised
            for entry in self.buffers:
                entry[3] = True
        else:
            self._resolve()
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
    text, builder.stopped = _cut(html or "", MAX_HTML_CHARS)
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
        builder.stopped = True
    return builder.finish()


def tree_nodes(tree: dict) -> int:
    return 1 + sum(tree_nodes(c) for c in tree.get("children") or [])


def tree_depth(tree: dict) -> int:
    return 1 + max((tree_depth(c) for c in tree.get("children") or []), default=0)


__all__ = ["PageFetchError", "FetchBusy", "FetchTimeout", "FetchCancelled", "WorkDeadline", "address_ok", "fetch_page", "page_to_tree", "clean_text", "resolve_pinned",
           "system_resolver", "tree_nodes", "tree_depth", "MAX_NODES", "MAX_DEPTH", "MAX_IMAGES", "LABEL_MAX"]
