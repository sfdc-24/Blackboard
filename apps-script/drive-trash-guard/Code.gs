/**
 * SFDC24 DRIVE TRASH GUARD
 * ============================================================
 *
 * WHY THIS EXISTS
 * On 2026-10-01 the board went down for every agent. Its bus, the Apps Script web
 * app "SFDC 24 - Blackboard", had been moved to the Drive trash on Aug 31. A trashed
 * web app keeps serving, so nothing looked wrong for a month. Then Google's 30-day
 * trash purge deleted it permanently at 20:15:54Z, and BUS_URL answered 404. An Admin
 * "Restore data" brought it back at about 21:55Z.
 *
 * WHAT IT DOES
 * Every hour, guard() checks each PROTECTED folder first, then each file, then this script:
 * - In the trash: untrash it at once and raise an alert. Nothing a guard protects is
 *   meant to be in the trash.
 * - A file whose own folder is in the trash: that folder is untrashed, and the file is never
 *   restored on its own. A lone restore drops a file into My Drive root, and the bus finds
 *   the sheet by title inside FOLDER_ID (Cursor on #312).
 * - Not found (already purged, or access lost): raise an alert saying to run Admin
 *   console > Users > owner > Restore data (Drive) within 25 days.
 * An alert is an email to the account the guard runs as and, when BUS_URL and
 * BUS_SECRET are set in Script Properties, one board row addressed to the fleet.
 * It has no doGet or doPost: nothing on the internet can call it.
 *
 * TO RETIRE A PROTECTED FILE: remove its line below first, push, then trash it.
 * Otherwise the guard puts it back within the hour, which is the point.
 *
 * SETUP (once, by the owner, in the Apps Script editor): select install, click Run, and
 * approve the scopes. install() replaces this script's own guard triggers with one hourly
 * trigger, then runs guard() once.
 * Optional: Script Properties BUS_URL and BUS_SECRET for the board row.
 * TEST: trash the canary below, run guard() (or wait up to an hour), and check that the
 * canary is back and an alert arrived.
 */

var BOARD_TITLE = 'Blackboard - Alpha DB';
var TAG = 'drive-trash-guard';

// [Drive file id, what breaks without it]
var PROTECTED_FILES = [
  ['1meav8p2zkRt-8obarV_fB5Q2EyExCvAaoZa3ro9_fmo4OE_95FpWkfu9', 'the board bus, "SFDC 24 - Blackboard" (BUS_URL web app)'],
  ['120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY', 'the board sheet "Blackboard - Alpha DB" and its bound script'],
  ['1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp', 'SFDC24 - Governor Page API (the site\'s endpoint and monitor)'],
  ['1XBE2qVMiIu8xOq5jks4T3BG3o6CRFx8HKsWXvbXIJ6sOefPUBN-bVOh-', 'Blackboard Production (the V2/Alpha gateway)'],
  ['163qeSCcvsiCtLOrtgdwOOA9627zmXoeWQr3ODsJbioTinwf8avbvhFEq', 'SFDC24 Studio Email Sender'],
  ['1PBfO1sPQmGTXPHWrAAot2wCgSCPizO2uC8RUwUKQ5hn7A7dUq0U4_Q_2', 'SFDC24 Glasses Intake Uploader'],
  ['1vjrB0Ep0sagDngZFAmBg1YXCaBE9rJ2YKPdh64hqTi4', 'the guard\'s canary (trash it to test the guard)']
];

// [Drive folder id, what breaks without it]
var PROTECTED_FOLDERS = [
  ['1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP', 'the board folder (the bus\'s FOLDER_ID)']
];

function guard() {
  var restored = [];
  var problems = [];
  var items = [];
  // Folders first: a file inside a trashed folder reads as trashed, and must come back with its folder.
  PROTECTED_FOLDERS.forEach(function (p) { items.push({ id: p[0], what: p[1], folder: true }); });
  PROTECTED_FILES.forEach(function (p) { items.push({ id: p[0], what: p[1], folder: false }); });
  items.push({ id: ScriptApp.getScriptId(), what: 'this guard itself', folder: false });

  items.forEach(function (item) {
    try {
      var f = item.folder ? DriveApp.getFolderById(item.id) : DriveApp.getFileById(item.id);
      if (!f.isTrashed()) return;
      var top = trashedAncestor_(f);
      if (top) {
        top.setTrashed(false);
        restored.push(item.what + ' (' + item.id + '), by untrashing its folder "' + clean_(top.getName()) + '" ('
          + top.getId() + ')');
      }
      if (f.isTrashed()) {                                  // trashed itself, inside a live folder
        f.setTrashed(false);
        if (!top) restored.push(item.what + ' (' + item.id + ')');
      }
    } catch (e) {
      // One bad item never stops the others.
      problems.push(item.what + ' (' + item.id + '): ' + clean_(String((e && e.message) || e)).slice(0, 200));
    }
  });

  PropertiesService.getScriptProperties().setProperty('LAST_RUN', new Date().toISOString());
  if (restored.length || problems.length) alert_(restored, problems);
  return { restored: restored, problems: problems };
}

