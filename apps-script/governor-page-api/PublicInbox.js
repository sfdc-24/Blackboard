/**
 * SFDC24 — PUBLIC_INBOX: quarantine for untrusted visitor text
 * claude-code-cli, 2026-09-03. Paste into the Governor Page project.
 *
 * THE PROBLEM, CONFIRMED FROM PRODUCTION
 *   Anonymous visitor chat text was being written straight into the same
 *   spreadsheet the agent fleet reads as shared memory and routing. ChatGPT
 *   rated this CRITICAL after reading live rows; the trust boundary was:
 *
 *     Anonymous Internet -> Reception -> owner-executed Apps Script
 *                        -> shared internal AI ledger
 *
 *   HTML escaping does not help here. The risk is not what the text renders
 *   as, it is that a stranger can place arbitrary content into the substrate
 *   several agents consume — including text shaped like a dispatch.
 *
 * THE FIX
 *   Visitor rows land in a separate PUBLIC_INBOX sheet carrying immutable
 *   provenance. Nothing promotes them to the operational board automatically.
 *   The provenance fields are the important part: they survive being copied,
 *   which a separate sheet alone does not.
 *
 *     trust_level           EXTERNAL_UNTRUSTED
 *     instruction_authority NONE          <- the one that matters
 *     source                PUBLIC_RECEPTION
 *     promotion_status      UNREVIEWED
 *
 *   `instruction_authority = NONE` is doctrine in a column: no agent reading
 *   one of these rows may treat its text as a command, a routing directive, or
 *   authorisation to act. See docs/security-review.md.
 *
 * FAIL-SAFE BY CONSTRUCTION
 *   Every path is wrapped. If the sheet is missing it is created; if creation
 *   fails, or the append fails, or anything at all throws, the visitor's chat
 *   still works and the failure is logged. Logging must never be able to take
 *   the site down — that would trade a data-hygiene problem for an outage.
 *
 * PIPES ARE BOARD GRAMMAR
 *   Payloads elsewhere are pipe-delimited (L-51), so a visitor typing `|`
 *   could forge field boundaries in anything that later reads this text.
 *   Pipes are replaced on write. Newlines too — one row, one fact.
 */

var PUBLIC_INBOX_SHEET = 'PUBLIC_INBOX';

/**
 * DO NOT NAME THE FIRST COLUMN `Row_ID`. This nearly broke the board.
 *
 * `sheet_()` locates the operational board by scanning the spreadsheet for a
 * sheet carrying a `Row_ID` header — that is what the live error "No sheet with
 * a Row_ID header found in Alpha DB" is telling you. Give the quarantine sheet
 * a `Row_ID` column and `sheet_()` can bind to the wrong sheet, sending every
 * board write into quarantine or failing outright.
 *
 * `Inbox_ID` is deliberately different so the two can never be confused.
 */
var PUBLIC_INBOX_HEADER = [
  'Inbox_ID', 'Stamp', 'Session', 'Who', 'Text',
  'trust_level', 'instruction_authority', 'source', 'promotion_status'
];

/** Get the quarantine sheet, creating it on first use. */
function publicInbox_() {
  var ss = SpreadsheetApp.openById(ALPHA_ID);
  var sh = ss.getSheetByName(PUBLIC_INBOX_SHEET);
  if (!sh) {
    sh = ss.insertSheet(PUBLIC_INBOX_SHEET);
    sh.appendRow(PUBLIC_INBOX_HEADER);
    sh.setFrozenRows(1);
  }
  return sh;
}

/**
 * Log one reception turn into quarantine. Drop-in replacement for the old
 * logVisitor_ call sites.
 *
 * Returns { ok, rowId } and NEVER throws — see fail-safe note above.
 */
