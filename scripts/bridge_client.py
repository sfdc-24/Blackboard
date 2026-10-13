#!/usr/bin/env python3
"""The direct route to Redis for an agent outside the private network. Stdlib only.

WHY THIS EXISTS
    The owner, 2026-10-13: "opertionalizing redis is number 1 priority right now". Measured the same
    night: the governed board route answers in about ten seconds and nobody uses it, because for an
    agent on a laptop every message is two board rows and a wait. cloud/redis-tool-bridge's
    POST /op (Blackboard PR 400) takes one governed operation from a VERIFIED caller, with no row at
    all. This module is the client for it: docs/REDIS-PRIMARY.md, Phase 3b.

IT IS OFF UNLESS THREE THINGS ARE SET, AND NOTHING DEPLOYED ANSWERS IT YET
    REDIS_BRIDGE=on                     the per-agent switch. Anything else is off: the caller uses
                                        the governed board route exactly as before.
    REDIS_BRIDGE_URL=https://...        the bridge's own URL. It is also the token's audience.
    REDIS_BRIDGE_IMPERSONATE=<sa email> the service account this agent acts as.
    The switch is per agent because the environment is: one machine turns it off and no other is
    touched. The deployed bridge is probe-only today (decision D1 is open), so switching this on
    now meets a 404, which is a failure this module says out loud and falls back from.

THE TOKEN
    Minted by `gcloud auth print-identity-token --impersonate-service-account=<sa>
    --audiences=<url> --include-email`: short-lived, for this audience only, by impersonation.
    NEVER a key file (decision D2: "no key files"). The token is never printed, logged, written to
    disk or put in a URL, and it is sent to the configured host and nowhere else: a redirect is
    refused rather than followed, because following one would carry the token to its target.

EVERY NON-2xx IS A FAILURE SAID OUT LOUD (REDIS-PRIMARY 8.8: "a refusal that nobody hears")
    There are two kinds, and they are not treated alike.
    - The BRIDGE failed: no token, no connection, a redirect, any status but 200, an answer that
      is not the JSON it should be. This is said, counted, and the caller falls back to the board
      route for this message.
    - The bridge worked and redis_gov REFUSED the operation (403 "refused"). This is said with
      redis_gov's own reason and it is NOT retried on the board route: that route runs the same
      access list and would refuse it again. It does not count against the bridge either.

THE BREAKER
    After three bridge failures in a row this agent stays on the board route for fifteen minutes
    and says so ONCE, in one row (the `announce` callable; the caller posts it). So a broken bridge
    costs latency, not messages, and not a token mint and a timeout on every send. A success or a
    governed refusal closes it: both prove the bridge is answering.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(REPO, ".redis_bridge_breaker.json")
SWITCH_ENV = "REDIS_BRIDGE"
URL_ENV = "REDIS_BRIDGE_URL"
IMPERSONATE_ENV = "REDIS_BRIDGE_IMPERSONATE"
TRIP_AFTER = 3
OPEN_SECONDS = 900
HTTP_TIMEOUT = 20
MINT_TIMEOUT = 60
# A Cloud Run URL and nothing looser: https, a host, no path, no query, no credentials in it.
_URL = re.compile(r"^https://[a-z0-9][a-z0-9.-]{2,250}$")
_SA = re.compile(r"^[a-z0-9][a-z0-9-]{4,62}@[a-z0-9-]{4,40}\.iam\.gserviceaccount\.com$")
_JWT = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")


class BridgeFailure(Exception):
    """The bridge itself failed. The message is safe to print: never a token, never a body."""


class Refused(Exception):
    """The bridge answered and redis_gov refused the operation. The message is redis_gov's reason."""


def config(environ=None):
    """(url, service account) when this agent's switch is on and both are well formed, else None.

    Off is silent: it is the default, and an agent that never asked for the bridge should not be
    told about it on every send. On-but-malformed is NOT silent; see attempt()."""
    env = os.environ if environ is None else environ
    if str(env.get(SWITCH_ENV) or "").strip().lower() != "on":
        return None
    url = str(env.get(URL_ENV) or "").strip().rstrip("/").lower()
    account = str(env.get(IMPERSONATE_ENV) or "").strip().lower()
    if not _URL.match(url) or not _SA.match(account):
        raise BridgeFailure("%s is on but %s or %s is missing or malformed"
                            % (SWITCH_ENV, URL_ENV, IMPERSONATE_ENV))
    return url, account


