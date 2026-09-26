"""A provider call with a hard wall-clock budget, a real cancel, and a fence.

WHY (Codex NO-GO on #272 at 8a9349e): the SDK's timeout is per operation, not
a total deadline. A body that trickles in a byte at a time never trips it - a
loopback probe with a 2 s timeout finished after 3.5 s - so a slow provider
can still carry a request past Cloud Run's 60 s while the thread keeps going.

    run_within(seconds, fn, cancel)   the caller waits at most `seconds` by the
        monotonic clock. The work runs on its own daemon thread; at the
        deadline the caller marks it abandoned (the fence: nothing it returns
        later is ever read), calls `cancel`, and raises DeadlineExceeded.
    abortable(client)                 a per-call copy of an Anthropic client on
        its own connection pool whose sockets `abort()` shuts down, so a read
        already blocked in another thread returns at once instead of at its
        read timeout. A client without `with_options` (a test fake) is used as
        it is and abort does nothing.
    classify(exc)                     "transient" (the deadline, a timeout, a
        dropped connection, 408/409/429, 5xx): nothing changed and it may work
        in a moment. "permanent" (any other 4xx - 400, 401, 403 - or a model
        refusal): it will not work by asking again. None: not a provider fault
        (a bug), which must still fail loudly.
Neither class is retried here or in the SDK (max_retries=0).
"""
from __future__ import annotations

import socket
import threading
import time


class DeadlineExceeded(Exception):
    """The budget ran out before the provider answered."""


def run_within(seconds: float, fn, cancel=None):
    lock = threading.Lock()
    done = threading.Event()
    box: dict = {}

    def work():
        try:
            outcome = (True, fn())
        except BaseException as exc:  # handed to the caller, never swallowed
            outcome = (False, exc)
        with lock:
            if not box.get("abandoned"):
                box["outcome"] = outcome
        done.set()

    worker = threading.Thread(target=work, name="bounded-provider-call", daemon=True)
    deadline = time.monotonic() + seconds
    worker.start()
    done.wait(max(0.0, deadline - time.monotonic()))
    with lock:
        outcome = box.get("outcome")
        if outcome is None:
            box["abandoned"] = True           # the fence: a late result is never read
    if outcome is None:
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        raise DeadlineExceeded("no answer within %.1f s" % seconds)
    ok, value = outcome
    if ok:
        return value
    raise value


def classify(exc) -> str | None:
    if isinstance(exc, (DeadlineExceeded, TimeoutError, ConnectionError)):
        return "transient"
    try:
        import anthropic
    except ImportError:
        return None
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return "transient"
    if isinstance(exc, anthropic.APIStatusError):
        code = int(getattr(exc, "status_code", 0) or 0)
        if code in (408, 409, 429) or code >= 500:
            return "transient"
        if 400 <= code < 500:
            return "permanent"
    return None


class _Tracked:
    """A network stream whose socket can be shut down from another thread."""

    def __init__(self, inner, owner):
        self._inner = inner
        owner._track(self)
        self._owner = owner

    def read(self, max_bytes, timeout=None):
        return self._inner.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        return self._inner.write(buffer, timeout)

    def close(self):
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _Tracked(self._inner.start_tls(ssl_context, server_hostname, timeout), self._owner)

    def get_extra_info(self, info):
        return self._inner.get_extra_info(info)


class _Backend:
    """httpcore2's sync backend, with every stream it opens tracked for abort."""

    def __init__(self):
        import httpcore2
        self._inner = httpcore2.SyncBackend()
        self._lock = threading.Lock()
        self._streams: list = []
        self._aborted = False

    def _track(self, stream):
        with self._lock:
            self._streams.append(stream)
            aborted = self._aborted
        if aborted:
            self._shut(stream)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _Tracked(self._inner.connect_tcp(host, port, timeout, local_address, socket_options), self)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return _Tracked(self._inner.connect_unix_socket(path, timeout, socket_options), self)

    def sleep(self, seconds):
        self._inner.sleep(seconds)

    @staticmethod
    def _shut(stream):
        sock = stream.get_extra_info("socket")
        if sock is None:
            return
        try:
            # The base method: on a TLS socket this shuts the connection down
            # without touching the SSL object a blocked read is using.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass                              # already closed, or detached by the TLS upgrade

    def abort(self):
        with self._lock:
            self._aborted = True
            streams = list(self._streams)
        for stream in streams:
            self._shut(stream)


class _Call:
    def __init__(self, client, backend=None, http=None):
        self.client, self._backend, self._http = client, backend, http

    def abort(self):
        if self._backend is not None:
            self._backend.abort()

    def close(self):
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass


def abortable(client) -> _Call:
    with_options = getattr(client, "with_options", None)
    if with_options is None:
        return _Call(client)
    import anthropic
    import httpx2
    backend = _Backend()
    transport = httpx2.HTTPTransport()
    transport._pool._network_backend = backend   # httpcore2.ConnectionPool(network_backend=...)
    http = anthropic.DefaultHttpxClient(transport=transport)
    return _Call(with_options(http_client=http), backend, http)


__all__ = ["DeadlineExceeded", "run_within", "classify", "abortable"]
