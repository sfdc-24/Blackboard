/*
  WHERE AN APPS SCRIPT SOURCE FILE ACTUALLY IS.

  The reconciliation in #167 replaced apps-script/governor-page-api/ with a
  verbatim `clasp pull` of the live script. clasp writes server-side JavaScript
  as `.js`, so Code.gs became Code.js, Auth.gs became Auth.js, and so on - and
  nine suites went on reading the old names. Two of them printed
  "SKIP-AS-FAILURE" and exited 2, six threw ENOENT, and only one of the nine was
  a required check, so the rest were red for a day without anyone seeing it.

  Other projects in apps-script/ have not been pulled and are still .gs. So the
  extension is not a constant, and hard-coding either one is the bug that just
  happened. Ask for the base name and get whichever is on disk.
*/
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', 'apps-script');

// base is without extension: gasPath('governor-page-api', 'Code')
function gasPath(project, base) {
  const dir = path.isAbsolute(project) ? project : path.join(ROOT, project);
  for (const ext of ['.js', '.gs']) {
    const p = path.join(dir, base + ext);
    if (fs.existsSync(p)) return p;
  }
  throw new Error(
    'Apps Script source not found: ' + path.join(dir, base) + '.{js,gs}. '
    + 'The tracked source tree is incomplete, or the project was renamed.',
  );
}

function gasRead(project, base) {
  return fs.readFileSync(gasPath(project, base), 'utf8');
}

module.exports = { gasPath, gasRead, GAS_ROOT: ROOT };
