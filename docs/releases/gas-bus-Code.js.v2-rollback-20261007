/**
 * SFDC24 BLACKBOARD BUS — Apps Script append/read endpoint
 * ============================================================
 *
 * Bound to abdus@sfdc24.com's Google account. Deploy as a Web App
 * (Execute as: Me · Who has access: Anyone). Runs on Google's own
 * servers, so nothing local has to stay awake for other instances
 * to use it.
 *
 * WHY THIS EXISTS
 * The Drive API every Claude instance uses (create/read/update/trash)
 * cannot edit a Doc or Sheet body in place — every revision today is
 * a create-replacement-verify-trash "swap" that mints a new file ID.
 * Apps Script's own DocumentApp / SpreadsheetApp services do NOT have
 * that limitation — they edit the live file directly. This script is
 * a thin, secret-gated HTTP wrapper around that native access, plus a
 * real server-side lock (LockService), so an append is one call and
 * one atomic operation instead of four calls and a new ID.
 *
 * THE ONLY GATE is a shared secret string every caller must send.
 * "Who has access" is set to Anyone specifically so instances with
 * no Google OAuth session (a bare HTTP client) can still call it —
 * that means the secret IS the entire security boundary. Treat it
 * like a password: long, random, never hardcoded below, never
 * pasted anywhere public.
 *
 * ------------------------------------------------------------
 * ONE-TIME SETUP (Project Settings ⚙ > Script Properties):
 *   FOLDER_ID   the Drive folder ID of "SFDC 24 - Claude"
 *   BUS_SECRET  a long random string — every caller must send it
 * ------------------------------------------------------------
 *
 * API
 * ---
 * GET  ?                                  -> health check, no secret needed
 * GET/POST {action:'time', secret}        -> current server timestamp
 * POST      {action:'append', secret, title, text, ...}
 * GET/POST {action:'read', secret, title} -> current file content
 *              sheets only, all optional and combinable:
 *              limit  N     the last N rows (a range read, not the whole sheet)
 *              match  text  only rows containing this text, case-insensitive
 *              since  ISO   only rows whose timestamp is at or after this
 *              -> adds total and filtered to the response so a narrow answer
 *                 can never be mistaken for an empty board
 *
 * See each handler below for full parameter docs.
 */

// ---- CONFIG --------------------------------------------------------

function getConfig_() {
  const props = PropertiesService.getScriptProperties();
  const folderId = props.getProperty('FOLDER_ID');
  const secret = props.getProperty('BUS_SECRET');
  if (!folderId || !secret) {
    throw new Error(
      'Script Properties FOLDER_ID and BUS_SECRET must be set first ' +
      '(Project Settings > Script Properties in the Apps Script editor).'
    );
  }
  return { folderId: folderId, secret: secret };
}

// ---- ENTRY POINTS ----------------------------------------------------

function doGet(e) {
  const params = (e && e.parameter) || {};
  const action = params.action || 'ping';

  if (action === 'ping') {
    return jsonOut_({ ok: true, service: 'sfdc24-blackboard-bus', time: nowStamp_() });
  }

  const cfg = getConfig_();
  if (params.secret !== cfg.secret) {
    return jsonOut_({ ok: false, error: 'Bad or missing secret.' }, 401);
  }

  try {
    if (action === 'time') return jsonOut_({ ok: true, time: nowStamp_(), iso: new Date().toISOString() });
    if (action === 'read') return handleRead_(params, cfg);
    return jsonOut_({ ok: false, error: 'Unknown action: ' + action }, 400);
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err) }, 500);
  }
}

function doPost(e) {
  let body;
  try {
    body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return jsonOut_({ ok: false, error: 'Request body must be JSON.' }, 400);
  }

  const cfg = getConfig_();
  if (body.secret !== cfg.secret) {
    return jsonOut_({ ok: false, error: 'Bad or missing secret.' }, 401);
  }

  const action = body.action || 'append';
  try {
    if (action === 'append') return handleAppend_(body, cfg);
    if (action === 'read') return handleRead_(body, cfg);
    if (action === 'time') return jsonOut_({ ok: true, time: nowStamp_(), iso: new Date().toISOString() });
    return jsonOut_({ ok: false, error: 'Unknown action: ' + action }, 400);
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err) }, 500);
  }
}

