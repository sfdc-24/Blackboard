/**
 * SFDC24 Blackboard Bus — `upload` action (binary files into a Drive folder)
 * claude-code-cli, 2026-09-02. For wf=GLASSES-INTAKE sub=WEBCAM-CAPTURE.
 *
 * WHY THIS IS NEEDED
 *   The v1 bus implements ping/time/read/append only — all text. The Glasses
 *   Intake feeder produces JPEGs, and this laptop has no Google Drive for
 *   Desktop sync root, so a scheduled job currently has NO path into Drive.
 *   This action closes that gap WITHOUT minting a new credential: it reuses
 *   the BUS_SECRET gate the fleet already holds, so nothing new to rotate,
 *   store, or leak (D-18).
 *
 * HOW TO DEPLOY (per DEPLOY.md §5 — editing alone does NOT update the live URL)
 *   1. Paste handleUpload_ below into Code.gs.
 *   2. In doPost, beside the existing append/read/time branches, add:
 *          if (action === 'upload') return handleUpload_(body, cfg);
 *   3. Deploy → Manage deployments → edit the EXISTING deployment → New
 *      version → Deploy. Same URL; a "New deployment" would mint a new one.
 *
 * CONTRACT
 *   POST { action:'upload', secret, folderId, filename, mimeType, base64 }
 *   ->   { ok:true, fileId, name, bytes, folderId, uploadedAt }
 *   Errors are explicit and fail closed. An upload that writes nothing must
 *   never answer ok:true — that is REQ-V8QD7R, and it is the defect this
 *   codebase has paid for twice.
 */

function handleUpload_(body, cfg) {
  // --- validate before touching Drive; reject, never default (REQ-T7PKM3) ---
  var folderId = body.folderId || cfg.FOLDER_ID;
  if (!folderId) {
    return jsonOut_({ ok: false, error: 'folderId missing and no FOLDER_ID script property' }, 400);
  }
  var filename = String(body.filename || '').trim();
  if (!filename) {
    return jsonOut_({ ok: false, error: 'filename required' }, 400);
  }
  // Drive has no directories inside a folder; a path separator here means the
  // caller is confused about the destination. Refuse rather than flatten it.
  if (filename.indexOf('/') !== -1 || filename.indexOf('\\') !== -1) {
    return jsonOut_({ ok: false, error: 'filename must not contain a path separator' }, 400);
  }
  var b64 = body.base64;
  if (typeof b64 !== 'string' || !b64) {
    return jsonOut_({ ok: false, error: 'base64 content required (non-empty string)' }, 400);
  }
  var mimeType = String(body.mimeType || 'application/octet-stream');

  var bytes;
  try {
    bytes = Utilities.base64Decode(b64);
  } catch (err) {
    return jsonOut_({ ok: false, error: 'base64 did not decode: ' + err }, 400);
  }
  if (!bytes || !bytes.length) {
    return jsonOut_({ ok: false, error: 'decoded payload is empty — refusing to create a 0-byte file' }, 400);
  }

  var folder;
  try {
    folder = DriveApp.getFolderById(folderId);
  } catch (err) {
    return jsonOut_({ ok: false, error: 'folder not found or not accessible: ' + folderId }, 404);
  }

  var blob = Utilities.newBlob(bytes, mimeType, filename);
  var file = folder.createFile(blob);

  // Echo what was ACTUALLY written, not what was asked for — a caller must be
  // able to catch a shape mismatch without a second round trip. (The missing
  // echo is item 5 of the long-standing v1 Code.gs backlog.)
  return jsonOut_({
    ok: true,
    fileId: file.getId(),
    name: file.getName(),
    bytes: file.getSize(),
    folderId: folderId,
    uploadedAt: new Date().toISOString()
  });
}
