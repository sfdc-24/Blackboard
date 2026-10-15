#!/usr/bin/env python3
"""The changes feed for drive-intake, by polling: the docs in the folder modified since the watermark.

WHY POLLING FIRST
  Aya's addendum to Converspan Channel Architecture v0.1: "Polling remains an
  acceptable first implementation if it preserves the same contract and avoids
  duplicate work." A Drive watch channel expires in seven days and needs a
  renewal job; a poll on a Cloud Scheduler tick needs neither, and the push
  endpoint (/hooks/drive) stays for when a watch is registered. Duplicate work
  is already impossible downstream: every hint goes through decide(), whose
  identity is the request revision and whose claim is SET NX.

WHAT ONE POLL ASKS, AND WHAT IT NEVER WRITES
  files.list in the approved folder: Google Docs only, not trashed, modified
  after the WATERMARK less a margin, OLDEST FIRST, when the previous listing
  finished. The watermark is where the last poll got to, not when it ran. A
  listing that still has pages after the cap is incomplete, and its watermark
  is the last doc it returned: the next poll resumes THERE, with no margin.
  Subtracting the margin would re-read the same thousand docs and the cursor
  would never move (Cursor on 0bee083). No watermark on record means a
  bounded look back, not "everything ever".

  This module writes NOTHING to the store. Cursor on 0bee083: the feed used to
  record the poll as soon as the listing returned, before a single hint was
  decided, so a doc queued for the laptop fell out of the window and was
  never delivered when the laptop came back. The one writer is poll() in
  main.py, after every hint is decided and the unfinished ones are kept.

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
DEFAULT_LOOKBACK = 86400     # with no watermark on record, one day back
DEFAULT_MARGIN = 600         # ten minutes behind the watermark, so an edit mid-poll is not missed
PAGE = 100
MAX_PAGES = 10               # a thousand changed docs in one poll is a poll; past that, the next continues


class Unconfigured(Exception):
    pass


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_of(rfc3339) -> float | None:
    """Drive's modifiedTime ('2025-10-09T08:53:20.123Z') as an epoch, or None when it is not one."""
    text = str(rfc3339 or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def query_for(folder_id: str, since_epoch: float) -> str:
    """The Drive query: this folder, Docs only, not trashed, modified after `since`."""
    if not folder_id:
        raise Unconfigured("no folder to poll (DRIVE_INTAKE_FOLDER_ID)")
    return ("'%s' in parents and mimeType = '%s' and trashed = false and modifiedTime > '%s'"
            % (folder_id, DOC, iso(since_epoch)))


def since_for(watermark, now: float, lookback=DEFAULT_LOOKBACK, margin=DEFAULT_MARGIN) -> float:
    """Where this poll looks back to: the watermark less the margin, else the bounded look back.
    Never ahead of now and never before the epoch."""
    if watermark is None:
        return max(0.0, float(now) - lookback)
    try:
        return max(0.0, min(float(watermark) - margin, float(now)))
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
    """changes(folder_id) -> {"hints": [...], "watermark": epoch, "complete": bool}. Writes nothing."""

    def __init__(self, token_provider, fetch, store, now=None, lookback=DEFAULT_LOOKBACK, margin=DEFAULT_MARGIN,
                 max_pages=MAX_PAGES):
        self.token_provider, self.fetch, self.store = token_provider, fetch, store
        self.now, self.lookback, self.margin, self.max_pages = now, lookback, margin, max_pages
        self.last_query = ""

    def _list(self, query: str, page_token: str = "") -> dict:
        token = self.token_provider()
        if not token:
            raise Unconfigured("no access token")
        params = {"q": query, "fields": "nextPageToken,files(id,parents,modifiedTime)",
                  "orderBy": "modifiedTime", "pageSize": str(PAGE),
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

    def changes(self, folder_id: str) -> dict:
        import time  # noqa: PLC0415
        now = float(self.now) if self.now is not None else time.time()
        # A capped listing is resumed at its last doc. The margin is only for a listing that
        # finished: an edit that landed while that poll ran is still inside the next window.
        resume = getattr(self.store, "resume", None)
        mark = self.store.watermark()
        if callable(resume) and resume() and mark is not None:
            try:
                since = max(0.0, min(float(mark), now))
            except (TypeError, ValueError):
                since = since_for(mark, now, self.lookback, self.margin)
        else:
            since = since_for(mark, now, self.lookback, self.margin)
        # The baseline (main.poll's first poll): nothing modified before it is listed, whatever the
        # watermark or the look back says.
        floor = getattr(self.store, "floor", None)
        low = floor() if callable(floor) else None
        if low is not None:
            since = max(since, min(float(low), now))
        query = query_for(folder_id, since)
        self.last_query = query
        files, page_token, complete = [], "", True
        for _page in range(self.max_pages):
            answer = self._list(query, page_token)
            files.extend(answer.get("files") or [])
            page_token = answer.get("nextPageToken") or ""
            if not page_token:
                break
        else:
            complete = not page_token
        # Complete: the watermark is now. Incomplete: the last doc returned (oldest first), so the
        # next poll continues from there and the docs past the cap are not past the cursor.
        watermark = now
        if not complete:
            last = epoch_of((files[-1] or {}).get("modifiedTime")) if files else None
            watermark = last if last is not None else since
        return {"hints": hints_from(files, folder_id), "watermark": watermark, "complete": complete}