// ---- APPEND — the core operation ------------------------------------
//
// POST body:
//   secret       required, must match BUS_SECRET
//   title        required — exact file title inside FOLDER_ID
//   text         for a Doc: the line(s) to append (split on \n, one
//                paragraph each). For a Sheet with no sheetRow given,
//                appended as a single-cell row.
//   sheetRow     optional array — if the target is a Sheet, appendRow()
//                this exactly instead of wrapping `text`.
//   sheetName    optional — a specific tab name; default is the first
//                sheet.
//   separator    optional bool, default true for Docs — appends one
//                blank paragraph before the new text so entries stay
//                visually separated, matching this folder's convention.
//
// This endpoint does NOT impose the four-line DATE/SOURCE/ACTION/FILES
// stamp format itself — that convention belongs to the callers (see
// SFDC24 — READ ME FIRST) and may evolve. Call action:'time' first (or
// use your own tool-fetched clock) to build your stamp, then send the
// fully-formed block as `text`. This keeps the endpoint a stable
// primitive instead of hard-coding a protocol detail that changes.

function handleAppend_(body, cfg) {
  if (!body.title) return jsonOut_({ ok: false, error: 'title is required.' }, 400);

  const lock = LockService.getScriptLock();
  const gotLock = lock.tryLock(20000);
  if (!gotLock) {
    return jsonOut_({ ok: false, error: 'Could not acquire the lock within 20s — another append is in progress. Retry.' }, 423);
  }

  try {
    const file = resolveFile_(body, cfg.folderId);
    const mime = file.getMimeType();

    if (mime === MimeType.GOOGLE_DOCS) {
      const doc = DocumentApp.openById(file.getId());
      const docBody = doc.getBody();
      if (body.separator !== false) docBody.appendParagraph('');
      String(body.text || '').split('\n').forEach(function (line) {
        docBody.appendParagraph(line);
      });
      doc.saveAndClose();
    } else if (mime === MimeType.GOOGLE_SHEETS) {
      const ss = SpreadsheetApp.openById(file.getId());
      const sheet = body.sheetName ? ss.getSheetByName(body.sheetName) : ss.getSheets()[0];
      if (!sheet) return jsonOut_({ ok: false, error: 'Sheet tab not found: ' + body.sheetName }, 400);
      const row = body.sheetRow || [nowStamp_(), body.text || ''];
      sheet.appendRow(row);
    } else {
      return jsonOut_({ ok: false, error: 'Unsupported file type for append: ' + mime }, 400);
    }

    return jsonOut_({ ok: true, fileId: file.getId(), title: file.getName(), appendedAt: nowStamp_() });
  } finally {
    lock.releaseLock();
  }
}

// ---- READ -----------------------------------------------------------
//
// params: secret, title (or fileId)
//         optional, sheets only: limit, match, since, sheetName
//
// Returns the Doc's full text, or rows of the first (or named) Sheet tab.
// For instances with no Drive connector of their own.
//
// WHY THE FILTERS EXIST, ADDED 2026-09-18
//   Every read of the operational board returned the whole sheet: measured the
//   same day at 2,831 rows and 4,297,598 bytes. Agents coordinate by posting a
//   row and reading it back, so one sentence between two agents cost a POST
//   plus four megabytes plus a second four megabytes to verify the row landed.
//   Mr Salam's words: "the board is taking multiple round trips and wasting
//   tokens".
//
//   `limit` also reads a RANGE rather than the whole sheet, so the script stops
//   paying for the rows it is about to throw away - that matters more as the
//   board grows, because getDataRange() on a sheet this size is the part that
//   will eventually meet the six-minute execution limit.
//
// BACKWARDS COMPATIBLE ON PURPOSE
//   No parameters means exactly the old behaviour, byte for byte. Five clients
//   on three machines call this endpoint and none of them may break today.
//
// WHAT A FILTERED RESPONSE SAYS ABOUT ITSELF
//   It carries `total` (rows in the sheet) and `filtered` (rows returned), so a
//   caller can never read a narrow answer as an empty board. That distinction
//   has already cost this project a false data-loss escalation.

