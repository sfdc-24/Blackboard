/**
 * SFDC24 - unattended monitor
 * claude-code-cli, 2026-09-03. Mr. Salam: "set up unattended monitoring for the bus".
 *
 * WHY IT LIVES HERE AND NOT ON THE LAPTOP
 *   A Windows scheduled task dies with the laptop lid, and the whole point is to
 *   watch while nobody is at the desk. A time-driven Apps Script trigger runs on
 *   Google infrastructure, so it survives the laptop sleeping, the VM
 *   restarting, and this Claude session ending. scripts/claude_board_worker.ps1
 *   reached the same conclusion in its own header: the intended home is the
 *   ALWAYS-ON VM, for laptop-off resilience.
 *
 * WHAT IT WATCHES
 *   1. www.sfdc24.com and the bare domain, checked and reported SEPARATELY.
 *   2. Board silence - no new row in MON_SILENCE_HOURS means the bus, the fleet
 *      or the gateway has stopped, and everything looks fine until someone reads.
 *   3. ANDON rows - the fleet's own alarm.
 *
 * TWO RULES THAT KEEP IT TRUSTWORTHY
 *   EDGE-TRIGGERED: email fires when a state CHANGES, never on every tick. A site
 *   still down at 03:00 does not email again; a recovery does.
 *
 *   DEBOUNCED: a state only flips after MON_FAIL_STRIKES consecutive checks
 *   agree. The first version lacked this and produced a false DOWN within
 *   thirteen minutes of going live - www was serving perfectly and the apex leg
 *   hiccuped, almost certainly a transient TLS handshake, since the bare domain
 *   rides NameSilo's Caddy fleet with on-demand certificates. It also collapsed
 *   both hostnames into one boolean, so a blip on the redirect convenience
 *   condemned the whole site. A monitor that cries wolf gets ignored exactly
 *   when it is finally right, so both faults are fixed here.
 *
 * SAFETY
 *   - MONITOR_ENABLED=off in Script Properties kills it instantly, no deploy.
 *   - MON_MAX_EMAILS_PER_DAY is a hard stop even if the edge logic is wrong.
 *   - monitorTick is guarded by requireGovernor_(). A time-driven trigger runs
 *     as its owner, so the trigger passes while an anonymous google.script.run
 *     caller does not. Without that guard this is exactly the class of hole the
 *     Version 13 audit closed: a visitor-callable function that sends mail and
 *     makes outbound fetches.
 *   - Every check is individually wrapped. A monitor that throws is a monitor
 *     that is silently off, which is worse than having none.
 */

var MON_STATE_KEY   = 'MONITOR_STATE';
var MON_ENABLED_KEY = 'MONITOR_ENABLED';
var MON_EMAIL_KEY   = 'MONITOR_EMAIL';

var MON_SILENCE_HOURS      = 6;
var MON_MAX_EMAILS_PER_DAY = 12;
var MON_FAIL_STRIKES       = 2;    // consecutive bad checks before a state flips
var MON_SITE_MARKER        = 'first piece of work looks like';

var MON_NL = String.fromCharCode(10);

function monitorDefaultState_() {
  return {
    www: 'unknown', apex: 'unknown', wwwStrikes: 0, apexStrikes: 0,
    silence: 'unknown', lastAndonTs: '', mailDay: '', mailCount: 0
  };
}

function monitorState_() {
  var raw = PropertiesService.getScriptProperties().getProperty(MON_STATE_KEY);
  if (!raw) return monitorDefaultState_();
  try {
    var st = JSON.parse(raw);
    // Older shape used a single `site` field. Start the new fields clean rather
    // than inheriting a verdict the old logic reached for different reasons.
    if (st.www === undefined) {
      st.www = 'unknown'; st.apex = 'unknown';
      st.wwwStrikes = 0; st.apexStrikes = 0;
      delete st.site;
    }
    return st;
  } catch (e) { return monitorDefaultState_(); }
}

function saveMonitorState_(st) {
  st.lastRun = new Date().toISOString();
  PropertiesService.getScriptProperties().setProperty(MON_STATE_KEY, JSON.stringify(st));
}

