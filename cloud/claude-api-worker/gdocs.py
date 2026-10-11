#!/usr/bin/env python3
"""Google Docs, the way both drive-intake and the worker need it: read, guarded append, read-back.

Three calls and nothing else:
  documents.get            -> the text, the revisionId, and (through Drive) the parent folder
  documents.batchUpdate    -> ONE insertText at the end of the body, guarded by
                              writeControl.requiredRevisionId so a stale writer is refused
                              instead of landing on top of someone else's line
  has(file_id, receipt_id) -> the literal "id <receipt_id>" read back from the doc

The access token comes from the metadata server (the service identity the
owner grants), never from a file in this repository. An adapter with no token
raises Unconfigured; it never reads or writes as nobody.

Verified offline only (the request and response shapes). The live calls need
the grants named in the pull request; the first real round trip is the
acceptance gate, not this file.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

DOCS = "https://docs.googleapis.com/v1/documents/"
DRIVE = "https://www.googleapis.com/drive/v3/files/"
METADATA = ("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
            "?scopes=https://www.googleapis.com/auth/documents,https://www.googleapis.com/auth/drive.readonly")
TIMEOUT = 30


class Unconfigured(Exception):
    pass


class RevisionConflict(Exception):
    """The doc moved since it was read. Re-read, then write once more with the revision it has now."""


def extract_text(body: dict) -> str:
    """Every textRun in body order, tables included. Docs ends every paragraph with a newline."""
    parts = []

    def walk(elements):
        for element in elements or []:
            paragraph = element.get("paragraph")
            if paragraph:
                for piece in paragraph.get("elements") or []:
                    run = piece.get("textRun")
                    if run and run.get("content"):
                        parts.append(run["content"])
            table = element.get("table")
            if table:
                for row in table.get("tableRows") or []:
                    for cell in row.get("tableCells") or []:
                        walk(cell.get("content"))
            toc = element.get("tableOfContents")
            if toc:
                walk(toc.get("content"))

    walk((body or {}).get("content"))
    return "".join(parts)


def append_request(block: str, expected_revision: str) -> dict:
    """The batchUpdate body. The text starts on its own line: end-of-segment is before the final newline."""
    text = block if block.startswith("\n") else "\n" + block
    request = {"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]}
    if expected_revision:
        request["writeControl"] = {"requiredRevisionId": expected_revision}
    return request


def metadata_token(fetch=None) -> str:
    fetch = fetch or _fetch
    status, raw = fetch("GET", METADATA, None, {"Metadata-Flavor": "Google"})
    if status != 200:
        raise Unconfigured("metadata server answered %s" % status)
    try:
        token = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw).get("access_token") or ""
    except ValueError:
        token = ""
    if not token:
        raise Unconfigured("metadata server gave no access token")
    return token


class GoogleDocs:
    """read(file_id) / append(file_id, expected_revision, receipt_id, block) / has(file_id, receipt_id)."""

    def __init__(self, token_provider=None, fetch=None, with_folder=True):
        self.token_provider = token_provider or metadata_token
        self.fetch = fetch or _fetch
        self.with_folder = with_folder

    def _call(self, method, url, body=None):
        token = self.token_provider()
        if not token:
            raise Unconfigured("no access token")
        headers = {"Authorization": "Bearer " + token}
        raw = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body).encode("utf-8")
        status, answer = self.fetch(method, url, raw, headers)
        if status == 400 and body is not None and b"revision" in (answer or b"").lower():
            raise RevisionConflict("the doc moved: %s" % (answer or b"")[:200].decode("utf-8", "replace"))
        if status != 200:
            raise RuntimeError("%s %s answered %s" % (method, url.split("?")[0].rsplit("/", 1)[-1], status))
        try:
            return json.loads(answer.decode("utf-8") if isinstance(answer, bytes) else answer)
        except ValueError as exc:
            raise RuntimeError("unparseable answer from Docs") from exc

    def read(self, file_id: str) -> dict:
        doc = self._call("GET", DOCS + urllib.parse.quote(file_id) + "?fields=revisionId,body")
        out = {"text": extract_text(doc.get("body") or {}), "revision_id": str(doc.get("revisionId") or ""),
               "folder_id": ""}
        if self.with_folder:
            meta = self._call("GET", DRIVE + urllib.parse.quote(file_id) + "?fields=parents")
            parents = meta.get("parents") or []
            out["folder_id"] = str(parents[0]) if parents else ""
        return out

    def append(self, file_id: str, expected_revision: str, receipt_id: str, block: str) -> str:
        answer = self._call("POST", DOCS + urllib.parse.quote(file_id) + ":batchUpdate",
                            append_request(block, expected_revision))
        return str((answer.get("writeControl") or {}).get("requiredRevisionId") or "")

    def has(self, file_id: str, receipt_id: str) -> bool:
        return ("id " + receipt_id) in self.read(file_id)["text"]


def _fetch(method, url, body, headers):  # pragma: no cover - the one socket in this file
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""