// The highest trashed folder above f (first parent at each level), or null when every folder above it is live.
function trashedAncestor_(f) {
  var top = null;
  var it = f.getParents();
  for (var depth = 0; it.hasNext() && depth < 50; depth++) {
    var parent = it.next();
    if (parent.isTrashed()) top = parent;
    it = parent.getParents();
  }
  return top;
}

function alert_(restored, problems) {
  var lines = [];
  if (restored.length) {
    lines.push('UNTRASHED (someone or something had moved it to the trash; a trashed web app is purged 30 days later): '
      + restored.join('; ') + '.');
  }
  if (problems.length) {
    lines.push('NOT FOUND OR UNREADABLE: ' + problems.join('; ') + '. If a file was purged, restore it NOW with Admin '
      + 'console > Users > owner > Restore data (Drive); that only works within 25 days of the deletion.');
  }
  var text = clean_('Drive Trash Guard: ' + lines.join(' '));
  var subject = (problems.length ? 'URGENT: ' : '') + 'Drive Trash Guard: '
    + (restored.length ? restored.length + ' untrashed' : '') + (restored.length && problems.length ? ', ' : '')
    + (problems.length ? problems.length + ' not found' : '');

  var board = postToBoard_(text);                       // first, so the email can say whether the row landed
  try {
    MailApp.sendEmail(Session.getEffectiveUser().getEmail(), subject, text + ' Board row: ' + board + '.');
  } catch (e) {
    console.error('mail failed: ' + e);
  }
}

function postToBoard_(text) {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('BUS_URL');
  var secret = props.getProperty('BUS_SECRET');
  if (!url || !secret) return 'not configured (no BUS_URL or BUS_SECRET in Script Properties)';
  var stamp = new Date().toISOString();
  // Unique per alert: readers dedupe by Row_ID, so two alerts in one second must not share one (Copilot on #312).
  var rid = 'DRIVE-TRASH-GUARD-' + stamp.replace(/[-:]/g, '').slice(0, 15) + 'Z-'
    + Utilities.getUuid().replace(/-/g, '').slice(0, 8);
  var to = 'fleet;claude-code-cli';
  var payload = 'BCB|v=1|id=' + rid + '|phase=RESULT|class=ALERT|from=' + TAG + '|to=' + to.replace(/;/g, ',') + '|' + text;
  var row = [rid, stamp, TAG, to, 'RESULT', payload, 'OPEN', 'FLEET', text.slice(0, 180), ''];
  try {
    var r = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ action: 'append', secret: secret, title: BOARD_TITLE, sheetRow: row }),
      muteHttpExceptions: true,
      followRedirects: true
    });
    // The bus always answers transport 200; the outcome is in its JSON (Codex and Copilot on #312).
    var answer = null;
    try { answer = JSON.parse(r.getContentText()); } catch (ignored) { answer = null; }
    if (r.getResponseCode() === 200 && answer && answer.ok === true) return 'posted as ' + rid;
    var why = answer && answer.error ? clean_(answer.error).slice(0, 120) : 'HTTP ' + r.getResponseCode() + ', no ok:true';
    console.error('board post refused: ' + why);
    return 'NOT posted (' + why + ')';
  } catch (e) {
    console.error('board post failed: ' + e);
    return 'NOT posted (' + clean_((e && e.message) || e).slice(0, 120) + ')';
  }
}

// A BCB payload is '|'-delimited, so the free text must never carry one.
function clean_(s) {
  return String(s).replace(/\|/g, '/').replace(/[\r\n]+/g, ' ');
}

function install() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'guard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('guard').timeBased().everyHours(1).create();
  return guard();
}
