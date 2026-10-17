#!/usr/bin/env python3
"""The push half of drive-intake: a Drive changes.watch channel and the resolver behind it.

WHY (the fast-handoff design, Gemini's Eventarc flags 1-3, 2026-10-16)
  Polling every 120 s is most of the time a task spends before dispatch: Gate 3 Step 5 run 4 waited
  71 s from createdTime to REQUESTED. A push gets it to seconds. Gemini's flag 1: Drive on the Workspace
  Events API is still Developer Preview, so the ingress is the GA changes.watch web hook. The poll stays
  as the reconcile path, so a push that never arrives costs one tick, never a task (Gemini's delta:
  hybrid push and reconcile, never push only).

WHAT A PUSH CARRIES, AND WHAT THE RESOLVER DOES WITH IT
  A changes-channel push says "something changed" and nothing else. The resolver reads changes.list
  from the page token stored in Redis, keeps the Google Docs whose parents include the folder, stores
  the new start token, and hands the file ids to decide(). It does NOT use files.list: a doc made a
  second ago may not be in the search index yet, and the changes feed is the one that is ordered.
  Duplicates and replays are harmless by construction: decide()'s claim is SET NX on the request
  revision, so two pushes that read the same token deliver one task (Gemini's flag 3, items 1-3).

THE CHANNEL'S LIFE
  A changes channel lasts at most seven days. /watch/renew (the scheduler, daily, with the poll token)
  opens a new channel when the current one has less than the warning left, records it in Redis, and
  only then stops the old one, so there is no gap. The record keeps the previous channel's id for the
  overlap, and the hook answers only a channel on record. The expiry on record is what /healthz reads.

  Keys (under fleet:, which the ACL grants): fleet:intake:watch (the channel record, JSON) and
  fleet:intake:watch:page (the changes page token). Nothing here names or prints a credential.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import uuid

CHANGES = "https://www.googleapis.com/drive/v3/changes"
STOP = "https://www.googleapis.com/drive/v3/channels/stop"
DOC = "application/vnd.google-apps.document"
WATCH_KEY = "fleet:intake:watch"
PAGE_KEY = "fleet:intake:watch:page"
CHANNEL_SECONDS = 604800        # the changes resource's ceiling; Drive may grant less and says so
KEY_TTL = 8 * 86400             # outlives the channel by a day, so an expired record is still readable
MAX_PAGES = 5
CHANGE_FIELDS = "nextPageToken,newStartPageToken,changes(fileId,removed,file(mimeType,parents,trashed))"


class Unconfigured(Exception):
    pass


def _text(raw):
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")


def drive_reason(raw) -> str:
    """Drive's own reason for a refusal (e.g. push.webhookUrlUnauthorized), never the whole body."""
    try:
        errors = (json.loads(_text(raw)).get("error") or {}).get("errors") or []
        return str((errors[0] or {}).get("reason") or "")[:80]
    except (ValueError, AttributeError, IndexError, TypeError):
        return ""