function handleRead_(params, cfg) {
  if (!params.title && !params.fileId) return jsonOut_({ ok: false, error: 'title or fileId is required.' }, 400);

  const file = resolveFile_(params, cfg.folderId);
  const mime = file.getMimeType();

  if (mime === MimeType.GOOGLE_DOCS) {
    const text = DocumentApp.openById(file.getId()).getBody().getText();
    return jsonOut_({ ok: true, fileId: file.getId(), title: file.getName(), text: text });
  } else if (mime === MimeType.GOOGLE_SHEETS) {
    const ss = SpreadsheetApp.openById(file.getId());
    const sheet = params.sheetName ? ss.getSheetByName(params.sheetName) : ss.getSheets()[0];
    if (!sheet) return jsonOut_({ ok: false, error: 'Sheet tab not found: ' + params.sheetName }, 400);

    const total = sheet.getLastRow();
    const cols = sheet.getLastColumn();
    const limit = parseInt(params.limit, 10);
    const match = String(params.match || '').toLowerCase();
    const since = String(params.since || '');
    const filtered = (limit > 0) || !!match || !!since;

    if (!filtered) {
      // The old path, untouched.
      return jsonOut_({ ok: true, fileId: file.getId(), title: file.getName(),
                        rows: sheet.getDataRange().getValues() });
    }

    // A tail read when a limit is given, so the rows nobody asked for are never
    // loaded. With match or since and no limit, the whole sheet is still read -
    // there is no way to find a substring without looking at it - but only the
    // matching rows travel, which is where the four megabytes went.
    let rows;
    if (limit > 0 && !match && !since) {
      const n = Math.min(limit, total);
      rows = total > 0 ? sheet.getRange(Math.max(1, total - n + 1), 1, n, cols).getValues() : [];
    } else {
      rows = sheet.getDataRange().getValues();
      if (since) {
        rows = rows.filter(function (r) {
          for (let i = 0; i < r.length; i++) {
            const cell = r[i];
            if (cell instanceof Date) return cell.toISOString() >= since;
            if (typeof cell === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(cell)) return cell >= since;
          }
          return false;   // a row with no timestamp cannot satisfy a since
        });
      }
      if (match) {
        rows = rows.filter(function (r) {
          return r.join(' ').toLowerCase().indexOf(match) !== -1;
        });
      }
      if (limit > 0 && rows.length > limit) rows = rows.slice(rows.length - limit);
    }

    return jsonOut_({ ok: true, fileId: file.getId(), title: file.getName(),
                      rows: rows, total: total, filtered: rows.length });
  }
  return jsonOut_({ ok: false, error: 'Unsupported file type for read: ' + mime }, 400);
}

// ---- HELPERS ----------------------------------------------------------

// Resolves a target strictly within FOLDER_ID — the endpoint's whole
// trust boundary is "this one Drive folder", never the caller's entire
// Drive. Accepts either an exact `title` (the folder's own addressing
// contract — errors loudly on 0 or 2+ matches, never guesses) or a
// `fileId` (verified to actually live in the folder before use).
function resolveFile_(params, folderId) {
  const folder = DriveApp.getFolderById(folderId);

  if (params.fileId) {
    const file = DriveApp.getFileById(params.fileId);
    const parents = file.getParents();
    let inFolder = false;
    while (parents.hasNext()) {
      if (parents.next().getId() === folderId) { inFolder = true; break; }
    }
    if (!inFolder) throw new Error('fileId ' + params.fileId + ' is not inside the configured folder.');
    return file;
  }

  const it = folder.getFilesByName(params.title);
  const matches = [];
  while (it.hasNext()) matches.push(it.next());
  if (matches.length === 0) throw new Error('No file titled "' + params.title + '" in the folder.');
  if (matches.length > 1) {
    throw new Error(
      'AMBIGUOUS: ' + matches.length + ' files titled "' + params.title + '". IDs: ' +
      matches.map(function (f) { return f.getId(); }).join(', ')
    );
  }
  return matches[0];
}

function nowStamp_() {
  return Utilities.formatDate(new Date(), 'America/New_York', "EEEE, MMMM d, yyyy 'at' h:mm a 'EDT'");
}

// Apps Script web apps cannot set a real HTTP transport status code —
// the connection always reports 200. The intended status is echoed as
// `_httpStatus` in the JSON body; callers should check `ok` and
// `_httpStatus`/`error`, never the transport status code.
function jsonOut_(obj, code) {
  obj._httpStatus = code || 200;
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
