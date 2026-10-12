"""One spoken change to ONE Case's Priority in the developer org, heard through the conference's live
transcript, run as a Cloud Run JOB execution - never on a laptop, never in a browser session.

    his words -> the chair's live transcript object (GCS) -> this job -> one PATCH -> read back -> restore

THE SCOPE IS PINNED IN SOURCE, not in configuration (Aya on 6753b44: an env var is not an approval).
The approved org, Case and values are module constants; the environment must REPEAT the org and Case
to confirm them, and a mismatch refuses before any network. Approval: AYA-CASE-00001036-EXACT-OWNER-
APPROVAL-20261012 (his YES of 2026-10-12 14:35:54Z; his typed approval in the builder's session at
15:10Z): org 00Dbm00000wK2ibEAC, Case 500bm000039LHSAAA4, Priority, Medium to High, verified by
read-back, then restored to Medium. The Case must HOLD Medium at arming, or the arming refuses:
Low-to-High-to-Low is nobody's approval.

ONE EXECUTION IS ONE ARMING, BOUND TO ONE ROOM, AND CONSUMED DURABLY (Aya: memory is not one-shot)
- DEMO_ARMED=1 or the job exits at its config, naming the missing variable and never a value.
- DEMO_ROOM is required and exact; the transcript's OWN HEADER must name that room on every read, or
  the run refuses. There is no "whichever transcript appears next" mode.
- Before anything is heard, the arming is CONSUMED: a create-once object (ifGenerationMatch=0) under
  conference/devorg-demo/ keyed by the room. A Cloud Run retry or a rerun of the same arming finds
  the object and refuses - memory-only state cannot survive a platform retry, so the bucket holds
  the one-shot. The deploy also sets the job's maxRetries to 0; this guard assumes it did not.
- The arming expires (DEMO_EXPIRES_MINUTES, 1..45, monotonic clock).

ONE CHANGE, ONE RESTORE, AND AN UNCERTAIN WRITE IS STILL A WRITE (Aya on 6753b44)
- At most one change and one restore per execution.
- BEFORE the PATCH leaves, a pending-undo journal object is written durably (create-once): if this
  process dies mid-write, the journal says what must be reconciled and to which value. The write is
  then recorded IN FLIGHT in memory too: a lost response or a raising read-back still reconciles.
- The restore runs at the spoken word, at expiry, at the call closing, or on any error - exactly
  once. It claims "restored" ONLY from an actual read-back holding Medium: a restore attempt that
  itself fails is reported as UNKNOWN (exit 1), never as done, and the journal remains for the
  operator.

WHAT A LINE MUST BE
- An Owner line of the bound room's transcript, WRITTEN AFTER this execution first saw that
  transcript live, and seen while the transcript still says live: a snapshot that says closed is a
  record of a finished call, and no command is taken from it (closed is checked BEFORE new lines).
- A negation or a question mark ANYWHERE in the line refuses the whole line ("Do not do this.
  Set the ..." and "Would this be right? Set the ..." are not requests; a substring cannot carry
  a negation, and neither can sentence order).
- One sentence of the shape: an instruction verb, then "the demo case priority" with nothing else
  between its words, ending "to high" or "to medium": "Set the demo case priority to high." /
  "Put the demo case priority back to medium." A question, two values in one sentence, extra words
  between the verb and the subject ("the OTHER demo case") or after the value ("...to high please"):
  not a request. "to low" is out of the approved scope and refused with a receipt.
- Each request is deduplicated by a correlation id of room, place, time and text.

THE ORG, AND THE ONE WRITE
- Identity is the org's existing integration credential (client-credentials connected app), read
  from the environment where the deploy bound the Secret Manager secrets Headless_domain /
  Headless_consumer_key / Headless_consumer_secret. No secret value and no token is ever printed.
- The token endpoint is called before any other request is BUILT (Aya on 6753b44: the instance URL
  does not exist until the token answer names it).
- The token host and the answering instance must be a developer or sandbox host; the org must answer
  as the pinned org and be a Developer Edition or sandbox, checked by query before any write.
- The one PATCH carries "Sforce-Auto-Assign: FALSE": a bare REST update applies the org's active
  assignment rule, and this org's rules reassign and notify on escalation.

RECEIPTS
- One JSON object per event to gs://<TRANSCRIPT_BUCKET>/conference/devorg-demo/ (the shape shared
  with the site's demo page), each carrying the room, the Case and the correlation it answers, and
  one log line each, best effort: the restore outranks the paper trail. An org error is logged as
  its status and errorCode only - never the error body, which can carry record data.

TWO MORE GATES AT THE WRITE, AND A RECOVERY (Aya on 3ac1457)
- The expiry is rechecked immediately before the PATCH: polling and reads can cross the deadline
  after the loop entered. And the Case must hold Medium NOW, read just before the write: an edit
  from elsewhere after arming must abort the mutation, not widen the cycle. Salesforce REST has no
  field-conditional update; read-abort plus the read-back is the close.
- DEMO_RECOVER_CORRELATION=<id> runs the bounded RECOVERY of that one journalled operation instead
  of a watch: it consumes no arming, reads no transcript, makes no new change, is itself one-shot
  (a resolved marker), restores only the journal's known applied value, and leaves any third value
  for a human.

Exit: 0 done, or expired unspent; 1 a write's outcome is not the asked value, an unexpected error
(restored first), or a restore whose own outcome is UNKNOWN; 2 config refused; 3 token or org
refused, or a recovery finding a third value; 4 transcript, journal or one-shot guard refused.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "v62.0"
PREFIX = "conference/transcripts"
RECEIPTS = "conference/devorg-demo"
# The approved scope, pinned in source. Changing any of these is a source change for review,
# never a configuration choice.
APPROVED_ORG = "00Dbm00000wK2ibEAC"
APPROVED_CASE = "500bm000039LHSAAA4"
BEFORE_VALUE = "Medium"
AFTER_VALUE = "High"
MAX_MINUTES = 45
POLL_SECONDS = 2.0
ORG_ID = re.compile(r"^00D[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
CASE_ID = re.compile(r"^500[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
ROOM = re.compile(r"^conf-[a-z0-9][a-z0-9-]{0,60}$")
ALLOWED_HOSTS = ("develop.my.salesforce.com", "sandbox.my.salesforce.com", "scratch.my.salesforce.com")
OWNER_LINE = re.compile(r"^- \*\*\[(\d{2}:\d{2}:\d{2})Z\] Owner:\*\* (.*)$")
SENTENCE = re.compile(r"[^.?!]+[.?!]*")
LEAD_IN = r"(?:(?:okay|ok|alright|right|now|so|and|please|yes|yeah|hey|claude|grok|codex|gemini)[,\s]+)*"
# The subject is EXACTLY "the demo case priority": a word between the verb and the subject, between
# its words, or after the value is not this request ("the OTHER demo case", "...to high please").
INSTRUCTION = re.compile(
    r"^" + LEAD_IN + r"(?:set|change|make|update|move|mark|raise|lower|bump|put|switch|restore)\s+"
    r"(?:the\s+)?demo case(?:'s|’s)?\s+priority\s+(?:back\s+)?to (high|medium|low)$", re.I)
# A negation ANYWHERE in the line refuses the whole line: neither a substring nor sentence order
# can carry a negation safely.
NEGATION = re.compile(r"\b(?:not|never|no|cannot|without|stop|wait|cancel|don)\b|n't\b|n’t\b", re.I)
METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


class Refused(Exception):
    """Named, with its exit code; nothing further is done."""

    def __init__(self, code: int, why: str):
        super().__init__(why)
        self.code = code


def now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(line: str) -> None:
    print("devorg-demo: %s" % line, flush=True)


def error_code_only(body) -> str:
    """An org error, reduced to what is safe to log: errorCode values, never messages or fields."""
    codes = []
    for item in body if isinstance(body, list) else []:
        if isinstance(item, dict) and item.get("errorCode"):
            codes.append(str(item["errorCode"]))
    return ",".join(codes) or "(no errorCode)"


# ---------------------------------------------------------------- the words

def front_matter(markdown: str) -> dict:
    """The transcript's own header (the chair's transcript_file.py): room, state, held."""
    rows = markdown.split("\n")
    if not rows or rows[0].strip() != "---":
        return {}
    out = {}
    for row in rows[1:]:
        if row.strip() == "---":
            return out
        key, sep, value = row.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return {}                       # the header never closed: not a header


def owner_lines(markdown: str) -> list:
    """(place among the owner's lines, time, text) for each Owner line, in file order."""
    found = []
    for row in markdown.split("\n"):
        hit = OWNER_LINE.match(row.rstrip("\r"))
        if hit:
            found.append((len(found), hit.group(1), hit.group(2).strip()))
    return found


def request(text: str):
    """The Priority value one Owner line asks for, or None: no negation and no question mark
    ANYWHERE in the line (a hypothetical beside an imperative poisons the whole utterance), and
    exactly one asking sentence of the exact shape."""
    if NEGATION.search(text) or "?" in text:
        return None
    asked = []
    for sentence in SENTENCE.findall(text):
        body = sentence.rstrip(".!").strip()
        if not body:
            continue
        hit = INSTRUCTION.match(body)
        if not hit:
            continue
        if len(re.findall(r"\b(?:high|medium|low)\b", body, re.I)) != 1:
            continue                # "from medium to high": say it plainly, or not at all
        asked.append(hit.group(1).capitalize())
    return asked[0] if len(asked) == 1 else None


def correlation(room: str, place: int, at: str, text: str) -> str:
    return hashlib.sha256(("%s\n%d\n%s\n%s" % (room, place, at, text)).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the two outside things

def _http(req: urllib.request.Request, timeout: float):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class Store:
    """The transcript bucket, with the job's own service-account token: reads, receipt writes, and
    the two create-once guards (the consumed arming, the pending-undo journal)."""

    def __init__(self, bucket: str):
        self.bucket = bucket
        self._token = ""
        self._until = 0.0

    def token(self) -> str:
        if time.monotonic() < self._until and self._token:
            return self._token
        req = urllib.request.Request(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
        status, body = _http(req, 10)
        if status != 200:
            raise Refused(4, "the metadata server answered HTTP %d for a storage token" % status)
        answer = json.loads(body.decode("utf-8"))
        self._token = answer["access_token"]
        self._until = time.monotonic() + max(60, int(answer.get("expires_in", 300)) - 60)
        return self._token

    def _get(self, url: str):
        token = self.token()
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        return _http(req, 30)

    def _put(self, name: str, entry: dict, once: bool) -> int:
        url = ("https://storage.googleapis.com/upload/storage/v1/b/%s/o?uploadType=media&name=%s%s"
               % (self.bucket, urllib.parse.quote(name, safe=""), "&ifGenerationMatch=0" if once else ""))
        token = self.token()
        req = urllib.request.Request(url, data=json.dumps(entry, sort_keys=True).encode("utf-8"),
                                     method="POST", headers={"Authorization": "Bearer " + token,
                                                             "Content-Type": "application/json"})
        status, _ = _http(req, 15)
        return status

    def transcript_of(self, room: str) -> str | None:
        """The bound room's one transcript object name, or None while it does not exist yet."""
        url = ("https://storage.googleapis.com/storage/v1/b/%s/o?prefix=%s&fields=items(name)"
               % (self.bucket, urllib.parse.quote("%s/%s/" % (PREFIX, room), safe="")))
        status, body = self._get(url)
        if status != 200:
            raise Refused(4, "listing the room's transcripts answered HTTP %d" % status)
        names = [item["name"] for item in json.loads(body.decode("utf-8")).get("items", [])
                 if item["name"].endswith("-conference-transcript.md")]
        if len(names) > 1:
            raise Refused(4, "the bound room has %d transcripts" % len(names))
        return names[0] if names else None

    def read(self, name: str) -> str:
        status, body = self._get("https://storage.googleapis.com/storage/v1/b/%s/o/%s?alt=media"
                                 % (self.bucket, urllib.parse.quote(name, safe="")))
        if status != 200:
            raise Refused(4, "reading the transcript answered HTTP %d" % status)
        return body.decode("utf-8", "replace")

    def read_json(self, name: str) -> dict:
        status, body = self._get("https://storage.googleapis.com/storage/v1/b/%s/o/%s?alt=media"
                                 % (self.bucket, urllib.parse.quote(name, safe="")))
        if status == 404:
            raise Refused(4, "there is no such journal object")
        if status != 200:
            raise Refused(4, "reading the journal answered HTTP %d" % status)
        return json.loads(body.decode("utf-8"))

    def receipt(self, entry: dict) -> None:
        """Best effort: a receipt that cannot land is said and skipped; the restore outranks it."""
        name = "%s/%s-%s.json" % (RECEIPTS, entry["at"].replace(":", ""), entry["correlation"])
        try:
            status = self._put(name, entry, once=False)
        except Exception as error:  # noqa: BLE001 - the restore must not die for a receipt
            status = -1
            say("receipt not written (%s)" % type(error).__name__)
        if status != 200:
            say("receipt answered HTTP %d" % status)

    def create_once(self, name: str, entry: dict) -> bool:
        """True = this call created the object; False = it already existed (412). Anything else,
        an unknown outcome included, refuses: an unknown write under a one-shot guard is not a yes."""
        status = self._put(name, entry, once=True)
        if status == 200:
            return True
        if status == 412:
            return False
        raise Refused(4, "a one-shot guard write answered HTTP %d" % status)


class Org:
    """The developer org through its existing integration credential: one PATCH, reads otherwise."""

    def __init__(self, domain: str, client_id: str, secret: str):
        host = urllib.parse.urlparse(domain if "://" in domain else "https://" + domain).netloc
        if not any(host == mark or host.endswith("." + mark) or host.endswith(mark) for mark in ALLOWED_HOSTS):
            raise Refused(3, "the org host is not a developer or sandbox host")
        self.domain = "https://" + host
        self._id = client_id
        self._secret = secret
        self.instance = ""
        self._token = ""

    def token(self) -> str:
        if self._token:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": self._id, "client_secret": self._secret}).encode()
        req = urllib.request.Request(self.domain + "/services/oauth2/token", data=body, method="POST")
        status, answer = _http(req, 30)
        if status != 200:
            raise Refused(3, "the token endpoint answered HTTP %d" % status)
        got = json.loads(answer.decode("utf-8"))
        instance = urllib.parse.urlparse(got.get("instance_url", "")).netloc
        if not any(instance == mark or instance.endswith(mark) for mark in ALLOWED_HOSTS):
            raise Refused(3, "the token's instance is not a developer or sandbox host")
        self.instance = "https://" + instance
        self._token = got["access_token"]
        return self._token

    def _call(self, method: str, path: str, payload=None, headers=()):
        # The token comes FIRST: the instance URL does not exist until the token answer names it
        # (Aya on 6753b44: the first query built a request against an empty endpoint).
        token = self.token()
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.instance + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in headers:
            req.add_header(name, value)
        status, body = _http(req, 30)
        return status, (json.loads(body.decode("utf-8")) if body.strip() else None)

    def query(self, soql: str) -> list:
        status, answer = self._call("GET", "/services/data/%s/query?q=%s" % (API, urllib.parse.quote(soql)))
        if status != 200:
            raise Refused(3, "a query answered HTTP %d (%s)" % (status, error_code_only(answer)))
        return answer["records"]

    def identity(self) -> dict:
        rows = self.query("SELECT Id, OrganizationType, IsSandbox FROM Organization")
        if len(rows) != 1:
            raise Refused(3, "the org did not name itself once")
        return {"org_id": rows[0]["Id"], "type": rows[0]["OrganizationType"], "sandbox": rows[0]["IsSandbox"]}

    def case(self, case_id: str) -> dict:
        rows = self.query("SELECT Id, CaseNumber, Subject, Priority, IsClosed FROM Case WHERE Id = '%s'" % case_id)
        if len(rows) != 1:
            raise Refused(3, "that Case was not found once")
        return rows[0]

    def set_priority(self, case_id: str, value: str) -> None:
        if value not in (BEFORE_VALUE, AFTER_VALUE):
            raise Refused(3, "not one of the approved values")
        # Sforce-Auto-Assign FALSE: a bare REST update applies the org's active assignment rule.
        status, answer = self._call("PATCH", "/services/data/%s/sobjects/Case/%s" % (API, case_id),
                                    {"Priority": value}, headers=(("Sforce-Auto-Assign", "FALSE"),))
        if status != 204:
            raise Refused(3, "the update answered HTTP %d (%s)" % (status, error_code_only(answer)))


# ---------------------------------------------------------------- the arming and the run

RECOVERY = re.compile(r"^[0-9a-f]{16}$")


def config(environ) -> dict:
    """The arming, from the execution's environment. The environment CONFIRMS the pinned scope,
    it cannot choose one. Refuses by NAME; no value is ever printed. With DEMO_RECOVER_CORRELATION
    set, this is the bounded RECOVERY of one journalled operation instead: no arming is consumed,
    no transcript is read, and the two modes refuse to combine."""
    recover = environ.get("DEMO_RECOVER_CORRELATION", "")
    if recover:
        if environ.get("DEMO_ARMED") == "1":
            raise Refused(2, "recovery and arming are different runs: not both")
        if not RECOVERY.match(recover):
            raise Refused(2, "DEMO_RECOVER_CORRELATION is malformed")
    elif environ.get("DEMO_ARMED") != "1":
        raise Refused(0, "not armed (DEMO_ARMED is not 1): nothing read, nothing written")
    out = {"recover": recover}
    rooms = () if recover else (("DEMO_ROOM", ROOM),)
    for name, shape in (("DEMO_ORG_ID", ORG_ID), ("DEMO_CASE_ID", CASE_ID)) + rooms:
        value = environ.get(name, "")
        if not shape.match(value):
            raise Refused(2, "%s is missing or malformed" % name)
        out[name] = value
    if out["DEMO_ORG_ID"][:15] != APPROVED_ORG[:15]:
        raise Refused(2, "DEMO_ORG_ID is not the approved org")
    if out["DEMO_CASE_ID"][:15] != APPROVED_CASE[:15]:
        raise Refused(2, "DEMO_CASE_ID is not the approved Case")
    try:
        minutes = int(environ.get("DEMO_EXPIRES_MINUTES", "20"))
    except ValueError:
        raise Refused(2, "DEMO_EXPIRES_MINUTES is not a number") from None
    if not 1 <= minutes <= MAX_MINUTES:
        raise Refused(2, "DEMO_EXPIRES_MINUTES is outside 1..%d" % MAX_MINUTES)
    out["minutes"] = minutes
    out["bucket"] = environ.get("TRANSCRIPT_BUCKET", "sfdc24-fleet-state")
    for name in ("Headless_domain", "Headless_consumer_key", "Headless_consumer_secret"):
        if not environ.get(name, "").strip():
            raise Refused(2, "%s is not bound" % name)
        out[name] = environ[name].strip()
    return out


class Demo:
    """At most one change and one restore, whatever happens around them. A write whose outcome was
    never heard counts as a write until the org says otherwise, and the journal of what to put back
    is durable BEFORE the write leaves."""

    def __init__(self, org: Org, store: Store, room: str, clock=time.monotonic, deadline: float = float("inf")):
        self.org, self.store = org, store
        self.room = room
        self.clock, self.deadline = clock, deadline
        self.changed = None             # the correlation of the one CONFIRMED change
        self.in_flight = None           # the correlation of a write whose outcome is unknown
        self.restored = False
        self.undo_unknown = False       # a restore attempt failed: never claimed as done
        self.held_wrong = False
        self.handled = set()            # correlations already answered, whatever they asked

    def receipt(self, **entry) -> dict:
        entry.setdefault("at", now_stamp())
        entry.setdefault("field", "Priority")
        entry.setdefault("room", self.room)
        entry.setdefault("case_id", APPROVED_CASE)
        self.store.receipt(entry)
        say("%s %s: asked %s, before %s, org holds %s" % (entry["kind"], entry["correlation"],
            entry.get("asked"), entry.get("before"), entry.get("read_back")))
        return entry

    def act(self, value: str, source: dict) -> None:
        if source["correlation"] in self.handled:
            return                      # the same line is never run twice
        self.handled.add(source["correlation"])
        if value == AFTER_VALUE and self.changed is None and self.in_flight is None:
            # The deadline is rechecked HERE, immediately before anything mutates: polling and
            # reads between loop entry and this point can cross it (Aya on 3ac1457).
            if self.clock() >= self.deadline:
                self.receipt(kind="refused", correlation=source["correlation"], source=source,
                             asked=value, before=None, read_back=None, held=None,
                             why="the arming expired before the write")
                return
            held = self.org.case(APPROVED_CASE)["Priority"]
            # The Case must hold Medium NOW, not merely at arming: an edit from elsewhere after
            # arming would otherwise run a cycle nobody approved (Aya on 3ac1457). Salesforce REST
            # has no field-conditional update, so the close is read-abort-readback; the read-back
            # below verifies the end state either way.
            if held != BEFORE_VALUE:
                self.receipt(kind="refused", correlation=source["correlation"], source=source,
                             asked=value, before=held, read_back=None, held=None,
                             why="the Case does not hold %s now" % BEFORE_VALUE)
                return
            # The journal first, durably: if this process dies between the PATCH and its read-back,
            # the pending object says what must be reconciled and to which value.
            created = self.store.create_once(
                "%s/pending-%s.json" % (RECEIPTS, source["correlation"]),
                {"kind": "pending-undo", "correlation": source["correlation"], "room": self.room,
                 "case_id": APPROVED_CASE, "field": "Priority", "restore_to": BEFORE_VALUE,
                 "before": held, "at": now_stamp(), "source": source})
            if not created:
                raise Refused(4, "this request's pending journal already exists: a rerun does not re-run it")
            # IN FLIGHT the moment the PATCH leaves (Aya on 6753b44: a commit followed by a timeout
            # left the change unrecorded and skipped the restore).
            self.in_flight = source["correlation"]
            self.org.set_priority(APPROVED_CASE, AFTER_VALUE)
            read_back = self.org.case(APPROVED_CASE)["Priority"]
            self.changed, self.in_flight = source["correlation"], None
            self.receipt(kind="change", correlation=source["correlation"], source=source,
                         asked=AFTER_VALUE, before=held, read_back=read_back, held=read_back == AFTER_VALUE)
            if read_back != AFTER_VALUE:
                self.held_wrong = True
        elif value == BEFORE_VALUE and self.changed and not self.restored:
            self.restore("the spoken word")
        else:
            self.receipt(kind="refused", correlation=source["correlation"], source=source, asked=value,
                         before=None, read_back=None, held=None,
                         why="out of the approved scope or out of order")

    def restore(self, why: str) -> None:
        """Put BEFORE_VALUE back - after a confirmed change, and equally after an uncertain write.
        Restored is claimed ONLY from a read-back holding it; a failing attempt is UNKNOWN."""
        if self.restored or self.undo_unknown or (self.changed is None and self.in_flight is None):
            return
        base = self.changed or self.in_flight
        if self.in_flight:
            why = "%s, reconciling a write whose outcome was never heard" % why
        try:
            held = self.org.case(APPROVED_CASE)["Priority"]
            if held != BEFORE_VALUE:
                self.org.set_priority(APPROVED_CASE, BEFORE_VALUE)
                read_back = self.org.case(APPROVED_CASE)["Priority"]
            else:
                read_back = held
        except Exception as error:  # noqa: BLE001 - an unknown outcome is reported, never claimed
            self.undo_unknown = True
            self.held_wrong = True
            say("UNDO UNKNOWN (%s): the pending journal for %s remains; reconcile by hand or rerun "
                "a fresh arming" % (type(error).__name__, base))
            try:
                self.receipt(kind="undo-unknown", correlation="undo-" + base, undoes=base,
                             asked=BEFORE_VALUE, before=None, read_back=None, held=None, why=why)
            except Exception:  # noqa: BLE001
                pass
            return
        self.restored = True
        self.receipt(kind="undo", correlation="undo-" + base, undoes=base,
                     asked=BEFORE_VALUE, before=held, read_back=read_back,
                     held=read_back == BEFORE_VALUE, why=why)
        if read_back != BEFORE_VALUE:
            self.held_wrong = True


def recover(store: Store, org: Org, correlation_id: str) -> int:
    """The bounded recovery of ONE journalled operation: restore its known applied value, exactly
    once, and nothing else. It consumes no arming, reads no transcript, makes no new change; a Case
    holding anything but the applied or the restore value is a human's decision, not this path's."""
    journal = store.read_json("%s/pending-%s.json" % (RECEIPTS, correlation_id))
    if journal.get("case_id") != APPROVED_CASE or journal.get("restore_to") != BEFORE_VALUE:
        raise Refused(4, "that journal does not describe the approved operation")
    room = str(journal.get("room", ""))
    if not ROOM.match(room):
        raise Refused(4, "that journal does not name a room")
    if not store.create_once("%s/resolved-%s.json" % (RECEIPTS, correlation_id),
                             {"kind": "resolved", "correlation": correlation_id, "at": now_stamp()}):
        raise Refused(4, "that operation was already recovered")
    demo = Demo(org, store, room)
    held = org.case(APPROVED_CASE)["Priority"]
    if held == BEFORE_VALUE:
        demo.receipt(kind="undo", correlation="undo-" + correlation_id, undoes=correlation_id,
                     asked=BEFORE_VALUE, before=held, read_back=held, held=True,
                     why="recovery: the Case already holds the restore value")
        return 0
    if held != AFTER_VALUE:
        demo.receipt(kind="refused", correlation="undo-" + correlation_id, undoes=correlation_id,
                     asked=BEFORE_VALUE, before=held, read_back=None, held=None,
                     why="recovery refused: the Case holds neither the applied nor the restore value")
        say("the Case holds a third value; a human decides")
        return 3
    org.set_priority(APPROVED_CASE, BEFORE_VALUE)
    read_back = org.case(APPROVED_CASE)["Priority"]
    demo.receipt(kind="undo", correlation="undo-" + correlation_id, undoes=correlation_id,
                 asked=BEFORE_VALUE, before=held, read_back=read_back, held=read_back == BEFORE_VALUE,
                 why="recovery of the journalled operation")
    return 0 if read_back == BEFORE_VALUE else 1


def run(environ, clock=time.monotonic, sleep=time.sleep) -> int:
    try:
        armed = config(environ)
    except Refused as refusal:
        say(str(refusal))
        return refusal.code
    try:
        store = Store(armed["bucket"])
        org = Org(armed["Headless_domain"], armed["Headless_consumer_key"], armed["Headless_consumer_secret"])
        identity = org.identity()
        if identity["org_id"][:15] != APPROVED_ORG[:15]:
            raise Refused(3, "the credential's org is not the approved org")
        if identity["type"] != "Developer Edition" and not identity["sandbox"]:
            raise Refused(3, "the org is neither a Developer Edition nor a sandbox")
        if armed["recover"]:
            return recover(store, org, armed["recover"])
        case = org.case(APPROVED_CASE)
        if case["Priority"] != BEFORE_VALUE:
            raise Refused(3, "the Case does not hold %s at arming; the approved cycle starts there" % BEFORE_VALUE)
        # The one-shot: this arming is consumed durably BEFORE anything is heard. A platform retry
        # or a second execution bound to the same room finds the object and refuses.
        if not store.create_once("%s/arming-%s.json" % (RECEIPTS, armed["DEMO_ROOM"]),
                                 {"kind": "arming", "room": armed["DEMO_ROOM"], "case_id": APPROVED_CASE,
                                  "org_id": APPROVED_ORG, "minutes": armed["minutes"], "at": now_stamp()}):
            raise Refused(4, "this room's arming was already consumed: a rerun does not re-arm")
    except Refused as refusal:
        say(str(refusal))
        return refusal.code
    deadline = clock() + armed["minutes"] * 60
    demo = Demo(org, store, armed["DEMO_ROOM"], clock=clock, deadline=deadline)
    say("armed %d minutes: Case %s, Priority %s now, room %s"
        % (armed["minutes"], case["CaseNumber"], case["Priority"], armed["DEMO_ROOM"]))
    watched, seen, live_seen = None, 0, False
    code = 0
    try:
        while clock() < deadline:
            if watched is None:
                watched = store.transcript_of(armed["DEMO_ROOM"])
                if watched is None:
                    sleep(POLL_SECONDS)
                    continue
                say("found the room's transcript")
            text = store.read(watched)
            header = front_matter(text)
            if header.get("room") != armed["DEMO_ROOM"]:
                raise Refused(4, "the transcript's own header does not name the bound room")
            # Closed is checked BEFORE any new line: a snapshot that says closed is the record of a
            # finished call, and no command is taken from it (Aya on 6753b44).
            if header.get("state") == "closed":
                if not live_seen:
                    say("that transcript is already closed; nothing to hear")
                    return 4
                say("the call closed")
                break
            if header.get("state") != "live":
                sleep(POLL_SECONDS)
                continue
            lines = owner_lines(text)
            if not live_seen:
                live_seen = True
                seen = len(lines)       # his words so far are history, not requests
                say("live: %d owner lines already there, left alone" % seen)
            for place, at, words in lines[seen:]:
                value = request(words)
                if value is None:
                    continue
                demo.act(value, {"kind": "transcript", "room": armed["DEMO_ROOM"], "line_time": at, "place": place,
                                 "correlation": correlation(armed["DEMO_ROOM"], place, at, words)})
                if demo.restored or demo.undo_unknown:
                    return 1 if demo.held_wrong else 0
            seen = max(seen, len(lines))
            sleep(POLL_SECONDS)
        else:
            say("the arming expired")
    except Refused as refusal:
        say(str(refusal))
        code = refusal.code
    except Exception as error:  # noqa: BLE001 - reported, never retried blind; the finally reconciles
        say("unexpected %s; restoring before exit" % type(error).__name__)
        code = 1
    finally:
        demo.restore("the arming ended")
    if demo.held_wrong:
        return 1
    return code


if __name__ == "__main__":
    sys.exit(run(os.environ))
