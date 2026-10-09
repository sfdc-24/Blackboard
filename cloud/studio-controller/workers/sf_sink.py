"""Where the build lane publishes each step, so the conference room can read it live.

Mr. Salam (2026-10-09): "essentially this will require building connection
between redis conference and salesforce org". Every build event - options
offered, the pick, the plan proposed, validating, validated or the error, the
confirm asked, deploying, deployed with counts, the built objects read back,
undo - goes to one injectable sink as a FLAT STRING MAP with no PII:

    {session_id, room, seq, phase, plan_hash, objects, components, result, at}

    phase       discuss | options | prototype | build | display  (the lane's phase)
    result      the step and its outcome: "options_offered", "validated", "deploy_failed", ...
    objects     comma-separated API names (metadata names, never record data)
    components  how many metadata components the step covers
    at          UTC ISO-8601

KEY LAYOUT (documented here; the Redis sink writes exactly this):

    conf:sf:<session_id>:events   a stream (XADD, MAXLEN ~ 500), TTL 7 days
    conf:sf:<session_id>:state    a hash holding the latest record (phase, result, ...), TTL 7 days

The default sink is a no-op log line: this service has no Redis access yet.
RedisSink is behind STUDIO_SF_REDIS (default off) and connects the way
scripts/redis_dual.py does: TLS to a PRIVATE address, trusting ONLY the mounted
CA (no public roots), AUTH from a mounted file. Never the host in a log line.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time

FIELDS = ("session_id", "room", "seq", "phase", "plan_hash", "objects", "components", "result", "at")
TTL_SECONDS = 7 * 24 * 3600
STREAM_MAXLEN = 500
DEFAULT_PORT = 6378
MOUNTED_CA = "/secrets/ca/redis-ca.pem"
MOUNTED_AUTH = "/secrets/auth/redis-auth"


def stream_key(session_id: str) -> str:
    return "conf:sf:%s:events" % session_id


def state_key(session_id: str) -> str:
    return "conf:sf:%s:state" % session_id


def record(session_id: str, seq: int, phase: str, result: str, *, plan_hash: str = "", objects=(),
           components: int = 0, room: str = "", now=None) -> dict:
    """The flat string map, closed to FIELDS. Nothing a person typed goes in it."""
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    return {"session_id": str(session_id), "room": str(room or ""), "seq": str(int(seq)),
            "phase": str(phase), "plan_hash": str(plan_hash or ""), "objects": ",".join(objects or ()),
            "components": str(int(components or 0)), "result": str(result), "at": at}


def _log(event: str, **fields) -> None:
    line = {"severity": "INFO", "event": event}
    line.update(fields)
    print(json.dumps(line, sort_keys=True), file=sys.stdout, flush=True)


class LogSink:
    """The default: one log line per record, no connection anywhere."""

    def publish(self, rec: dict) -> None:
        _log("studio.sf_build_sink", **{k: rec[k] for k in ("session_id", "seq", "phase", "result")})


def private_ca_context(ca_path: str) -> ssl.SSLContext:
    """A TLS context that trusts the ONE mounted CA and nothing else (the redis_dual.py
    pattern): never create_default_context, never the public bundle."""
    if not ca_path:
        raise ValueError("a private-CA context needs a CA path")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False   # Memorystore's SAN is not read yet; the chain must end at the mounted CA
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    context.load_verify_locations(cafile=str(ca_path))
    return context


def _connection_class(redis_module):
    base = redis_module.connection.SSLConnection

    class PrivateCASSLConnection(base):
        def _wrap_socket_with_ssl(self, sock):
            return private_ca_context(self.ca_certs).wrap_socket(sock, server_hostname=self.host)

    return PrivateCASSLConnection


class RedisSink:
    """XADD to conf:sf:<sid>:events and HSET conf:sf:<sid>:state, both with a 7-day TTL.

    A failure never reaches the lane: it is counted and logged (without the host),
    and the build carries on - the session record stays the source of truth."""

    def __init__(self, client=None, *, environ=None):
        self.failures = 0
        self._client = client
        self._environ = environ if environ is not None else os.environ

    def _connect(self):
        if self._client is not None:
            return self._client
        env = self._environ
        host = (env.get("REDIS_HOST") or "").strip()
        port = int((env.get("REDIS_PORT") or str(DEFAULT_PORT)).strip())
        ca = (env.get("REDIS_CA_CERT_PATH") or "").strip() or (MOUNTED_CA if os.path.exists(MOUNTED_CA) else "")
        auth_path = (env.get("REDIS_AUTH_FILE") or "").strip() or (MOUNTED_AUTH if os.path.exists(MOUNTED_AUTH) else "")
        if not host or not ca or not auth_path:
            raise RuntimeError("REDIS_HOST, REDIS_CA_CERT_PATH and REDIS_AUTH_FILE are required")
        with open(auth_path, encoding="utf-8") as fh:
            password = fh.read().strip()
        import redis  # only when the sink is switched on
        pool = redis.ConnectionPool(connection_class=_connection_class(redis), host=host, port=port,
                                    password=password, ssl_ca_certs=ca, socket_timeout=3,
                                    socket_connect_timeout=3)
        self._client = redis.Redis(connection_pool=pool)
        return self._client

    def publish(self, rec: dict) -> None:
        rec = {k: str(rec.get(k, "")) for k in FIELDS}
        sid = rec["session_id"]
        try:
            client = self._connect()
            pipe = client.pipeline(transaction=False)
            pipe.xadd(stream_key(sid), rec, maxlen=STREAM_MAXLEN, approximate=True)
            pipe.expire(stream_key(sid), TTL_SECONDS)
            pipe.hset(state_key(sid), mapping=rec)
            pipe.expire(state_key(sid), TTL_SECONDS)
            pipe.execute()
        except Exception as exc:          # never the host, never the auth string
            self.failures += 1
            _log("studio.sf_build_sink_failed", session_id=sid, seq=rec["seq"], reason=type(exc).__name__,
                 failures=self.failures)


def sink_from_env(enabled: bool):
    return RedisSink() if enabled else LogSink()


__all__ = ["record", "LogSink", "RedisSink", "sink_from_env", "stream_key", "state_key", "FIELDS",
           "TTL_SECONDS"]
