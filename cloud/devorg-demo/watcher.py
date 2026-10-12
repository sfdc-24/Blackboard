"""One spoken change to ONE Case's Priority in the developer org, heard through the conference's live
transcript, run as a Cloud Run JOB execution - never on a laptop, never in a browser session.

    his words -> the chair's live transcript object (GCS) -> this job -> one PATCH -> read back -> restore

Approved scope (AYA-CASE-00001036-EXACT-OWNER-APPROVAL-20261012; the owner's YES of 2026-10-12
14:35:54Z, and his typed approval in the builder's session at 15:10Z): org 00Dbm00000wK2ibEAC,
Case 500bm000039LHSAAA4 only, field Priority only, Medium to High, verified by read-back, then
restored. "to low" is refused as outside the approval.

ONE EXECUTION IS ONE ARMING (AYA-WATCHER-DENIED-IMPLEMENTATION-HOLD-20261012: bind the exact room)
- The job does nothing unless DEMO_ARMED is exactly "1": without it, the config is named (names,
  never values) and the process exits 0 before any token, bucket or org is touched. Off by default.
- The arming BINDS the exact room: DEMO_ROOM is required, and only that room's transcript is read.
  There is no "whichever transcript appears next" mode.
- One execution makes at most ONE change and ONE restore, then exits. A rerun is a new arming by the
  operator who executes it; nothing re-arms itself.
- The arming expires: DEMO_EXPIRES_MINUTES (1..45) on the monotonic clock. At expiry, at transcript
  close, or on any error after the change, a change not yet restored IS restored before the job ends.

WHAT A LINE MUST BE
- An Owner line of the bound room's transcript, WRITTEN AFTER this execution first saw that
  transcript live (lines already present are counted and skipped: a replayed call triggers nothing).
- One sentence that begins with an instruction verb, names "demo case" and "priority", and ends
  "to high" or "to medium": "Set the demo case priority to high." A question, a sentence holding a
  negation, or two values in one sentence is not a request (a substring cannot carry a negation, so
  a sentence with one is refused whole). The Owner label only says the chair heard the room's owner;
  it authorises this one armed change and nothing wider.
- "to high" makes the change; "to medium" restores; each request is deduplicated by a correlation id
  made of the room, the line's place, its time and its text.

THE ORG, AND THE ONE WRITE
- Identity is the org's existing integration credential (client-credentials connected app), read
  from the environment where the deploy bound the Secret Manager secrets Headless_domain /
  Headless_consumer_key / Headless_consumer_secret. No secret value and no token is ever printed.
- The token host and the answering instance must be a developer or sandbox host
  (develop/sandbox/scratch .my.salesforce.com); the org it answers for must be DEMO_ORG_ID
  (15-character prefix) and a Developer Edition or sandbox - checked by query before any write.
- The one PATCH carries "Sforce-Auto-Assign: FALSE": a bare REST update applies the org's active
  assignment rule by default, and this org's rules reassign and email on escalation. (Checked read
  only 2026-10-12: no workflow rule, record-triggered flow, Apex trigger, validation rule,
  auto-response rule or notification setting acts on a Case Priority update here, and every
  assignment/escalation entry needs the Case's Account - this Case has none.)
- After every write the Case is read back; a read-back that does not hold the asked value is
  reported (receipt held=false, exit 1) and never blindly retried.

RECEIPTS
- One JSON object per event to gs://<TRANSCRIPT_BUCKET>/conference/devorg-demo/ (the shape shared
  with the site's demo page: correlation, kind, field, asked, before, read_back, held, at, undoes,
  source), and one log line each. A receipt that fails to upload is said and skipped: putting the
  value back outranks the paper trail.

Exit: 0 done, or expired unspent; 1 a write's outcome is not the asked value; 2 config refused;
3 token or org refused; 4 transcript refused (wrong room, never live, or already closed).
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
VALUES = ("High", "Medium", "Low")
MAX_MINUTES = 45
POLL_SECONDS = 2.0
ORG_ID = re.compile(r"^00D[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
CASE_ID = re.compile(r"^500[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
ROOM = re.compile(r"^conf-[a-z0-9][a-z0-9-]{0,60}$")
ALLOWED_HOSTS = ("develop.my.salesforce.com", "sandbox.my.salesforce.com", "scratch.my.salesforce.com")
OWNER_LINE = re.compile(r"^- \*\*\[(\d{2}:\d{2}:\d{2})Z\] Owner:\*\* (.*)$")
SENTENCE = re.compile(r"[^.?!]+[.?!]*")
LEAD_IN = r"(?:(?:okay|ok|alright|right|now|so|and|please|yes|yeah|hey|claude|grok|codex|gemini)[,\s]+)*"
INSTRUCTION = re.compile(
    r"^" + LEAD_IN + r"(?:set|change|make|update|move|mark|raise|lower|bump|put|switch|restore)\b"
    r"[^.?!]*\bdemo case\b[^.?!]*\bpriority\b[^.?!]*\bto (high|medium|low)$", re.I)
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
    """The Priority value one Owner line asks for, or None: exactly one asking sentence, or nothing."""
    asked = []
    for sentence in SENTENCE.findall(text):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?"):
            continue
        body = sentence.rstrip(".!").strip()
        if NEGATION.search(body):
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
    """The transcript bucket, with the job's own service-account token: reads, plus receipt writes."""

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
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token()})
        return _http(req, 30)

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

    def receipt(self, entry: dict) -> None:
        """Best effort: a receipt that cannot land is said and skipped; the restore outranks it."""
        name = "%s/%s-%s.json" % (RECEIPTS, entry["at"].replace(":", ""), entry["correlation"])
        url = ("https://storage.googleapis.com/upload/storage/v1/b/%s/o?uploadType=media&name=%s"
               % (self.bucket, urllib.parse.quote(name, safe="")))
        req = urllib.request.Request(url, data=json.dumps(entry, sort_keys=True).encode("utf-8"), method="POST",
                                     headers={"Authorization": "Bearer " + self.token(),
                                              "Content-Type": "application/json"})
        try:
            status, _ = _http(req, 15)
        except Exception as error:  # noqa: BLE001 - the restore must not die for a receipt
            status = -1
            say("receipt not written (%s)" % type(error).__name__)
        if status != 200:
            say("receipt answered HTTP %d" % status)


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
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.instance + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in headers:
            req.add_header(name, value)
        status, body = _http(req, 30)
        return status, (json.loads(body.decode("utf-8")) if body.strip() else None)

    def query(self, soql: str) -> list:
        status, answer = self._call("GET", "/services/data/%s/query?q=%s" % (API, urllib.parse.quote(soql)))
        if status != 200:
            raise Refused(3, "a query answered HTTP %d" % status)
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
        if value not in VALUES:
            raise Refused(3, "not one of this field's three values")
        # Sforce-Auto-Assign FALSE: a bare REST update applies the org's active assignment rule.
        status, answer = self._call("PATCH", "/services/data/%s/sobjects/Case/%s" % (API, case_id),
                                    {"Priority": value}, headers=(("Sforce-Auto-Assign", "FALSE"),))
        if status != 204:
            raise Refused(3, "the update answered HTTP %d: %s" % (status, str(answer)[:120]))