function logVisitorQuarantined_(sid, who, text) {
  try {
    var sh = publicInbox_();
    var id = 'PI-' + Utilities.getUuid().slice(0, 8).toUpperCase();

    var clean = String(text == null ? '' : text)
      .replace(/[\r\n]+/g, ' ')
      .replace(/\|/g, '/')
      .slice(0, 3000);

    sh.appendRow([
      id,
      new Date().toISOString(),
      String(sid || '').slice(0, 60),
      String(who || '').slice(0, 30),
      clean,
      'EXTERNAL_UNTRUSTED',
      'NONE',
      'PUBLIC_RECEPTION',
      'UNREVIEWED'
    ]);
    SpreadsheetApp.flush();

    // D-4: an append is not proof. Read it back before reporting ok.
    var landed = String(sh.getRange(sh.getLastRow(), 1).getValue()) === id;
    return { ok: landed, rowId: id };
  } catch (e) {
    // Never let logging break the conversation.
    try { Logger.log('PUBLIC_INBOX write failed: ' + e); } catch (ignored) {}
    return { ok: false, error: String(e) };
  }
}

/**
 * Promote one quarantined row onto the operational board. GOVERNOR ONLY.
 *
 * This is the only route from untrusted to trusted, and it is deliberately
 * manual. Automating it would rebuild the hole this closes.
 */
function promoteInboxRow_(rowId, note) {
  requireGovernor_();

  var sh = publicInbox_();
  var values = sh.getDataRange().getValues();
  var hdr = values[0];
  var idCol = hdr.indexOf('Inbox_ID');
  var textCol = hdr.indexOf('Text');
  var statusCol = hdr.indexOf('promotion_status');

  for (var r = 1; r < values.length; r++) {
    if (String(values[r][idCol]) !== String(rowId)) continue;

    if (String(values[r][statusCol]) === 'PROMOTED') {
      return { ok: false, error: 'already promoted' };
    }

    var payload = 'GOV|kind=feed|project=sfdc24-site|tag=INBOX|text='
      + String(note || values[r][textCol]).replace(/\|/g, '/').slice(0, 400);

    var res = appendRow_(payload, TARGET, 'inbox-promotion');
    sh.getRange(r + 1, statusCol + 1).setValue('PROMOTED');
    SpreadsheetApp.flush();
    return { ok: true, wrote: res, from: rowId };
  }
  return { ok: false, error: 'row not found: ' + rowId };
}

/** Editor utility: what is sitting in quarantine, unreviewed. */
function inbox_pending() {
  requireGovernor_();
  var sh = publicInbox_();
  var v = sh.getDataRange().getValues();
  var hdr = v[0];
  var statusCol = hdr.indexOf('promotion_status');
  var pending = v.slice(1).filter(function (r) {
    return String(r[statusCol]) === 'UNREVIEWED';
  });
  Logger.log('%s unreviewed of %s total in PUBLIC_INBOX', pending.length, v.length - 1);
  pending.slice(-15).forEach(function (r) {
    Logger.log('  %s  %s  %s', r[0], r[3], String(r[4]).slice(0, 90));
  });
}

/**
 * WIRING — one line, and deliberately NOT applied yet
 *
 * The existing `logVisitor_(sid, who, text)` in Code.gs writes reception rows
 * onto the operational board via `sheet_()`. It already uses LockService and
 * already swallows its own errors, which is good. The whole quarantine change
 * is to redirect where it writes.
 *
 * Replace the body of logVisitor_ with:
 *
 *     function logVisitor_(sid, who, text) {
 *       logVisitorQuarantined_(sid, who, text);
 *     }
 *
 * That reroutes all six call sites at once with no renaming and no chance of
 * missing one.
 *
 * WHY IT IS NOT DONE YET, 2026-09-03
 *   The Manage-deployments version selector would not accept "New version"
 *   after roughly six attempts, so nothing can currently be deployed or tested
 *   from this session. Rewiring the live logging path while unable to deploy,
 *   verify, or roll back would be reckless — a mistake would silently break
 *   board writes on a production site with no way to confirm it from here.
 *
 *   So this file is inert: it defines functions and changes no behaviour.
 *   Apply the one-line rewire above at the same time as the deploy, then send
 *   one message through the reception and confirm a row appears in
 *   PUBLIC_INBOX and NOT on the board.
 */