def mint(url: str, account: str, run=subprocess.run, which=shutil.which) -> str:
    """One identity token for `url`, as `account`, by impersonation. Raises BridgeFailure.

    Nothing gcloud printed is ever repeated: its error text can name accounts and its output IS the
    credential. A failure is reported as its exit code and nothing else."""
    tool = which("gcloud")
    if not tool:
        raise BridgeFailure("gcloud is not on PATH, so no token can be minted")
    try:
        done = run([tool, "auth", "print-identity-token",
                    "--impersonate-service-account=" + account,
                    "--audiences=" + url, "--include-email"],
                   capture_output=True, text=True, timeout=MINT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BridgeFailure("minting a token timed out after %d s" % MINT_TIMEOUT) from None
    except OSError as error:
        raise BridgeFailure("gcloud could not be run (%s)" % type(error).__name__) from None
    if done.returncode != 0:
        raise BridgeFailure("gcloud could not mint a token (exit %d)" % done.returncode)
    token = (done.stdout or "").strip()
    if not _JWT.match(token):
        raise BridgeFailure("gcloud returned something that is not an identity token")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure. Following one would send the bearer token to the redirect's target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def call(url: str, token: str, request: dict, opener=None) -> dict:
    """POST one operation to <url>/op. The result dict on 200; Refused; or BridgeFailure."""
    body = json.dumps(request, sort_keys=True).encode("utf-8")
    req = urllib.request.Request(url + "/op", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    opener = opener or urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as response:
            status, raw = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            answer = json.loads(error.read(65536).decode("utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable error body is still just this status
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        said = str(answer.get("error") or "")[:60]
        if status == 403 and said == "refused":
            raise Refused(str(answer.get("reason") or "refused, with no reason given")[:300]) from None
        raise BridgeFailure("HTTP %d%s" % (status, (" " + said) if said else "")) from None
    except Exception as error:  # noqa: BLE001 - the TYPE only: a message can quote the request
        raise BridgeFailure("no answer from the bridge (%s)" % type(error).__name__) from None
    if status != 200:
        raise BridgeFailure("HTTP %d" % status)
    try:
        answer = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise BridgeFailure("HTTP 200 with a body that is not JSON") from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise BridgeFailure("HTTP 200 without ok: true")
    return answer


# ------------------------------------------------------------------------------------- the breaker

def load_state(path=None) -> dict:
    try:
        with open(path or STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        return {"failures": int(data.get("failures") or 0),
                "open_until": float(data.get("open_until") or 0.0)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"failures": 0, "open_until": 0.0}


def save_state(state: dict, path=None) -> None:
    with open(path or STATE, "w", encoding="utf-8", newline="") as fh:
        json.dump(state, fh, sort_keys=True)


def clock(moment: float) -> str:
    return time.strftime("%H:%M:%SZ", time.gmtime(moment))


def attempt(op: str, key: str, *, value=None, count=None, environ=None, now=None, state_path=None,
            say=print, announce=None, minter=None, caller=None):
    """Try one operation through the bridge.

    Returns the bridge's result dict on success. Returns None when the caller should use the
    governed board route: the switch is off (silently), the breaker is open, or the bridge failed
    (both said). Raises Refused when redis_gov refused the operation: the caller must NOT retry it
    on the board route.

    announce(text) is called at most once per trip of the breaker, at the moment it opens.
    """
    now = time.time() if now is None else now
    try:
        found = config(environ)
    except BridgeFailure as why:
        say("BRIDGE NOT USED: %s. Using the board route." % why)
        return None
    if found is None:
        return None
    url, account = found
    state = load_state(state_path)
    if now < state["open_until"]:
        say("bridge breaker is open until %s; using the board route" % clock(state["open_until"]))
        return None
    request = {"op": op, "key": key}
    if value is not None:
        request["val"] = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
        request["enc"] = "b64"
    if count is not None:
        request["count"] = int(count)
    try:
        token = (minter or mint)(url, account)
        result = (caller or call)(url, token, request)
    except Refused:
        _record(state_path, {"failures": 0, "open_until": 0.0}, say)   # the bridge answered
        raise
    except BridgeFailure as why:
        failures = state["failures"] + 1
        if failures >= TRIP_AFTER:
            until = now + OPEN_SECONDS
            _record(state_path, {"failures": 0, "open_until": until}, say)
            line = ("the Redis bridge failed %d times in a row (last: %s); this agent uses the "
                    "board route until %s" % (TRIP_AFTER, why, clock(until)))
            say("BRIDGE FAILED (%d of %d): %s. BREAKER OPEN: %s." % (failures, TRIP_AFTER, why, line))
            if announce is not None:
                try:
                    announce(line)
                except Exception as error:  # noqa: BLE001 - the announcement must not cost the message
                    say("the breaker's one row could not be posted (%s)" % type(error).__name__)
        else:
            _record(state_path, {"failures": failures, "open_until": 0.0}, say)
            say("BRIDGE FAILED (%d of %d): %s. Using the board route for this message."
                % (failures, TRIP_AFTER, why))
        return None
    if state["failures"] or state["open_until"]:
        _record(state_path, {"failures": 0, "open_until": 0.0}, say)
    return result


def _record(path, state, say) -> None:
    """Persist the breaker. A state file that cannot be written is said, and is not a reason to
    lose the message: the worst case is one more attempt against a broken bridge."""
    try:
        save_state(state, path)
    except OSError as error:
        say("the bridge breaker's state could not be saved (%s)" % type(error).__name__)
