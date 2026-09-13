const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const project = path.join(__dirname, '..', 'apps-script/governor-page-api');

function monitor(initial = {}) {
  const properties = {};
  const delivered = [];
  let mailWorks = false;
  let day = '2026-09-06';
  let attempts = 0;
  const site = { wwwOk: true, apexOk: true, detail: 'fixture' };
  const board = { rows: 12, newestTs: '2026-09-06T12:00:00Z', ageHours: 0.1, unreadableTs: 0, andon: null };
  const context = vm.createContext({
    PropertiesService: { getScriptProperties: () => ({
      getProperty: key => properties[key] || null,
      setProperty: (key, value) => { properties[key] = value; },
    }) },
    Utilities: { formatDate: () => day },
    Session: { getEffectiveUser: () => ({ getEmail: () => 'fixture@example.invalid' }) },
    MailApp: { sendEmail: message => {
      attempts++;
      if (!mailWorks) throw new Error('You do not have permission to call MailApp.sendEmail. Required permissions: https://www.googleapis.com/auth/script.send_mail');
      delivered.push(message);
    } },
    Logger: { log: () => {} },
    requireGovernor_: () => {},
  });
  vm.runInContext(fs.readFileSync(path.join(project, 'Monitor.gs'), 'utf8'), context);
  properties.MONITOR_STATE = JSON.stringify({ ...context.monitorDefaultState_(),
    www: 'up', apex: 'up', silence: 'active', ...initial });
  context.checkSite_ = () => site;
  context.scanBoard_ = () => board;
  return {
    site, board, delivered,
    tick: () => context.monitorTick(),
    state: () => JSON.parse(properties.MONITOR_STATE),
    allowMail: () => { mailWorks = true; },
    nextDay: () => { day = '2026-09-07'; },
    attempts: () => attempts,
  };
}

function siteCheck({ wwwCode = 200, wwwBody = '', apexCode = 200 } = {}) {
  const requests = [];
  const response = (code, body = '') => ({
    getResponseCode: () => code,
    getContentText: () => body,
  });
  const context = vm.createContext({
    UrlFetchApp: { fetch: (url, options) => {
      requests.push({ url, options: { ...options } });
      if (url === 'https://www.sfdc24.com/') return response(wwwCode, wwwBody);
      if (url === 'http://sfdc24.com/') return response(apexCode);
      throw new Error(`unexpected URL: ${url}`);
    } },
  });
  vm.runInContext(fs.readFileSync(path.join(project, 'Monitor.gs'), 'utf8'), context);
  return {
    marker: context.MON_SITE_MARKER,
    result: JSON.parse(JSON.stringify(context.checkSite_())),
    requests,
  };
}

test('v31 site check accepts the invariant canonical homepage marker', () => {
  const marker = '<link rel="canonical" href="https://www.sfdc24.com/">';
  const checked = siteCheck({
    wwwBody: `<html><head>${marker}</head><body>` +
      '<h2 class="sec">The first piece of work is a document</h2></body></html>',
  });

  assert.equal(checked.marker, marker);
  assert.deepEqual(checked.result, {
    wwwOk: true,
    apexOk: true,
    detail: 'www 200 with marker | apex chain 200',
  });
  assert.deepEqual(checked.requests.map(({ url, options }) => ({ url, options })), [
    {
      url: 'https://www.sfdc24.com/',
      options: { muteHttpExceptions: true, followRedirects: true },
    },
    {
      url: 'http://sfdc24.com/',
      options: { muteHttpExceptions: true, followRedirects: true },
    },
  ]);
});

test('v31 site check rejects an unrelated 200 page without the canonical marker', () => {
  const checked = siteCheck({
    wwwBody: '<html><head><title>Welcome</title></head><body>Salesforce assessment</body></html>',
  });

  assert.equal(checked.result.wwwOk, false);
  assert.equal(checked.result.apexOk, true);
  assert.match(checked.result.detail, /homepage marker is missing - wrong page or an error shell/);
});

test('v31 site check rejects a branded 200 error shell despite current and retired copy', () => {
  const checked = siteCheck({
    wwwBody: '<html><head><title>Page not found - SFDC24</title></head>' +
      '<body><a>sfdc24</a><h1>That page is not here</h1>' +
      '<h2>The first piece of work is a document</h2>' +
      '<p>first piece of work looks like</p></body></html>',
  });

  assert.equal(checked.result.wwwOk, false);
  assert.equal(checked.result.apexOk, true);
  assert.match(checked.result.detail, /homepage marker is missing - wrong page or an error shell/);
});

test('v31 retries an ANDON after a real mail failure and deduplicates only after success', () => {
  const m = monitor();
  m.board.andon = { ts: '2026-09-06T12:00:00Z', src: 'fixture', text: 'ANDON|test alert' };
  m.tick();
  assert.equal(m.attempts(), 1);
  assert.equal(m.state().lastAndonTs, '');
  assert.equal(m.state().mailCount, 0);
  m.allowMail();
  m.tick();
  assert.equal(m.delivered.length, 1);
  assert.match(m.state().lastAndonTs, /ANDON\|test alert/);
  m.tick();
  assert.equal(m.delivered.length, 1);
  assert.equal(m.attempts(), 2);
});

for (const field of ['www', 'apex']) {
  test(`v31 ${field} outage debounce keeps measuring while a failed notification remains pending`, () => {
    const m = monitor();
    m.site[field + 'Ok'] = false;
    m.tick();
    assert.equal(m.attempts(), 0, 'one bad observation does not alert');
    m.tick();
    assert.equal(m.attempts(), 1);
    assert.equal(m.state()[field], 'up', 'failed mail must not record the notification as sent');
    assert.equal(m.state()[field + 'Strikes'], 2, 'measurements still advance');
    m.allowMail();
    m.tick();
    assert.equal(m.state()[field], 'down');
    assert.equal(m.delivered.length, 1);
    m.tick();
    assert.equal(m.delivered.length, 1);
    assert.equal(m.state()[field + 'Strikes'], 4);
  });
}

for (const [before, age, after] of [['active', 8, 'silent'], ['active', null, 'unreadable'], ['silent', 0.1, 'active']]) {
  test(`v31 board transition ${before} to ${after} retries until the notification succeeds`, () => {
    const m = monitor({ silence: before });
    m.board.ageHours = age;
    m.tick();
    assert.equal(m.state().silence, before);
    assert.equal(m.attempts(), 1);
    m.allowMail();
    m.tick();
    assert.equal(m.state().silence, after);
    assert.equal(m.delivered.length, 1);
    m.tick();
    assert.equal(m.delivered.length, 1);
  });
}

test('v31 preserves a pending alarm at the daily cap and delivers after the quota day changes', () => {
  const m = monitor({ mailDay: '2026-09-06', mailCount: 12 });
  m.board.andon = { ts: '2026-09-06T12:00:00Z', src: 'fixture', text: 'ANDON|quota test' };
  m.allowMail();
  m.tick();
  assert.equal(m.attempts(), 0);
  assert.equal(m.state().lastAndonTs, '');
  m.nextDay();
  m.tick();
  assert.equal(m.delivered.length, 1);
  assert.equal(m.state().mailCount, 1);
});

test('v31 manifest retains the complete explicit scope set without expanding it', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(project, 'appsscript.json'), 'utf8'));
  assert.deepEqual(manifest.oauthScopes?.slice().sort(), [
    'spreadsheets', 'script.external_request', 'script.scriptapp',
    'script.send_mail', 'userinfo.email', 'userinfo.profile',
  ].map(scope => 'https://www.googleapis.com/auth/' + scope).sort());
});