# ---------------------------------------------------------------- the arming and the run

def config(environ) -> dict:
    """The arming, from the execution's environment. Refuses by NAME; no value is ever printed."""
    if environ.get("DEMO_ARMED") != "1":
        raise Refused(0, "not armed (DEMO_ARMED is not 1): nothing read, nothing written")
    out = {}
    for name, shape in (("DEMO_ORG_ID", ORG_ID), ("DEMO_CASE_ID", CASE_ID), ("DEMO_ROOM", ROOM)):
        value = environ.get(name, "")
        if not shape.match(value):
            raise Refused(2, "%s is missing or malformed" % name)
        out[name] = value
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
    """At most one change and one restore, whatever happens around them."""

    def __init__(self, org: Org, store: Store, case_id: str, before: str):
        self.org, self.store = org, store
        self.case_id, self.before = case_id, before
        self.changed = None             # the correlation of the one change
        self.restored = False
        self.held_wrong = False
        self.handled = set()            # correlations already answered, whatever they asked

    def receipt(self, **entry) -> dict:
        entry.setdefault("at", now_stamp())
        entry.setdefault("field", "Priority")
        self.store.receipt(entry)
        say("%s %s: asked %s, before %s, org holds %s" % (entry["kind"], entry["correlation"],
            entry.get("asked"), entry.get("before"), entry.get("read_back")))
        return entry

    def act(self, value: str, source: dict) -> None:
        if source["correlation"] in self.handled:
            return                      # the same line is never run twice
        self.handled.add(source["correlation"])
        if value == "High" and self.changed is None:
            held = self.org.case(self.case_id)["Priority"]
            self.org.set_priority(self.case_id, "High")
            read_back = self.org.case(self.case_id)["Priority"]
            self.changed = source["correlation"]
            self.receipt(kind="change", correlation=source["correlation"], source=source,
                         asked="High", before=held, read_back=read_back, held=read_back == "High")
            if read_back != "High":
                self.held_wrong = True
        elif value == "Medium" and self.changed and not self.restored:
            self.restore("the spoken word")
        else:
            self.receipt(kind="refused", correlation=source["correlation"], source=source, asked=value,
                         before=None, read_back=None, held=None,
                         why="out of the approved scope or out of order")

    def restore(self, why: str) -> None:
        if not self.changed or self.restored:
            return
        held = self.org.case(self.case_id)["Priority"]
        if held != self.before:
            self.org.set_priority(self.case_id, self.before)
        read_back = self.org.case(self.case_id)["Priority"]
        self.restored = True
        self.receipt(kind="undo", correlation="undo-" + self.changed, undoes=self.changed,
                     asked=self.before, before=held, read_back=read_back,
                     held=read_back == self.before, why=why)
        if read_back != self.before:
            self.held_wrong = True


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
        if identity["org_id"][:15] != armed["DEMO_ORG_ID"][:15]:
            raise Refused(3, "the credential's org is not the armed org")
        if identity["type"] != "Developer Edition" and not identity["sandbox"]:
            raise Refused(3, "the org is neither a Developer Edition nor a sandbox")
        case = org.case(armed["DEMO_CASE_ID"])
        if case["Priority"] not in VALUES:
            raise Refused(3, "the Case's Priority holds a value outside High, Medium, Low; not touching it")
    except Refused as refusal:
        say(str(refusal))
        return refusal.code
    deadline = clock() + armed["minutes"] * 60
    demo = Demo(org, store, armed["DEMO_CASE_ID"], case["Priority"])
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
            lines = owner_lines(text)
            if not live_seen:
                if header.get("state") == "closed":
                    say("that transcript is already closed; nothing to hear")
                    return 4
                if header.get("state") != "live":
                    sleep(POLL_SECONDS)
                    continue
                live_seen = True
                seen = len(lines)       # his words so far are history, not requests
                say("live: %d owner lines already there, left alone" % seen)
            for place, at, words in lines[seen:]:
                value = request(words)
                if value is None:
                    continue
                demo.act(value, {"kind": "transcript", "room": armed["DEMO_ROOM"], "line_time": at, "place": place,
                                 "correlation": correlation(armed["DEMO_ROOM"], place, at, words)})
                if demo.restored:
                    return 1 if demo.held_wrong else 0
            seen = max(seen, len(lines))
            if header.get("state") == "closed":
                say("the call closed")
                break
            sleep(POLL_SECONDS)
        else:
            say("the arming expired")
    except Refused as refusal:
        say(str(refusal))
        code = refusal.code
    finally:
        demo.restore("the arming ended")
    if demo.held_wrong:
        return 1
    return code


if __name__ == "__main__":
    sys.exit(run(os.environ))