/** Send one alert, respecting the daily cap. Never throws. */
function monitorNotify_(st, subject, body) {
  var today = Utilities.formatDate(new Date(), 'America/Toronto', 'yyyy-MM-dd');
  if (st.mailDay !== today) { st.mailDay = today; st.mailCount = 0; }
  if (st.mailCount >= MON_MAX_EMAILS_PER_DAY) {
    Logger.log('MONITOR: daily email cap reached - suppressing: %s', subject);
    return false;
  }
  try {
    var to = PropertiesService.getScriptProperties().getProperty(MON_EMAIL_KEY)
             || Session.getEffectiveUser().getEmail();
    MailApp.sendEmail({ to: to, subject: subject, body: body });
    st.mailCount++;
    Logger.log('MONITOR: emailed %s - %s', to, subject);
    return true;
  } catch (e) {
    Logger.log('MONITOR: email failed: %s', e);
    return false;
  }
}

/**
 * Check both hostnames independently. www is where visitors land; the bare
 * domain is a redirect convenience. Conflating them is what caused the false
 * alarm described in the header.
 */
function checkSite_() {
  var out = { wwwOk: false, apexOk: false, detail: '' };
  var notes = [];

  try {
    var www = UrlFetchApp.fetch('https://www.sfdc24.com/', {
      muteHttpExceptions: true, followRedirects: true
    });
    var code = www.getResponseCode();
    if (code !== 200) {
      notes.push('www HTTP ' + code);
    } else if (www.getContentText().indexOf(MON_SITE_MARKER) < 0) {
      notes.push('www 200 but the homepage marker is missing - wrong page or an error shell');
    } else {
      out.wwwOk = true;
      notes.push('www 200 with marker');
    }
  } catch (e) {
    notes.push('www fetch threw: ' + e);
  }

  try {
    var apex = UrlFetchApp.fetch('http://sfdc24.com/', {
      muteHttpExceptions: true, followRedirects: true
    });
    if (apex.getResponseCode() === 200) {
      out.apexOk = true;
      notes.push('apex chain 200');
    } else {
      notes.push('apex HTTP ' + apex.getResponseCode() + ' - check the L-81 CNAME trap');
    }
  } catch (e) {
    notes.push('apex fetch threw: ' + e);
  }

  out.detail = notes.join(' | ');
  return out;
}

/** Newest board row, plus any ANDON newer than the one we last saw. */
function scanBoard_() {
  var found = sheet_(), sh = found.sheet, hdr = found.hdr;
  var last = sh.getLastRow();
  var res = { newestTs: '', ageHours: null, andon: null, rows: last - hdr.row };
  if (last <= hdr.row) return res;

  var n = Math.min(60, last - hdr.row);
  var vals = sh.getRange(last - n + 1, 1, n, hdr.names.length).getValues();
  var tsCol = hdr.idx['Timestamp'], payCol = hdr.idx['Payload'], srcCol = hdr.idx['Source_Tag'];

  for (var i = 0; i < vals.length; i++) {
    var t = iso_(vals[i][tsCol]);
    if (t > res.newestTs) res.newestTs = t;
    var p = String(vals[i][payCol] || '');
    if (p.indexOf('ANDON|') === 0) {
      if (!res.andon || t > res.andon.ts) {
        res.andon = { ts: t, src: String(vals[i][srcCol] || ''), text: p.slice(0, 400) };
      }
    }
  }
  var ms = Date.now() - Date.parse(res.newestTs);
  if (!isNaN(ms)) res.ageHours = ms / 3600000;
  return res;
}

/**
 * One monitor pass. Called by the time-driven trigger.
 * GOVERNOR ONLY - see the safety note in the file header.
 */