class DriveWatch:
    def __init__(self, token_provider, fetch, client, now=None):
        self.token_provider, self.fetch, self.client, self.now = token_provider, fetch, client, now

    def _clock(self) -> float:
        return float(self.now) if self.now is not None else time.time()

    def _call(self, method, url, body=None, what="drive"):
        token = self.token_provider()
        if not token:
            raise Unconfigured("no access token")
        headers = {"Authorization": "Bearer " + token}
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body).encode("utf-8")
        status, answer = self.fetch(method, url, raw, headers)
        if status == 204 or (status == 200 and not _text(answer).strip()):
            return {}
        if status != 200:
            raise RuntimeError(("%s answered %s %s" % (what, status, drive_reason(answer))).strip())
        try:
            return json.loads(_text(answer))
        except ValueError as exc:
            raise RuntimeError("unparseable answer from %s" % what) from exc

    # -- the record ------------------------------------------------------------------------------
    def record(self):
        raw = self.client.get(WATCH_KEY)
        if raw is None:
            return None
        try:
            found = json.loads(_text(raw))
        except ValueError:
            return None
        return found if isinstance(found, dict) else None

    def expires_at(self):
        found = self.record() or {}
        try:
            return float(found["expiration"])
        except (KeyError, TypeError, ValueError):
            return None

    def channel_ok(self, channel_id: str) -> bool:
        """The current channel, or the previous one while both are open. Anything else is not ours."""
        found = self.record() or {}
        if not channel_id:
            return False
        return channel_id in (found.get("id"), (found.get("previous") or {}).get("id"))

    def page_token(self):
        raw = self.client.get(PAGE_KEY)
        if raw is None:
            return None
        return _text(raw) or None

    # -- renewal ---------------------------------------------------------------------------------
    def renew(self, address: str, channel_token: str, warn: float, force=False) -> dict:
        if not address.startswith("https://"):
            raise Unconfigured("no https push address (DRIVE_INTAKE_PUSH_URL)")
        if not channel_token:
            raise Unconfigured("no channel token")
        now = self._clock()
        current = self.record()
        left = None
        if current is not None:
            try:
                left = float(current.get("expiration")) - now
            except (TypeError, ValueError):
                left = None
        if not force and left is not None and left > warn:
            return {"status": "current", "seconds_left": int(left)}
        page = self.page_token()
        if not page:
            page = str(self._call("GET", CHANGES + "/startPageToken?supportsAllDrives=true",
                                  what="changes.getStartPageToken").get("startPageToken") or "")
            if not page:
                raise RuntimeError("changes.getStartPageToken gave no token")
            self.client.set(PAGE_KEY, page, ex=KEY_TTL)
        channel_id = "drive-intake-" + uuid.uuid4().hex
        query = urllib.parse.urlencode({"pageToken": page, "supportsAllDrives": "true",
                                        "includeItemsFromAllDrives": "true"})
        answer = self._call("POST", CHANGES + "/watch?" + query, {
            "id": channel_id, "type": "web_hook", "address": address, "token": channel_token,
            "expiration": str(int((now + CHANNEL_SECONDS) * 1000))}, what="changes.watch")
        try:
            expiration = float(answer.get("expiration")) / 1000.0
        except (TypeError, ValueError):
            expiration = now + 3600    # Drive's default when it states none
        fresh = {"id": channel_id, "resource_id": str(answer.get("resourceId") or ""),
                 "expiration": expiration, "created": now}
        if current is not None and current.get("id"):
            fresh["previous"] = {"id": current.get("id"), "resource_id": current.get("resource_id") or ""}
        self.client.set(WATCH_KEY, json.dumps(fresh), ex=KEY_TTL)
        out = {"status": "renewed", "seconds_left": int(expiration - now), "stopped_previous": "none"}
        if "previous" in fresh:
            # Only now, with the new channel on record: no gap. A failed stop lets the old one expire.
            try:
                self._call("POST", STOP, {"id": fresh["previous"]["id"],
                                          "resourceId": fresh["previous"]["resource_id"]}, what="channels.stop")
                out["stopped_previous"] = "stopped"
            except Exception as exc:  # noqa: BLE001
                out["stopped_previous"] = "left_to_expire:" + type(exc).__name__
        return out

    # -- the resolver ----------------------------------------------------------------------------
    def resolve(self, folder_id: str):
        """(hints, next token): the docs in the folder that changed since the stored token, and where the
        feed got to. WRITES NOTHING (Aya on bddafa3): the caller advances the token with advance() only once
        every hint is durable in the pending set, so a crash between the read and the decide loses no push."""
        page = self.page_token()
        if not page:
            raise Unconfigured("no page token on record (renew first)")
        hints, seen, next_token = [], set(), page
        for _ in range(MAX_PAGES):
            query = urllib.parse.urlencode({"pageToken": page, "fields": CHANGE_FIELDS, "pageSize": "100",
                                            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
            answer = self._call("GET", CHANGES + "?" + query, what="changes.list")
            for change in answer.get("changes") or []:
                file_id = str((change or {}).get("fileId") or "")
                meta = (change or {}).get("file") or {}
                if (not file_id or file_id in seen or change.get("removed") or meta.get("trashed")
                        or meta.get("mimeType") != DOC or folder_id not in (meta.get("parents") or [])):
                    continue
                seen.add(file_id)
                hints.append({"file_id": file_id, "folder_id": folder_id})
            if answer.get("newStartPageToken"):
                next_token = str(answer["newStartPageToken"])
                break
            page = str(answer.get("nextPageToken") or "")
            if not page:
                break
            next_token = page
        return hints, next_token

    def advance(self, token: str) -> None:
        """Move the feed's cursor. Called after the hints it covers are pending, never before."""
        if token:
            self.client.set(PAGE_KEY, token, ex=KEY_TTL)
