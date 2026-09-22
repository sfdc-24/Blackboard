const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { gasPath } = require('./gas_source.cjs');

for (const project of ['governor-page-api', 'glasses-intake-uploader']) {
  function load(stamped) {
    const fail = () => { throw new Error('Health route attempted data/provider access'); };
    const forbidden = new Proxy({}, { get: fail });
    const context = vm.createContext({ PropertiesService: forbidden, UrlFetchApp: forbidden,
      DriveApp: forbidden, SpreadsheetApp: forbidden, Session: forbidden, CacheService: forbidden,
      ScriptApp: forbidden, HtmlService: forbidden });
    vm.runInContext(fs.readFileSync(gasPath(project, 'Code'), 'utf8'), context);
    context.json_ = value => value;
    if (stamped) context.sfdc24BuildIdentity_ = () => ({ schema: 1, service: 'sfdc24-build',
      project, commit: 'a'.repeat(40), sourceSha256: 'b'.repeat(64) });
    return context;
  }

  test(`${project}: build health returns only metadata without accessing data or providers`, () => {
    const context = load(true);
    const value = context.doGet({ parameter: { health: 'build', nonce: 'c'.repeat(32), action: 'tts', format: 'json' } });
    assert.deepEqual(JSON.parse(JSON.stringify(value)), { ok: true, schema: 1, service: 'sfdc24-build',
      project, commit: 'a'.repeat(40), sourceSha256: 'b'.repeat(64), nonce: 'c'.repeat(32) });
  });

  test(`${project}: unstamped source fails the build check`, () => {
    const value = load(false).doGet({ parameter: { health: 'build', nonce: 'c'.repeat(32) } });
    assert.equal(value.ok, false);
    assert.equal(value.error, 'build identity unavailable');
  });

  test(`${project}: health route does not reflect arbitrary visitor input`, () => {
    const value = load(true).doGet({ parameter: { health: 'build', nonce: '<script>bad</script>' } });
    assert.equal(value.nonce, '');
  });

  test(`${project}: existing entrypoint behavior is retained`, () => {
    const context = load(true);
    if (project === 'governor-page-api') {
      let authChecks = 0;
      context.whoami_ = () => { authChecks += 1; return { isGovernor: false }; };
      assert.equal(context.doGet({ parameter: { format: 'json' } }).error, 'not authorized');
      assert.equal(authChecks, 1);
    } else {
      const value = context.doGet();
      assert.equal(value.service, 'sfdc24-glasses-uploader');
      assert.equal(value.version, 2);
      assert.deepEqual(Array.from(value.actions), ['upload', 'prune']);
    }
  });
}
