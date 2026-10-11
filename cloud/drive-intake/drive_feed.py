#!/usr/bin/env python3
"""The changes feed for drive-intake, by polling: the docs in the folder modified since the last poll.

WHY POLLING FIRST
  Aya's addendum to Converspan Channel Architecture v0.1: "Polling remains an
  acceptable first implementation if it preserves the same contract and avoids
  duplicate work." A Drive watch channel expires in seven days and needs a
  renewal job; a poll on a Cloud Scheduler tick needs neither, and the push
  endpoint (/hooks/drive) stays for when a watch is registered. Duplicate work
  is already impossible downstream: every hint goes through decide(), whose
  identity is the request revision and whose claim is SET NX.

WHAT ONE POLL ASKS
  files.list in the approved folder: Google Docs only, not trashed, modified
  after the last successful poll less a margin (a doc edited while the
  previous poll ran is seen by the next one). No last poll on record means a
  bounded look back, not "everything ever". A poll that fails records
  nothing, so the next one looks back over the same span.

  The token comes from the metadata server (the service identity the owner
  grants on the folder), the way cloud/claude-api-worker/gdocs.py does it.
  Verified offline only; the first live poll is the acceptance gate's job.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

FILES = "https://www.googleapis.com/drive/v3/files"
DOC = "application/vnd.google-apps.document"
DEFAULT_LOOKBACK = 86400     # with no poll on record, one day back
DEFAULT_MARGIN = 600         # ten minutes behind the last poll, so an edit mid-poll is not missed
PAGE = 100


class Unconfigured(Exception):
    pass


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_for(folder_id: str, since_epoch: float) -> str:
    """The Drive query: this folder, Docs only, not trashed, modified after `since`."""
    if not folder_id:
        raise Unconfigured("no folder to poll (DRIVE_INTAKE_FOLDER_ID)")
    return ("'%s' in parents and mimeType = '%s' and trashed = false and modifiedTime > '%s'"
            % (folder_id, DOC, iso(since_epoch)))


def since_for(last_poll, now: float, lookback=DEFAULT_LOOKBACK, margin=DEFAULT_MARGIN) -> float:
    """Where this poll looks back to: the last poll less the margin, else the bounded look back."""
    if last_poll is None:
        return max(0.0, float(now) - lookback)
    try:
        return max(0.0, min(float(last_poll) - margin, float(now)))
    except (TypeError, ValueError):
        return max(0.0, float(now) - lookback)


def hints_from(files: list, folder_id: str) -> list:
    """The feed's answer as decide() hints: file id and the folder it says it is in."""
    out = []
    for item in files or []:
        file_id = str((item or {}).get("id") or "")
        if not file_id:
            continue
        parents = (item or {}).get("parents") or []
        parent = str(parents[0]) if parents else folder_id
        out.append({"file_id": file_id, "folder_id": parent})
    return out


def http_post(url, body, headers):  # pragma: no cover - the ring's one socket, kept out of main.py
    """POST and return the raw answer; the caller reads the readout. Used by WakeRing in main()."""
    import urllib.request  # noqa: PLC0415
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


class DriveFolder:
    """changes(folder_id) -> [{file_id, folder_id}], and the poll bookkeeping in the store given."""

    def __init__(self, token_provider, fetch, store, now=None, lookback=DEFAULT_LOOKBACK, margin=DEFAULT_MARGIN):
        self.token_provider, self.fetch, self.store = token_provider, fetch, store
        self.now, self.lookback, self.margin = now, lookback, margin
        self.last_query = ""

    def _list(self, query: str, page_token: str = "") -> dict:
        token = self.token_provider()
        if not token:
            raise Unconfigured("no access token")
        params = {"q": query, "fields": "nextPageToken,files(id,parents,modifiedTime)",
                  "orderBy": "modifiedTime desc", "pageSize": str(PAGE),
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            params["pageToken"] = page_token
        status, raw = self.fetch("GET", FILES + "?" + urllib.parse.urlencode(params), None,
                                 {"Authorization": "Bearer " + token})
        if status != 200:
            raise RuntimeError("files.list answered %s" % status)
        try:
            return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except ValueError as exc:
            raise RuntimeError("unparseable answer from Drive") from exc

    def changes(self, folder_id: str) -> list:
        import time  # noqa: PLC0415
        now = float(self.now) if self.now is not None else time.time()
        since = since_for(self.store.last_poll(), now, self.lookback, self.margin)
        query = query_for(folder_id, since)
        self.last_query = query
        files, page_token = [], ""
        for _page in range(10):                       # a thousand changed docs in one poll is not a poll
            answer = self._list(query, page_token)
            files.extend(answer.get("files") or [])
            page_token = answer.get("nextPageToken") or ""
            if not page_token:
                break
        self.store.note_poll(now)                     # recorded only after the listing succeeded
        return hints_from(files, folder_id)
