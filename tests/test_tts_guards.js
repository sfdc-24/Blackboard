#!/usr/bin/env node
/*
 * SFDC24 — paid-voice guard proofs (site P0 issue 2)
 * claude-code-cli, 2026-09-05 · WRK-codex-site-p0-20260904T212005956Z
 *
 * WHAT IS BEING PROVED
 *   Every paid text-to-speech render is bought with a real card, so the only
 *   thing standing between a visitor and an unbounded bill is code. Two defects
 *   were open against that code:
 *
 *     A. Paid renders survived the chat caps. `offline_` answers ok:true with a
 *        canned note, and the mint condition only looked at `ok`, so a session
 *        past its cap -- or the whole site past its daily cap, or CHAT_ENABLED
 *        turned off -- kept minting keys and kept buying speech.
 *     B. The key was single-use by comment only. `cache.get` then
 *        `cache.remove` is not atomic, so two requests carrying the same key
 *        both read the text and both paid.
 *
 *   The unit under test is therefore not "does it speak" but "how many times
 *   does it reach for the paid speech provider". Every assertion below counts
 *   calls to OpenAI's audio endpoint separately from Codex chat calls. Nothing
 *   here touches the network, the live script, or a real key.
 *
 * WHY THERE IS A LEGACY WITNESS
 *   A test that passes against the fix proves nothing on its own -- it might be
 *   asserting something that was always true. T10 and T11 re-run two scenarios
 *   against the pre-fix code shape and show them FAILING to hold the budget.
 *   That is what makes T2 and T8 evidence rather than decoration.
 *
 * RUN
 *   node tests/test_tts_guards.js
 *
 * Loads the tracked Governor source of truth. This is offline evidence about
 * the reviewed source; production still needs immutable-version read-back.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');
const { gasPath } = require('./gas_source.cjs');

let CODE_PATH;
try { CODE_PATH = gasPath('governor-page-api', 'Code'); }
catch (e) {
  console.error('SKIP-AS-FAILURE: ' + e.message);
  console.error('The tracked Governor source tree is incomplete.');
  process.exit(2);
}
const SOURCE = fs.readFileSync(CODE_PATH, 'utf8');

// ---------------------------------------------------------------- harness ---
// A minimal Apps Script runtime. Only the services Code.js actually reaches for
// on these paths are present; anything else would be a silent stub pretending
// the system is simpler than it is.
function makeRuntime(opts) {
  opts = opts || {};

  const calls = { openai: 0, codex: 0, anthropic: 0 };
  const props = new Map(Object.entries(opts.props || {}));
  const cacheStore = new Map();
  let lockHeld = false;
  let lockContentionSeen = 0;
  const scriptProperties = {
    getProperty: (k) => (props.has(k) ? props.get(k) : null),
    setProperty: (k, v) => { props.set(k, String(v)); return scriptProperties; },
    setProperties: (values) => {
      if (!lockHeld) throw new Error('chat budget write without script lock');
      Object.entries(values).forEach(([k, v]) => props.set(k, String(v)));
      return scriptProperties;
    },
    deleteProperty: (k) => { props.delete(k); return scriptProperties; },
    getProperties: () => Object.fromEntries(props)
  };

  // One script-wide lock, and NOT re-entrant. A real LockService lock is held
  // by an execution, so a second concurrent execution asking for it waits and
  // then throws. Modelling it as re-entrant would quietly make the race under
  // test impossible to express.
  const lock = {
    waitLock() {
      if (lockHeld) { lockContentionSeen++; throw new Error('Could not acquire lock'); }
      lockHeld = true;
    },
    releaseLock() { lockHeld = false; }
  };

  const cache = {
    get(k) {
      const hit = cacheStore.has(k) ? cacheStore.get(k) : null;
      if (opts.onCacheGet) opts.onCacheGet(k, { lockHeld: () => lockHeld });
      return hit;
    },
    put(k, v) { cacheStore.set(k, String(v)); },
    remove(k) { cacheStore.delete(k); }
  };

  function response(code, body) {
    return {
      getResponseCode: () => code,
      getContentText: () => body,
      getContent: () => Buffer.from(body, 'utf8')
    };
  }

  const sandbox = {
    console,
    Logger: { log() {} },
    Session: {
      getActiveUser: () => ({ getEmail: () => '' }),
      getEffectiveUser: () => ({ getEmail: () => 'owner@example.invalid' })
    },
    PropertiesService: {
      getScriptProperties: () => scriptProperties
    },
    CacheService: { getScriptCache: () => cache },
    LockService: { getScriptLock: () => lock },
    Utilities: {
      Charset: { UTF_8: 'UTF_8' },
      DigestAlgorithm: { SHA_256: 'SHA_256' },
      getUuid: () => crypto.randomUUID(),
      base64Encode: (bytes) => Buffer.from(bytes).toString('base64'),
      computeDigest: (_algorithm, value) => Array.from(crypto.createHash('sha256').update(String(value)).digest())
        .map((byte) => byte > 127 ? byte - 256 : byte),
      formatDate: (d, _zone, pattern) => pattern === 'yyyyMMdd'
        ? d.toISOString().slice(0, 10).replace(/-/g, '')
        : d.toISOString().slice(0, 10)
    },
    UrlFetchApp: {
      fetch(url) {
        if (String(url).indexOf('api.anthropic.com') >= 0) {
          calls.anthropic++;
          if (opts.anthropicStatus && opts.anthropicStatus !== 200) {
            return response(opts.anthropicStatus, JSON.stringify({ error: { message: 'mocked upstream failure' } }));
          }
          return response(200, JSON.stringify({ content: [{ type: 'text', text: 'A real model reply.' }] }));
        }
        if (String(url).indexOf('api.openai.com/v1/responses') >= 0) {
          calls.codex++;
          if (opts.codexStatus && opts.codexStatus !== 200) {
            return response(opts.codexStatus, JSON.stringify({ error: { message: 'mocked Codex failure' } }));
          }
          return response(200, JSON.stringify({
            status: 'completed',
            output: [{ type: 'message', content: [{ type: 'output_text', text: 'A real Codex reply.' }] }]
          }));
        }
        if (String(url).indexOf('api.openai.com/v1/audio/speech') >= 0) {
          calls.openai++;                       // <- the number this file exists to hold down
          if (opts.onTtsFetch) opts.onTtsFetch();
          return response(200, 'fake-mp3-bytes');
        }
        throw new Error('unexpected outbound call to ' + url);
      }
    },
    ContentService: {
      createTextOutput: (s) => ({ _s: s, setMimeType() { return this; }, getContent() { return this._s; } }),
      MimeType: { JSON: 'application/json', JAVASCRIPT: 'text/javascript' }
    },
    MimeType: { GOOGLE_DOCS: 'application/vnd.google-apps.document', GOOGLE_SHEETS: 'application/vnd.google-apps.spreadsheet' },

    // Defined BEFORE the source loads, because Code.js calls them and they live
    // in sibling files this harness deliberately does not pull in. Quarantined
    // logging and session lookup are proved elsewhere; here they are noise.
    logVisitorQuarantined_() {},
    readSession_() { return null; },
    conversationIdentity_() {
      return { key: 'ca_' + 'a'.repeat(32), token: 'mock-conversation-token', session: null, kind: 'a' };
    }
  };

  const ctx = vm.createContext(sandbox);
  const src = opts.legacyMint
    ? SOURCE.replace(
        'out.ok && out.reply && !out.degraded && ttsConfigured_()',
        'out.ok && out.reply && ttsConfigured_()')
    : SOURCE;
  if (opts.legacyMint && src === SOURCE) {
    throw new Error('legacy-mint rewrite matched nothing -- the guard under test has moved; fix this harness before trusting it');
  }
  vm.runInContext(src, ctx, { filename: 'apps-script/governor-page-api/Code.js' });

  // The pre-fix claim: read, then delete, with nothing in between holding the
  // two together. Restored verbatim in shape so the witness tests describe the
  // code that actually shipped.
  if (opts.legacyClaim) {
    ctx.claimTts_ = function (ak) {
      const c = ctx.CacheService.getScriptCache();
      const t = c.get('tts_' + ak);
      if (!t) return { ok: false, reason: 'expired' };
      c.remove('tts_' + ak);
      return { ok: true, text: t, dailyUsed: 1, dailyCap: 60, sessionUsed: 1, sessionCap: 8 };
    };
  }

  return {
    ctx, calls, props, cache,
    lockContention: () => lockContentionSeen,
    say: (params) => JSON.parse(ctx.voiceReply_(Object.assign({ cb: '' }, params)).getContent()),
    tts: (params) => JSON.parse(ctx.ttsAudio_(Object.assign({ cb: '' }, params)).getContent())
  };
}

const HEALTHY = { OPENAI_KEY: 'sk-test-not-a-real-key', ANTHROPIC_KEY: 'ak-test-not-a-real-key' };

// ------------------------------------------------------------- assertions ---
let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  PASS  ' + name); return; }
  failures++;
  console.log('  FAIL  ' + name + (detail ? '  --> ' + detail : ''));
}
function section(t) { console.log('\n' + t); }

// T1 ------------------------------------------------------------------------
section('T1 · a healthy turn mints a key and buys exactly one render');
{
  const r = makeRuntime({ props: Object.assign({}, HEALTHY) });
  const said = r.say({ vid: 's1', q: 'hello there' });
  check('reply is the model reply, not the offline note', said.ok === true && !said.degraded, JSON.stringify(said).slice(0, 120));
  check('a key was minted', typeof said.ak === 'string' && /^ak[a-f0-9]+$/.test(said.ak || ''), String(said.ak));
  const heard = r.tts({ ak: said.ak });
  check('audio came back', heard.ok === true, JSON.stringify(heard).slice(0, 120));
  check('exactly 1 provider call', r.calls.openai === 1, 'openai=' + r.calls.openai);
}

// T2 ------------------------------------------------------------------------
section('T2 · past the session cap: no key, no spend');
{
  const r = makeRuntime({ props: Object.assign({}, HEALTHY) });
  // Burn the session budget through the real path, not by poking a counter --
  // the cap has to be reached the way a visitor reaches it.
  for (let i = 0; i < 12; i++) r.say({ vid: 's2', q: 'turn ' + i });
  const capped = r.say({ vid: 's2', q: 'one more please' });
  check('reply is the degraded note', capped.ok === true && capped.degraded === 'session-cap', JSON.stringify(capped).slice(0, 140));
  check('visitor still gets words', typeof capped.reply === 'string' && capped.reply.length > 0);
  check('NO key minted', capped.ak === undefined, String(capped.ak));
  const before = r.calls.openai;
  r.tts({ ak: 'ak' + 'deadbeef' });     // a key that was never issued
  check('zero paid renders for a capped session', r.calls.openai === before, 'openai=' + r.calls.openai);
}

// T3 ------------------------------------------------------------------------
section('T3 · past the whole-site daily cap: no key, no spend');
{
  const today = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const p = Object.assign({}, HEALTHY);
  // Past the cap whatever it is today (default, top-up or override): the
  // point is that past it there is no key and no spend.
  p['CHAT_COUNT_' + today] = '100000';
  const r = makeRuntime({ props: p });
  const capped = r.say({ vid: 's3', q: 'hello' });
  check('degraded=daily-cap', capped.degraded === 'daily-cap', JSON.stringify(capped).slice(0, 140));
  check('NO key minted', capped.ak === undefined);
  check('zero paid renders', r.calls.openai === 0, 'openai=' + r.calls.openai);
}

// T4 ------------------------------------------------------------------------
section('T4 · CHAT_ENABLED=off stops the bill, not just the model');
{
  const r = makeRuntime({ props: Object.assign({ CHAT_ENABLED: 'off' }, HEALTHY) });
  const paused = r.say({ vid: 's4', q: 'anyone there' });
  check('degraded=paused', paused.degraded === 'paused', JSON.stringify(paused).slice(0, 140));
  check('NO key minted', paused.ak === undefined);
  check('zero model calls', r.calls.anthropic === 0, 'anthropic=' + r.calls.anthropic);
  check('zero paid renders', r.calls.openai === 0, 'openai=' + r.calls.openai);
}

// T5 ------------------------------------------------------------------------
section('T5 · both chat providers fail: visitor is answered, no speech is bought');
{
  const r = makeRuntime({
    props: Object.assign({}, HEALTHY),
    anthropicStatus: 500,
    codexStatus: 500
  });
  const broke = r.say({ vid: 's5', q: 'hello' });
  check('degraded=api-error', broke.degraded === 'api-error', JSON.stringify(broke).slice(0, 140));
  check('the page is NOT handed a blank reply', typeof broke.reply === 'string' && broke.reply.trim().length > 0);
  check('NO key minted', broke.ak === undefined);
  check('the Codex fallback was attempted', r.calls.codex === 1, 'codex=' + r.calls.codex);
  check('zero paid renders', r.calls.openai === 0, 'openai=' + r.calls.openai);
}

// T6 ------------------------------------------------------------------------
section('T6 · TTS_ENABLED=off keeps the chat and drops the paid voice');
{
  const r = makeRuntime({ props: Object.assign({ TTS_ENABLED: 'off' }, HEALTHY) });
  const said = r.say({ vid: 's6', q: 'hello' });
  check('chat still answers normally', said.ok === true && !said.degraded, JSON.stringify(said).slice(0, 120));
  check('NO key minted', said.ak === undefined);
  const refused = r.tts({ ak: 'ak' + 'c0ffee' });
  check('direct tts call is refused', refused.ok === false && refused.reason === 'no-key', JSON.stringify(refused));
  check('zero paid renders', r.calls.openai === 0, 'openai=' + r.calls.openai);
}

// T7 ------------------------------------------------------------------------
section('T7 · replaying a key sequentially buys nothing the second time');
{
  const r = makeRuntime({ props: Object.assign({}, HEALTHY) });
  const said = r.say({ vid: 's7', q: 'hello' });
  const first = r.tts({ ak: said.ak });
  const second = r.tts({ ak: said.ak });
  check('first use succeeds', first.ok === true);
  check('second use is expired', second.ok === false && second.reason === 'expired', JSON.stringify(second));
  check('exactly 1 provider call', r.calls.openai === 1, 'openai=' + r.calls.openai);
}

// T8 ------------------------------------------------------------------------
section('T8 · two callers racing one key buy exactly one render');
{
  // Re-enter after the winning caller has atomically deleted the property claim
  // and released the lock, but while its provider request is still in flight.
  let reentered = false;
  let inner = null;
  const r = makeRuntime({
    props: Object.assign({}, HEALTHY),
    onTtsFetch() {
      if (reentered) return;
      reentered = true;
      inner = JSON.parse(r.ctx.ttsAudio_({ cb: '', ak: pendingAk }).getContent());
    }
  });
  var pendingAk = r.say({ vid: 's8', q: 'hello' }).ak;
  const outer = JSON.parse(r.ctx.ttsAudio_({ cb: '', ak: pendingAk }).getContent());
  check('the race was actually exercised', reentered === true);
  check('one caller wins', (outer.ok === true) !== (inner && inner.ok === true), 'outer=' + outer.ok + ' inner=' + (inner && inner.ok));
  check('the loser sees expired', (outer.ok ? inner.reason : outer.reason) === 'expired');
  check('exactly 1 provider call', r.calls.openai === 1, 'openai=' + r.calls.openai);
}

// T9 ------------------------------------------------------------------------
section('T9 · the lock is held across the read AND the delete');
{
  let heldDuringRead = null;
  const r = makeRuntime({
    props: Object.assign({}, HEALTHY),
    onCacheGet(key, probe) { if (key.indexOf('tts_') === 0 && heldDuringRead === null) heldDuringRead = probe.lockHeld(); }
  });
  const said = r.say({ vid: 's9', q: 'hello' });
  r.tts({ ak: said.ak });
  check('cache read happened inside the critical section', heldDuringRead === true, 'held=' + heldDuringRead);
}

// T10 -----------------------------------------------------------------------
section('T10 · LEGACY WITNESS — the old mint condition did spend past the cap');
{
  const r = makeRuntime({ props: Object.assign({}, HEALTHY), legacyMint: true });
  for (let i = 0; i < 12; i++) r.say({ vid: 'L1', q: 'turn ' + i });
  const capped = r.say({ vid: 'L1', q: 'one more please' });
  check('old code minted a key for a capped session', typeof capped.ak === 'string', String(capped.ak));
  r.tts({ ak: capped.ak });
  check('old code bought a render the caps had refused', r.calls.openai === 1, 'openai=' + r.calls.openai);
  check('...so T2 is the guard doing work, not a tautology', true);
}

// T11 -----------------------------------------------------------------------
section('T11 · LEGACY WITNESS — the old claim double-charged on one key');
{
  let reentered = false;
  const r = makeRuntime({
    props: Object.assign({}, HEALTHY),
    legacyClaim: true,
    onCacheGet(key) {
      if (reentered || key.indexOf('tts_') !== 0) return;
      reentered = true;
      r.ctx.ttsAudio_({ cb: '', ak: legacyAk });
    }
  });
  var legacyAk = r.say({ vid: 'L2', q: 'hello' }).ak;
  r.ctx.ttsAudio_({ cb: '', ak: legacyAk });
  check('the race was exercised', reentered === true);
  check('old code paid TWICE for one key', r.calls.openai === 2, 'openai=' + r.calls.openai);
  check('...so T8 is the lock doing work, not a tautology', true);
}

// ----------------------------------------------------------------- verdict ---
console.log('\n' + (failures === 0
  ? 'VERDICT: PASS — paid renders are bounded by the chat caps and one key buys one render.'
  : 'VERDICT: FAIL — ' + failures + ' assertion(s) failed. Do not deploy.'));
process.exit(failures === 0 ? 0 : 1);
