/**
 * SFDC24 — TEMPORARY: enumerate functions callable via google.script.run
 * claude-code-cli, 2026-09-03. Delete this file's function after reading the log.
 *
 * THE PROBLEM
 *   In Apps Script, google.script.run can invoke ANY top-level function whose
 *   name does not end in an underscore. That convention is not style — it is
 *   the access control. Anyone who loads the reception page can call every
 *   exposed function from their browser console.
 *
 * WHY THIS APPROACH
 *   My first plan was a diagnostic that walks the global scope and logs every
 *   function name. Gemini checked it and it is wrong: in the V8 runtime `this`
 *   does not expose globals, so that approach silently finds nothing. Its
 *   alternative is used here — have the script read its OWN source through the
 *   Apps Script REST API and pattern-match the declarations.
 *
 * REQUIRED SCOPE
 *   appsscript.json must include:
 *     "https://www.googleapis.com/auth/script.projects.readonly"
 *   Without it UrlFetchApp throws a 403 that looks like a bug and is not.
 *   Adding a scope forces re-authorisation on next run.
 *
 * DELIBERATELY NOT UNDERSCORE-SUFFIXED
 *   Private functions do not appear in the editor's Run dropdown, and this has
 *   to be runnable. It is therefore itself exposed while it exists, which is
 *   exactly the problem it is measuring. It only reads source and writes to the
 *   log — no writes, no secrets returned — and it gets deleted the moment the
 *   log is read. Do not leave it deployed.
 */
function AUDIT_TEMP_listExposedFunctions() {
  var scriptId = ScriptApp.getScriptId();
  var url = 'https://script.googleapis.com/v1/projects/' + scriptId + '/content';

  var res = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    muteHttpExceptions: true
  });

  if (res.getResponseCode() !== 200) {
    Logger.log('FAILED %s — %s', res.getResponseCode(),
               res.getContentText().slice(0, 400));
    Logger.log('If this is 403, the script.projects.readonly scope is missing '
               + 'from appsscript.json.');
    return;
  }

  var project = JSON.parse(res.getContentText());
  var declRe = /^\s*function\s+([A-Za-z0-9_$]+)\s*\(/gm;

  var exposed = [];
  var privateCount = 0;

  (project.files || []).forEach(function (file) {
    if (!file.source) return;
    var m;
    declRe.lastIndex = 0;
    while ((m = declRe.exec(file.source)) !== null) {
      var name = m[1];
      if (name.charAt(name.length - 1) === '_') {
        privateCount++;
      } else {
        exposed.push(file.name + '.' + (file.type || '?') + '  ->  ' + name);
      }
    }
  });

  Logger.log('=== FILES IN PROJECT: %s ===', (project.files || []).length);
  (project.files || []).forEach(function (f) {
    Logger.log('  %s (%s) %s chars', f.name, f.type, (f.source || '').length);
  });

  Logger.log('=== PRIVATE (trailing underscore, NOT callable): %s ===', privateCount);
  Logger.log('=== EXPOSED TO google.script.run: %s ===', exposed.length);
  exposed.forEach(function (e) { Logger.log('  ' + e); });
  Logger.log('Of these, only doGet and doPost SHOULD be reachable. '
             + 'Every other name is callable by any visitor.');
}
