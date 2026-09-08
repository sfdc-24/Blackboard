#!/usr/bin/env node
/*
 * SFDC24 — BEAT 1 domain lookup
 * claude-code-cli, 2026-09-08 · docs/DOMAINS-AND-HOSTING.md
 *
 * WHAT IS ON TRIAL
 *   A visitor's description becomes a DNS label which is then interpolated into
 *   a registrar's URL — and NameSilo's API carries the API key IN THAT URL.
 *   So the assertions here are mostly about two things:
 *
 *     1. Nothing a visitor types can add parameters to that request. The label
 *        is REBUILT from an allowlist, not escaped, and anything that does not
 *        survive the rebuild is dropped rather than repaired.
 *     2. The key never comes back out. Provider error text may be shown to a
 *        visitor or written to the board, and a provider will happily quote our
 *        own request URL back at us inside a message.
 *
 *   Plus the quieter rule this repo keeps relearning: a lookup that cannot run
 *   must say it did not run. "checked: false" is a different thing from "no
 *   results", and collapsing the two would tell a visitor a taken name is free.
 *
 * RUN
 *   node tests/test_domain_lookup.js        (no network, no key)
 */
'use strict';

const path = require('path');
const fs = require('fs');

const SRC = path.join(__dirname, '..', 'prototype', 'beat1', 'domains.js');
if (!fs.existsSync(SRC)) { console.error('SKIP-AS-FAILURE: prototype/beat1/domains.js not found'); process.exit(2); }
const D = require(SRC);

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  PASS  ' + name); return; }
  failures++;
  console.log('  FAIL  ' + name + (detail ? '  --> ' + detail : ''));
}
async function main() {

console.log('\nlabels are rebuilt from an allowlist, never escaped');
{
  check('ordinary words survive', D.toLabel('Acme Roofing') === 'acme-roofing', D.toLabel('Acme Roofing'));
  check('case is folded', D.toLabel('ACME') === 'acme');
  check('accents fold to ASCII', D.toLabel('Montréal') === 'montreal', D.toLabel('Montréal'));
  check('runs of punctuation collapse to one hyphen',
    D.toLabel('a  ...  b') === 'a-b', D.toLabel('a  ...  b'));
  check('leading and trailing hyphens are removed',
    D.toLabel('--edge--') === 'edge', D.toLabel('--edge--'));

  // The injection cases. Every one of these characters is meaningful in the
  // URL that carries our API key.
  const nasty = [
    ['ampersand', 'acme&key=stolen'],
    ['equals', 'acme=1'],
    ['question mark', 'acme?x=1'],
    ['slash', 'acme/../etc'],
    ['percent', 'acme%26key%3Dx'],
    ['comma (namesilo separator)', 'acme,evil.com'],
    ['quote', 'acme"onload="x'],
    ['newline', 'acme\nHost: evil'],
    ['NUL byte', 'acme\u0000evil'],
    ['space-padded', '  acme  ']
  ];
  for (const [label, input] of nasty) {
    const out = D.toLabel(input);
    check(label + ' cannot survive', /^[a-z0-9-]*$/.test(out) && out.indexOf('--') === -1,
      JSON.stringify(out));
  }

  check('a 64-character label is rejected', D.toLabel('a'.repeat(64)) === '');
  check('a 63-character label is kept', D.toLabel('a'.repeat(63)).length === 63);
  check('an all-digit label is rejected', D.toLabel('12345') === '');
  // Two separate rules, and the first version of the code only pretended to have
  // either. Folding xn--80ak6aa92e to xn-80ak6aa92e would name a DIFFERENT
  // domain and then report on its availability, so punycode input is refused.
  check('punycode input is refused outright', D.toLabel('xn--80ak6aa92e') === '', D.toLabel('xn--80ak6aa92e'));
  check('and no output can be punycode-shaped',
    ['a--b', 'x  --  y', 'ab--cd--ef'].every((v) => D.toLabel(v).indexOf('--') === -1),
    JSON.stringify(['a--b', 'x  --  y', 'ab--cd--ef'].map(D.toLabel)));
  check('emoji alone yields nothing', D.toLabel('🎉🎉') === '', D.toLabel('🎉🎉'));
  check('empty input yields nothing', D.toLabel('') === '');
}

console.log('\nString() cannot be made to throw by a crafted request body');
{
  check('an object with a non-callable toString', D.toLabel({ toString: 1 }) === '');
  check('null', D.toLabel(null) === '');
  check('undefined', D.toLabel(undefined) === '');
  check('an array', typeof D.toLabel(['a', 'b']) === 'string');
}

console.log('\nsuggestions come from what they actually said');
{
  const s = D.suggestLabels('I want a website for my roofing company in Mississauga');
  check('uses their words', s.some((x) => x.indexOf('roofing') !== -1), JSON.stringify(s));
  check('drops stopwords', !s.some((x) => x === 'want' || x === 'the' || x === 'for'), JSON.stringify(s));
  check('every suggestion is a legal label', s.every((x) => D.toLabel(x) === x), JSON.stringify(s));
  check('deterministic', JSON.stringify(D.suggestLabels('roofing company')) ===
    JSON.stringify(D.suggestLabels('roofing company')));
  check('a description with no usable words yields none',
    D.suggestLabels('a the it is').length === 0, JSON.stringify(D.suggestLabels('a the it is')));
  check('no invented filler is appended',
    !D.suggestLabels('roofing company').some((x) => /(-hq|-ly|^get-|-io)$/.test(x)),
    JSON.stringify(D.suggestLabels('roofing company')));
}

console.log('\ncandidates stay inside the TLDs we actually understand');
{
  const c = D.candidates(['acme'], ['com', 'ca', 'zzz', 'xyz']);
  check('unknown TLDs are dropped', c.every((d) => /\.(com|ca)$/.test(d)), JSON.stringify(c));
  check('.ca is offered (he is in Ontario)', c.indexOf('acme.ca') !== -1);
  check('the cap is honoured', D.candidates(['a1', 'b2', 'c3'], D.TLDS, 4).length === 4);
  check('an empty TLD list falls back rather than exploding',
    D.candidates(['acme'], [], 9).length > 0);
}

console.log('\nthe key never comes back out');
{
  const KEY = 'ns-live-abcdef0123456789';
  const echoed = 'Bad request: https://www.namesilo.com/api/checkRegisterAvailability?version=1&key='
    + KEY + '&domains=acme.com';
  const red = D.redact(echoed, [KEY]);
  check('a quoted key is removed', red.indexOf(KEY) === -1, red);
  check('a key is removed even when we did not know it',
    D.redact(echoed, []).indexOf(KEY) === -1, D.redact(echoed, []));
  check('token= and api_key= are covered too',
    D.redact('x?token=abc123def456 y', []).indexOf('abc123def456') === -1);
  check('ordinary text is untouched', D.redact('all fine', ['secretvalue']) === 'all fine');
}

console.log('\nthe provider refuses to send anything it did not build');
{
  let threw = false;
  try { D.providers.namesilo.buildRequest(['acme.com&key=x'], { apiKey: 'k'.repeat(12) }); }
  catch (e) { threw = true; }
  check('an unsafe domain is refused at the request boundary', threw);

  const req = D.providers.namesilo.buildRequest(['acme.com', 'acme.ca'], { apiKey: 'k'.repeat(12) });
  check('the built URL contains both domains', /domains=acme\.com%2Cacme\.ca/.test(req.url), req.url);
  check('the key is declared as a secret so errors can redact it',
    req.secrets && req.secrets.length === 1);

  const cf = D.providers.cloudflare.buildRequest(['acme.com'], { apiToken: 't'.repeat(20), accountId: 'acc1' });
  check('cloudflare carries the token in a header, not the query',
    cf.url.indexOf('t'.repeat(20)) === -1 && /^Bearer /.test(cf.headers.Authorization), cf.url);

  // codex, PR34: the cloudflare path validated NOTHING. The domains go out under
  // our credential whether that credential sits in the URL or in a header.
  let cfThrew = false;
  try { D.providers.cloudflare.buildRequest(['-evil.com'], { apiToken: 't'.repeat(20), accountId: 'a' }); }
  catch (e) { cfThrew = true; }
  check('cloudflare also refuses an unsafe domain', cfThrew);
}

console.log('\nthe second boundary is not weaker than the first');
{
  // codex, PR34: the original boundary read /^[a-z0-9-]{1,63}\.[a-z]{2,24}$/, which
  // accepts every single thing toLabel exists to reject. A second boundary weaker
  // than the first is a comment that looks like a check.
  const unsafe = ['-abc.com', 'abc-.com', 'xn--80ak6aa92e.com', 'a--b.com',
                  'no-dot', '.com', 'ok.', 'UPPER.com', 'a b.com', 'a.b.com'];
  for (const d of unsafe) {
    let threw = false;
    try { D.assertSendable_(d, 'test'); } catch (e) { threw = true; }
    check('refuses ' + JSON.stringify(d), threw);
  }
  check('accepts a domain toLabel would actually produce',
    D.assertSendable_('acme-roofing.com', 'test') === 'acme-roofing.com');
  check('accepts .ca', D.assertSendable_('acme.ca', 'test') === 'acme.ca');
}

console.log('\nresults are bound to the domains we asked about');
{
  const rows = [
    { domain: 'asked.com', available: true, price: 9.95, currency: 'USD' },
    { domain: 'never-asked.com', available: true, price: 1, currency: 'USD' }
  ];
  const bound = D.bindToRequested_(rows, ['asked.com']);
  check('a domain we never sent is dropped',
    bound.length === 1 && bound[0].domain === 'asked.com', JSON.stringify(bound));
  check('an empty request set yields nothing', D.bindToRequested_(rows, []).length === 0);
  check('null rows do not throw', D.bindToRequested_(null, ['a.com']).length === 0);
}

console.log('\nthe cloudflare request matches the published contract');
{
  const r = D.providers.cloudflare.buildRequest(['acmecorp.dev'],
    { apiToken: 't'.repeat(20), accountId: 'acc1' });
  check('POST, not GET', r.method === 'POST', String(r.method));
  check('path is /accounts/{id}/registrar/domain-check',
    /\/client\/v4\/accounts\/acc1\/registrar\/domain-check$/.test(r.url), r.url);
  check('no domains in the query string', r.url.indexOf('?') === -1, r.url);
  check('domains travel in a JSON body',
    JSON.stringify(JSON.parse(r.body)) === JSON.stringify({ domains: ['acmecorp.dev'] }), r.body);
  check('content-type is set for the body', r.headers['Content-Type'] === 'application/json');
}

console.log('\nproviders parse a real-shaped response');
{
  const ns = D.providers.namesilo.parse(JSON.stringify({
    reply: {
      available:   { domain: [{ domain: 'acme.com', price: 9.95 }] },
      unavailable: { domain: ['taken.com'] }
    }
  }));
  check('available and unavailable are both returned', ns.length === 2, JSON.stringify(ns));
  check('availability is a boolean, not a string',
    ns[0].available === true && ns[1].available === false);
  check('price is a number when given', ns[0].price === 9.95);
  check('price is null rather than 0 when absent', ns[1].price === null, JSON.stringify(ns[1]));

  // THE DOCUMENTED RESPONSE, copied from Cloudflare's published example rather
  // than imagined. The previous fixture used result[].available/price, a shape
  // that exists nowhere in their docs -- so this suite passed 61/61 while the
  // adapter could not have worked against the live API for a moment. codex found
  // it. A fixture invented alongside the code it tests proves only that the
  // author was consistent with themselves.
  const cf = D.providers.cloudflare.parse(JSON.stringify({
    success: true, errors: [], messages: [],
    result: { domains: [ { name: 'acmecorp.dev', registrable: true, tier: 'standard',
      pricing: { currency: 'USD', registration_cost: '10.11', renewal_cost: '10.11' } } ] }
  }));
  check('cloudflare parses the documented envelope', cf.length === 1, JSON.stringify(cf));
  check('reads result.domains[].name', cf[0].domain === 'acmecorp.dev');
  check('reads registrable, not available', cf[0].available === true);
  check('coerces the STRING registration_cost to a number', cf[0].price === 10.11, String(cf[0].price));
  check('reads the currency from pricing', cf[0].currency === 'USD');
  check('an unregistrable domain reads false',
    D.providers.cloudflare.parse(JSON.stringify({ result: { domains: [
      { name: 'taken.com', registrable: false, pricing: {} } ] } }))[0].available === false);
  check('the old invented shape now yields nothing',
    D.providers.cloudflare.parse(JSON.stringify({
      result: [{ name: 'acme.dev', available: true, price: 10.44 }] })).length === 0);
}

console.log('\n"could not check" is never reported as "not available"');
{
  const noKey = await D.lookup('roofing company', {});
  check('with no transport, checked is false', noKey.checked === false);
  check('and the reason says so', /key/.test(noKey.reason), noKey.reason);
  check('but the candidate names are still offered', noKey.suggestions.length > 0);
  check('and no result claims anything', noKey.results.length === 0);

  const KEY = 'ns-live-abcdef0123456789';
  const boom = await D.lookup('roofing company', {
    provider: 'namesilo',
    config: { apiKey: KEY },
    transport: async (url) => { throw new Error('502 from ' + url); }
  });
  check('a provider failure does not throw outward', boom.ok === true);
  check('a provider failure reports checked:false', boom.checked === false);
  check('and the key is not in the reason', boom.reason.indexOf(KEY) === -1, boom.reason);

  const good = await D.lookup('roofing company', {
    provider: 'namesilo',
    tlds: ['com'],
    config: { apiKey: KEY },
    transport: async () => JSON.stringify({
      reply: { available: { domain: [{ domain: 'roofingcompany.com', price: 9.95 }] } }
    })
  });
  check('a good lookup reports checked:true', good.checked === true);
  check('and returns the row', good.results.length === 1 && good.results[0].available === true);

  const empty = await D.lookup('a the it is', { transport: async () => '{}' });
  check('an unusable description does not call out at all',
    empty.checked === false && /no usable name/.test(empty.reason), empty.reason);
}

console.log('\nnames are only offered to someone who came here to build something');
{
  check('build intent is offered names', D.shouldOfferNames({ intent: 'build' }) === true);
  // The judgement worth protecting: a broken Apex trigger is not a sales lead for
  // a domain, and someone standing up an org already has one. PRODUCT.md is
  // explicit that the sale comes after they hold a working thing.
  check('a fix is NOT offered names', D.shouldOfferNames({ intent: 'fix' }) === false);
  check('an environment is NOT offered names', D.shouldOfferNames({ intent: 'env' }) === false);
  check('an unclear intent is NOT offered names', D.shouldOfferNames({ intent: 'unclear' }) === false);
  check('no sketch at all is not offered names', D.shouldOfferNames(null) === false);
  check('a near-miss intent string is not offered names',
    D.shouldOfferNames({ intent: 'build ' }) === false && D.shouldOfferNames({ intent: 'BUILD' }) === false);
}

console.log('\nthe transport is never handed a URL built from raw visitor text');
{
  let sawUrl = null;
  await D.lookup('acme & key=stolen ?? roofing', {
    provider: 'namesilo',
    tlds: ['com'],
    config: { apiKey: 'k'.repeat(12) },
    transport: async (url) => { sawUrl = url; return '{}'; }
  });
  if (sawUrl === null) {
    check('a URL was built for a partly-hostile description', false, 'transport never called');
  } else {
    const qs = sawUrl.split('?')[1] || '';
    const params = qs.split('&').map((p) => p.split('=')[0]);
    check('the query has exactly the parameters we intended',
      JSON.stringify(params) === JSON.stringify(['version', 'type', 'key', 'domains']),
      JSON.stringify(params));
    check('no smuggled key parameter', params.filter((p) => p === 'key').length === 1);
  }
}

console.log('\n' + (failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)') + '\n');
process.exit(failures === 0 ? 0 : 1);
}
main().catch((e) => { console.error('harness error: ' + (e && e.stack || e)); process.exit(3); });
