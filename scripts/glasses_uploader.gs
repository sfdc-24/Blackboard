/**
 * SFDC24 — Glasses Intake Uploader (STANDALONE Apps Script web app)
 * claude-code-cli, 2026-09-02. wf=GLASSES-INTAKE sub=WEBCAM-CAPTURE.
 *
 * WHY THIS EXISTS RATHER THAN A PATCH TO THE BUS
 *   The `upload` action was written for the v1 bus (scripts/codegs_upload_action.gs),
 *   but on 2026-09-02 the bus's Apps Script PROJECT could not be located: the live
 *   deployment (…GOP7G_RcXrjQ) answers requests and appends to Drive, yet it does
 *   not appear in this account's script.google.com project list, and Drive returns
 *   only two script projects (Governor Page API, Blackboard Production) — neither
 *   of which is the bus. Until that is resolved the bus cannot be patched at all.
 *
 *   So this is a SEPARATE, single-purpose web app. It touches nothing the fleet
 *   depends on, and it can be deleted the day the bus becomes patchable again.
 *
 * DEPLOY (about two minutes)
 *   1. script.google.com → New project → name it "SFDC24 Glasses Intake Uploader".
 *   2. Delete the myFunction stub, paste this whole file, Save.
 *   3. Project Settings (gear) → Script Properties → Add script property:
 *        UPLOAD_SECRET = <a long random value>
 *      Do NOT reuse BUS_SECRET here. A fresh value means this endpoint's blast
 *      radius is this one folder, not the whole board (D-18).
 *      Optional: FOLDER_ID = 1skJIwAYenlwMovtW07BsdFOi18cFchlZ  (Glasses Intake)
 *   4. Deploy → New deployment → type Web app → Execute as: Me →
 *      Who has access: Anyone → Deploy → authorize.
 *   5. Copy the Web app URL (it ENDS IN /exec) and hand it over with the secret.
 *
 * VERIFY (sends no file content)
 *   GET  the /exec URL  -> {"ok":true,"service":"sfdc24-glasses-uploader",...}
 *   POST {action:'upload', secret:'…'} with no base64
 *                       -> {"ok":false,"error":"base64 content required …"}
 *   Those two answers together mean it is live and correctly gated.
 */

var DEFAULT_FOLDER_ID = '1skJIwAYenlwMovtW07BsdFOi18cFchlZ'; // "Glasses Intake"

function doGet() {
  return json_({
    ok: true,
    service: 'sfdc24-glasses-uploader',
    time: new Date().toISOString()
  });
}

function doPost(e) {
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');

    var secret = PropertiesService.getScriptProperties().getProperty('UPLOAD_SECRET');
    if (!secret) {
      return json_({ ok: false, error: 'UPLOAD_SECRET not set in Script Properties' });
    }
    // Fail closed, and give the same answer for missing and wrong — a probe must
    // not be able to distinguish them.
    if (String(body.secret || '') !== secret) {
      return json_({ ok: false, error: 'Unauthorized' });
    }
    if (String(body.action || 'upload') !== 'upload') {
      return json_({ ok: false, error: 'Unknown action: ' + body.action });
    }

    var filename = String(body.filename || '').trim();
    if (!filename) return json_({ ok: false, error: 'filename required' });
    if (filename.indexOf('/') !== -1 || filename.indexOf('\\') !== -1) {
      return json_({ ok: false, error: 'filename must not contain a path separator' });
    }

    var b64 = body.base64;
    if (typeof b64 !== 'string' || !b64) {
      return json_({ ok: false, error: 'base64 content required (non-empty string)' });
    }

    var bytes;
    try {
      bytes = Utilities.base64Decode(b64);
    } catch (err) {
      return json_({ ok: false, error: 'base64 did not decode: ' + err });
    }
    // REQ-V8QD7R, the defect this codebase has paid for twice: never answer
    // ok:true for a write that put nothing there.
    if (!bytes || !bytes.length) {
      return json_({ ok: false, error: 'decoded payload is empty — refusing to create a 0-byte file' });
    }

    var folderId = body.folderId
      || PropertiesService.getScriptProperties().getProperty('FOLDER_ID')
      || DEFAULT_FOLDER_ID;

    var folder;
    try {
      folder = DriveApp.getFolderById(folderId);
    } catch (err) {
      return json_({ ok: false, error: 'folder not found or not accessible: ' + folderId });
    }

    var file = folder.createFile(
      Utilities.newBlob(bytes, String(body.mimeType || 'application/octet-stream'), filename)
    );

    // Echo what was ACTUALLY written, so the caller can catch a size mismatch
    // without a second round trip.
    return json_({
      ok: true,
      fileId: file.getId(),
      name: file.getName(),
      bytes: file.getSize(),
      folderId: folderId,
      uploadedAt: new Date().toISOString()
    });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