function monitorTick() {
  requireGovernor_();

  var props = PropertiesService.getScriptProperties();
  if (String(props.getProperty(MON_ENABLED_KEY) || '').toLowerCase() === 'off') {
    Logger.log('MONITOR: disabled via %s - nothing done', MON_ENABLED_KEY);
    return;
  }

  var st = monitorState_();
  var lines = [];
  var NL = MON_NL;

  // ---- 1. the public site, two independent signals, both debounced --------
  try {
    var site = checkSite_();
    lines.push('site: www=' + (site.wwwOk ? 'up' : 'DOWN') +
               ' apex=' + (site.apexOk ? 'up' : 'DOWN') + '  (' + site.detail + ')');

    st.wwwStrikes  = site.wwwOk  ? 0 : (st.wwwStrikes  || 0) + 1;
    st.apexStrikes = site.apexOk ? 0 : (st.apexStrikes || 0) + 1;

    var wwwNow  = site.wwwOk  ? 'up'
                : (st.wwwStrikes  >= MON_FAIL_STRIKES ? 'down' : (st.www  || 'unknown'));
    var apexNow = site.apexOk ? 'up'
                : (st.apexStrikes >= MON_FAIL_STRIKES ? 'down' : (st.apex || 'unknown'));

    if (st.www !== 'unknown' && st.www !== wwwNow) {
      if (wwwNow === 'down') {
        monitorNotify_(st, 'SFDC24 ALERT: www.sfdc24.com is DOWN',
          'The address visitors actually land on stopped serving correctly, on ' +
          MON_FAIL_STRIKES + ' consecutive checks 15 minutes apart.' + NL + NL +
          site.detail + NL + 'Time: ' + new Date().toString() + NL + NL +
          'Most likely cause, from rule L-81: re-enabling NameSilo Domain ' +
          'Forwarding installs forwarder A records on www as well as the apex, ' +
          'which replaces the www CNAME and makes www redirect to itself. ' +
          'Correct state: www is a CNAME to ghs.googlehosted.com with NO A ' +
          'record, and the apex carries the forwarder A records.' + NL + NL +
          'You get one more email when it recovers, and nothing in between.');
      } else {
        monitorNotify_(st, 'SFDC24: www.sfdc24.com is back up',
          'Serving correctly again.' + NL + NL + site.detail +
          NL + 'Time: ' + new Date().toString());
      }
    }

    // The bare domain is a redirect convenience, not the destination. Worth
    // knowing about; not worth the same alarm, and never while www is fine.
    if (st.apex !== 'unknown' && st.apex !== apexNow && wwwNow === 'up') {
      monitorNotify_(st,
        'SFDC24: bare sfdc24.com ' + (apexNow === 'down' ? 'is not redirecting' : 'redirects again'),
        'www is healthy either way, so anyone typing the full address is fine.' +
        NL + NL + site.detail + NL + 'Time: ' + new Date().toString());
    }

    st.www = wwwNow;
    st.apex = apexNow;
  } catch (e) { lines.push('site check threw: ' + e); }

  // ---- 2. board silence and ANDON ---------------------------------------
  try {
    var b = scanBoard_();
    lines.push('board: ' + b.rows + ' rows, newest ' + b.newestTs +
               (b.ageHours === null ? '' : ' (' + b.ageHours.toFixed(1) + 'h ago)'));

    var quiet = (b.ageHours !== null && b.ageHours > MON_SILENCE_HOURS) ? 'silent' : 'active';
    if (st.silence !== 'unknown' && st.silence !== quiet) {
      if (quiet === 'silent') {
        monitorNotify_(st, 'SFDC24: the board has gone quiet',
          'No new row on Blackboard - Alpha DB for over ' + MON_SILENCE_HOURS + ' hours.' +
          NL + NL + 'Newest row: ' + b.newestTs + NL + NL +
          'That usually means the bus, the WhatsApp gateway or the agent fleet ' +
          'has stopped, rather than that nothing is happening.');
      } else {
        monitorNotify_(st, 'SFDC24: the board is active again',
          'Rows are landing again. Newest: ' + b.newestTs);
      }
    }
    st.silence = quiet;

    if (b.andon && b.andon.ts > String(st.lastAndonTs || '')) {
      monitorNotify_(st, 'SFDC24 ANDON raised by ' + b.andon.src,
        'An instance raised an ANDON on the board.' + NL + NL + b.andon.text +
        NL + NL + 'Raised: ' + b.andon.ts);
      st.lastAndonTs = b.andon.ts;
    }
  } catch (e) { lines.push('board check threw: ' + e); }

  saveMonitorState_(st);
  Logger.log('MONITOR ' + new Date().toISOString() + NL + '  ' + lines.join(NL + '  '));
}
