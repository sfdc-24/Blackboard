#!/usr/bin/env node
/*
 * Beat-1 local runtime boundary tests.
 *
 * A disposable copy gets a fake .env and an ephemeral loopback port. No test
 * below reaches an authorised conversation route, so no provider request is
 * made. The trial is the local HTTP boundary: capability, static allowlist,
 * bounded error paths, and local-only browser surfaces.
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawn, spawnSync } = require('child_process');

const SOURCE = path.join(__dirname, '..', 'prototype', 'beat1');
const FILES = ['serve.js', 'sketch.js', 'build.js', 'history.js', 'index.html', 'voice.html'];
let failures = 0;

function check(name, condition, detail) {
  if (condition) { console.log('  PASS  ' + name); return; }
  failures++;
  console.log('  FAIL  ' + name + (detail ? '  --> ' + detail : ''));
}

function request(port, requestPath, options) {
  options = options || {};
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: '127.0.0.1',
      port,
      path: requestPath,
      method: options.method || 'GET',
      headers: options.headers || {}
    }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject);
    if (options.body != null) req.write(options.body);
    req.end();
  });
}

async function stop(child) {
  if (!child || child.exitCode != null) return;
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 2000);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
    child.kill();
  });
}

async function main() {
  const tempBase = fs.realpathSync(os.tmpdir());
  const tempRoot = fs.mkdtempSync(path.join(tempBase, 'beat1-runtime-'));
  const tempSource = path.join(tempRoot, 'prototype', 'beat1');
  fs.mkdirSync(tempSource, { recursive: true });
  FILES.forEach((name) => fs.copyFileSync(path.join(SOURCE, name), path.join(tempSource, name)));
  fs.writeFileSync(path.join(tempRoot, '.env'), 'ANTHROPIC_API_KEY=fake-runtime-test-key\nANTHROPIC_MODEL=test-model\n');

  let child;
  let stdout = '';
  let stderr = '';
  try {
    child = spawn(process.execPath, [path.join(tempSource, 'serve.js')], {
      cwd: tempRoot,
      env: Object.assign({}, process.env, { BEAT1_PORT: '0', BEAT1_PROVIDER_CALL_CAP: '1' }),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true
    });
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });

    const port = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('server did not announce an ephemeral port')), 10000);
      const inspect = () => {
        const match = stdout.match(/beat1 prototype on http:\/\/127\.0\.0\.1:(\d+)/);
        if (match) { clearTimeout(timer); resolve(Number(match[1])); }
      };
      child.stdout.on('data', inspect);
      child.once('exit', (code) => {
        clearTimeout(timer);
        reject(new Error('server exited before readiness: ' + code + ' ' + stderr));
      });
    });

    console.log('\nT1 · browser surfaces are local and dependency-bounded');
    const index = await request(port, '/');
    const voice = await request(port, '/voice.html');
    const indexToken = (index.body.match(/RUNTIME_TOKEN = "([a-f0-9]{64})"/) || [])[1];
    const voiceToken = (voice.body.match(/RUNTIME_TOKEN = "([a-f0-9]{64})"/) || [])[1];
    check('typed page served with an injected process capability', index.status === 200 && !!indexToken);
    check('voice page receives the same capability', voice.status === 200 && voiceToken === indexToken);
    check('capability is never left as a source placeholder',
      !index.body.includes('__BEAT1_TOKEN__') && !voice.body.includes('__BEAT1_TOKEN__'));
    check('responses refuse caching, framing and referrer leakage',
      index.headers['cache-control'] === 'no-store' &&
      index.headers['x-frame-options'] === 'DENY' &&
      index.headers['referrer-policy'] === 'no-referrer');
    check('no permissive CORS header exposes the loopback capability',
      index.headers['access-control-allow-origin'] === undefined);
    check('voice has no remote script, stylesheet, image or manifest dependency',
      !/<(?:script|link|img)[^>]+(?:src|href)=["']https?:/i.test(voice.body) &&
      !/<link[^>]+rel=["']manifest["']/i.test(voice.body));
    const typedAccept = index.body.indexOf('Beat1History.acceptUser(history, text)');
    const typedFetch = index.body.indexOf('fetch("/api/turn"');
    check('typed client bounds and retains history before serializing the request',
      typedAccept >= 0 && typedFetch > typedAccept &&
      index.body.includes('history: priorHistory') && !index.body.includes('history: history'));
    const voiceAccept = voice.body.indexOf('Beat1History.acceptUser(history, text)');
    const voiceFetch = voice.body.indexOf('fetch(EXEC');
    check('voice accepts the user message before starting network work',
      voiceAccept >= 0 && voiceFetch > voiceAccept && voice.body.includes('history:priorHistory'));
    check('voice POSTs JSON with header capability to the same-origin turn route',
      voice.body.includes('var EXEC = "/api/turn"') &&
      /fetch\(EXEC,\s*\{[\s\S]*?method:\s*"POST"/.test(voice.body) &&
      /"x-beat1-token"\s*:\s*RUNTIME_TOKEN/.test(voice.body) &&
      /body:\s*JSON\.stringify\(\{text:text,\s*history:priorHistory\}\)/.test(voice.body));
    check('voice contains no JSONP, legacy say route or query credential machinery',
      !/\/api\/say|function\s+jsonp|[?&](?:token|cb)=/i.test(voice.body));

    console.log('\nT2 · arbitrary Host is rejected before token-bearing content');
    const rebound = await request(port, '/', { headers: { host: 'attacker.example:' + port } });
    const wrongPort = await request(port, '/', { headers: { host: '127.0.0.1:' + (port + 1) } });
    check('DNS-rebound host receives a misdirected-request response',
      rebound.status === 421 && wrongPort.status === 421 &&
      /loopback-host-required/.test(rebound.body) && /loopback-host-required/.test(wrongPort.body));
    check('wrong host or port receives no runtime capability',
      !rebound.body.includes(indexToken) && !wrongPort.body.includes(indexToken) &&
      !/[a-f0-9]{64}/.test(rebound.body) && !/[a-f0-9]{64}/.test(wrongPort.body));

    const reboundApi = await request(port, '/api/turn', {
      method: 'POST',
      headers: {
        host: 'attacker.example:' + port,
        'content-type': 'application/json',
        'x-beat1-token': indexToken
      },
      body: JSON.stringify({ text: 'must not run' })
    });
    check('Host rejection precedes even a valid API capability',
      reboundApi.status === 421 && /loopback-host-required/.test(reboundApi.body));

    console.log('\nT3 · static serving is an allowlist, not a filesystem browser');
    const source = await request(port, '/sketch.js');
    const clientRuntime = await request(port, '/history.js');
    const traversal = await request(port, '/..%2f..%2f.env');
    check('implementation source is not served', source.status === 404);
    check('only the intentional browser history helper is served',
      clientRuntime.status === 200 && /Beat1History/.test(clientRuntime.body));
    check('encoded traversal is not served', traversal.status === 404 && !traversal.body.includes('fake-runtime-test-key'));

    console.log('\nT4 · API paths fail closed before provider work');
    const missing = await request(port, '/api/turn', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: 'drive-by request' })
    });
    check('missing capability is rejected', missing.status === 403 && /local-capability-required/.test(missing.body));

    const badJson = await request(port, '/api/turn', {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-beat1-token': indexToken },
      body: '{'
    });
    check('malformed JSON is rejected', badJson.status === 400 && /invalid-json/.test(badJson.body));

    const empty = await request(port, '/api/turn', {
      method: 'POST', headers: { 'content-type': 'application/json', 'x-beat1-token': indexToken },
      body: '{}'
    });
    check('empty turns do not spend a call', empty.status === 400 && /text-required/.test(empty.body));

    const queryOnly = await request(port, '/api/turn?token=' + encodeURIComponent(indexToken));
    check('a query-string capability cannot authenticate an API request',
      queryOnly.status === 403 && /local-capability-required/.test(queryOnly.body));

    const oldJsonp = await request(port, '/api/say', {
      headers: { 'x-beat1-token': indexToken }
    });
    check('the authenticated legacy JSONP route no longer exists', oldJsonp.status === 404);

    const stillUp = await request(port, '/voice.html');
    check('rejected requests do not take down the process', stillUp.status === 200);
    check('neither fake key nor runtime capability is logged',
      !stdout.includes('fake-runtime-test-key') && !stderr.includes('fake-runtime-test-key') &&
      !stdout.includes(indexToken) && !stderr.includes(indexToken));

    const invalidCap = spawnSync(process.execPath, [path.join(tempSource, 'serve.js')], {
      cwd: tempRoot,
      env: Object.assign({}, process.env, { BEAT1_PORT: '0', BEAT1_PROVIDER_CALL_CAP: 'not-a-number' }),
      encoding: 'utf8', timeout: 5000, windowsHide: true
    });
    check('malformed provider-call caps fail closed at startup',
      invalidCap.status === 2 && /must be an integer/.test(invalidCap.stderr || ''));
  } finally {
    await stop(child);
    const relative = path.relative(tempBase, tempRoot);
    if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) {
      fs.rmSync(tempRoot, { recursive: true, force: true });
    }
  }

  console.log('\n' + (failures === 0
    ? 'VERDICT: PASS — the local server is capability-gated, source-allowlisted and local-only.'
    : 'VERDICT: FAIL — ' + failures + ' runtime boundary assertion(s) failed.'));
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error('RUNTIME TEST ERROR:', err && err.stack || err);
  process.exit(1);
});
